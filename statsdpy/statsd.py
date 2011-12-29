import eventlet
from eventlet.green import socket
from daemonutils import Daemon, readconf
import optparse
import time
import sys
import os
import re


class StatsdServer(object):

    def __init__(self, conf):
        self.keycheck = re.compile(r'\s+|/|[^a-zA-Z_\-0-9\.]')
        self.ratecheck = re.compile('^@([\d\.]+)')
        self.counters = {}
        self.timers = {}
        self.pct_threshold = 90
        self.stats_seen = 0
        self.debug = True

    def report_stats(self, payload):
        """
        Send data to graphite host

        :param payload: Data to send to graphite
        """
        if self.debug:
            print "reporting stats"
        try:
            with eventlet.Timeout(5, True) as timeout:
                graphite = socket.socket()
                graphite.connect(("127.0.0.1", 2003))
                graphite.sendall(payload)
                graphite.close()
        except Exception as err:
            print "error connecting to graphite: %s" % err

    def stats_flush(self):
        """
        Periodically flush stats to graphite
        """
        tstamp = int(time.time())
        flush_interval = 10 #seconds not milli
        payload = []
        while True:
            eventlet.sleep(flush_interval)
            if self.debug:
                print "seen %d stats so far." % self.stats_seen
                print "current counters: %s" % self.counters
                print "flushing to graphite"
            for item in self.counters:
                stats = 'stats.%s %s %s\n' % (item,
                            self.counters[item] / flush_interval, tstamp)
                stats_counts = 'stats_counts.%s %s %s\n' % (item,
                                    self.counters[item], tstamp)
                payload.append(stats)
                payload.append(stats_counts)
                self.counters[item] = 0
            for key in self.timers:
                if len(self.timers[key]) > 0:
                    count = len(self.timers[key])
                    low = min(self.timers[key])
                    high = max(self.timers[key])
                    total = sum(self.timers[key])
                    mean = low
                    max_threshold = high
                    tstamp = int(time.time())
                    if count > 1:
                        threshold_index = \
                            int((self.pct_threshold / 100.0) * count)
                        max_threshold = self.timers[key][threshold_index - 1]
                        mean = total / count
                    payload.append("stats.timers.%s.mean %d ts %d\n" % \
                            (key, mean, tstamp))
                    payload.append("stats.timers.%s.upper %d ts %d\n" % \
                            (key, max, tstamp))
                    payload.append("stats.timers.%s.upper_%d %d ts %d\n" % \
                            (key, self.pct_threshold, max_threshold, tstamp))
                    payload.append("stats.timers.%s.lower %d ts %d\n" % \
                            (key, low, tstamp))
                    payload.append("stats.timers.%s.count %d ts %d\n" % \
                            (key, count, tstamp))
                    self.timers[key] = []
            if payload:
                self.report_stats("".join(payload))

    def process_timer(self, key, fields):
        """
        Process a received timer event

        :param key: Key of timer
        :param fields: Received fields
        """
        try:
            if key not in self.timers:
                self.timers[key] = []
            self.timers[key].append(float(fields[0] or 0))
            self.stats_seen += 1
        except Exception as err:
            print "error decoding timer event: %s" % err

    def process_counter(self, key, fields):
        """
        Process a received counter event

        :param key: Key of counter
        :param fields: Received fields
        """
        try:
            if key not in self.counters:
                self.counters[key] = 0
            if len(fields) is 3:
                if self.ratecheck.match(fields[2]):
                    sample_rate = float(fields[2].lstrip("@"))
                else:
                    raise Exception("bad sample rate.")
            self.counters[key] += float(fields[0] or 1) * \
                (1 / float(sample_rate))
            self.stats_seen += 1
        except Exception as err:
            print "error decoding counter event: %s" % err

    def decode_recvd(self, data):
        """
        Decode and process the data from a received event.

        :param data: Data to decode and process.
        """
        bits = data.split(':')
        if len(bits) == 2:
            key = self.keycheck.sub('_', bits[0])
            print "got key: %s" % key
            fields = bits[1].split("|")
            field_count = len(fields)
            if field_count >= 2:
                if fields[1] is "ms":
                    self.process_timer(key, fields)
                elif fields[1] is "c":
                    self.process_counter(key, fields)
                else:
                    print "error: unsupported stats type"
            else:
                print "error: not enough fields received"
        else:
            print "error: invalid request"

    def run(self):
        eventlet.spawn_n(self.stats_flush)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        addr = ('127.0.0.1', 8125)
        sock.bind(addr)
        buf = 8192
        print "Listening on %s:%d" % addr
        while 1:
            data, addr = sock.recvfrom(buf)
            if not data:
                break
            else:
                self.decode_recvd(data)


class Statsd(Daemon):

    def run(self, conf):
        server = StatsdServer(conf)
        server.run()


def run_server():
    usage = '''
    %prog start|stop|restart [--conf=/path/to/some.conf] [--foreground|-f]
    '''
    args = optparse.OptionParser(usage)
    args.add_option('--foreground', '-f', action="store_true",
        help="Run in foreground")
    args.add_option('--conf', default="./statsd.conf",
        help="path to config. default = ./statsd.conf")
    options, arguments = args.parse_args()

    if len(sys.argv) <= 1:
        args.print_help()
        sys.exit(1)

    if not os.path.isfile(options.conf):
        print "Couldn't find a config"
        args.print_help()
        sys.exit(1)

    if options.foreground:
        print "Running in foreground."
        conf = readconf(options.conf)
        statsd = StatsdServer(conf['main'])
        statsd.run()
        sys.exit(0)

    if len(sys.argv) >= 2:
        statsdaemon = Statsd('/tmp/statsd.pid')
        if 'start' == sys.argv[1]:
            conf = readconf(options.conf)
            statsdaemon.start(conf['main'])
        elif 'stop' == sys.argv[1]:
            statsdaemon.stop()
        elif 'restart' == sys.argv[1]:
            statsdaemon.restart()
        else:
            args.print_help()
            sys.exit(2)
        sys.exit(0)
    else:
        args.print_help()
        sys.exit(2)

if __name__ == '__main__':
    run_server()