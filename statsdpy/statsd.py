import eventlet
from eventlet.green import socket
from statsdpy.daemonutils import Daemon, readconf
from logging.handlers import SysLogHandler
import logging
from sys import maxint
import optparse
import time
import sys
import os
import re


class StatsdServer(object):

    def __init__(self, conf):
        TRUE_VALUES = set(('true', '1', 'yes', 'on', 't', 'y'))
        self.logger = logging.getLogger('statsdpy')
        self.logger.setLevel(logging.INFO)
        self.syslog = SysLogHandler(address='/dev/log')
        self.formatter = logging.Formatter('%(name)s: %(message)s')
        self.syslog.setFormatter(self.formatter)
        self.logger.addHandler(self.syslog)
        self.conf = conf
        self.graphite_host = conf.get('graphite_host', '127.0.0.1')
        self.graphite_port = int(conf.get('graphite_port', '2003'))
        self.listen_addr = conf.get('listen_addr', '127.0.0.1')
        self.listen_port = int(conf.get('listen_port', 8125))
        if conf.get('debug', True) in TRUE_VALUES:
            self.debug = True
        else:
            self.debug = False
        self.flush_interval = int(conf.get('flush_interval', 10))
        self.pct_threshold = int(conf.get('percent_threshold', 90))
        self.graphite_addr = (self.graphite_host, self.graphite_port)
        self.keycheck = re.compile(r'\s+|/|[^a-zA-Z_\-0-9\.]')
        self.ratecheck = re.compile('^@([\d\.]+)')
        self.counters = {}
        self.timers = {}
        self.stats_seen = 0

    def report_stats(self, payload):
        """
        Send data to graphite host

        :param payload: Data to send to graphite
        """
        if self.debug:
            print "reporting stats -> {\n%s}" % payload
        try:
            with eventlet.Timeout(5, True) as timeout:
                graphite = socket.socket()
                graphite.connect(self.graphite_addr)
                graphite.sendall(payload)
                graphite.close()
        except Exception as err:
            self.logger.critical("error connecting to graphite: %s" % err)
            if self.debug:
                print "error connecting to graphite: %s" % err

    def stats_flush(self):
        """
        Periodically flush stats to graphite
        """
        payload = []
        while True:
            tstamp = int(time.time())
            eventlet.sleep(self.flush_interval)
            if self.debug:
                print "seen %d stats so far." % self.stats_seen
                print "current counters: %s" % self.counters
            for item in self.counters:
                stats = 'stats.%s %s %s\n' % (item,
                            self.counters[item] / self.flush_interval, tstamp)
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
                            (key, max_threshold, tstamp))
                    payload.append("stats.timers.%s.upper_%d %d ts %d\n" % \
                            (key, self.pct_threshold, max_threshold, tstamp))
                    payload.append("stats.timers.%s.lower %d ts %d\n" % \
                            (key, low, tstamp))
                    payload.append("stats.timers.%s.count %d ts %d\n" % \
                            (key, count, tstamp))
                    self.timers[key] = []
            if payload:
                self.report_stats("".join(payload))
                payload = []

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
            if self.stats_seen >= maxint:
                self.logger.info("hit maxint, reset seen counter")
                self.stats_seen = 0
            self.stats_seen += 1
        except Exception as err:
            self.logger.info("error decoding timer event: %s" % err)
            if self.debug:
                print "error decoding timer event: %s" % err

    def process_counter(self, key, fields):
        """
        Process a received counter event

        :param key: Key of counter
        :param fields: Received fields
        """
        sample_rate = 1.0
        try:
            if len(fields) is 3:
                if self.ratecheck.match(fields[2]):
                    sample_rate = float(fields[2].lstrip("@"))
                else:
                    raise Exception("bad sample rate.")
            counter_value = float(fields[0] or 1) * (1 / float(sample_rate))
            if key not in self.counters:
                self.counters[key] = 0
            self.counters[key] += counter_value
            if self.stats_seen >= maxint:
                self.logger.info("hit maxint, reset seen counter")
                self.stats_seen = 0
            self.stats_seen += 1
        except Exception as err:
            self.logger.info("error decoding counter event: %s" % err)
            if self.debug:
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
                if fields[1] == "ms":
                    self.process_timer(key, fields)
                elif fields[1] == "c":
                    self.process_counter(key, fields)
                else:
                    if self.debug:
                        print "error: unsupported stats type"
                        print "key -> %s\nfields ->%s" % (key, fields)
            else:
                if self.debug:
                    print "error: not enough fields received"
        else:
            if self.debug:
                print "error: invalid request"

    def run(self):
        eventlet.spawn_n(self.stats_flush)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        addr = (self.listen_addr, self.listen_port)
        sock.bind(addr)
        buf = 8192
        self.logger.info("Listening on %s:%d" % addr)
        if self.debug:
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
