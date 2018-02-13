"""
Provides a simplified object to handle Prometheus metrics.
"""

import time as time_
import logging
from operator import itemgetter
import gzip
from io import BytesIO

class Metrics(object):
    """ Simplified container to hold Prometheus Metrics.

    YAML representation:
        metric_name (str):
            'type': type (str)
            'doc': documentation (str)
            'entries':
                {(label (str), value (str)), ...} (frozenset) :
                    time1 (int): value1 (float)
                    time2 (int): value2 (float)

    Python representation
        {metric_name:{'type':type, 'doc':doc, 'entries': { frozenset([(label,value),...]) : {time:value, ...}, ...}}, ...}

    """
    def __init__(self, arg=None, default_type=None, latest_only=False, **default_labels):
        self.default_type = default_type
        self.default_labels = default_labels
        self.latest_only = latest_only
        self.last_time = 0
        self.log = logging.getLogger(__name__)
        self.last_time_table = {}
        self.last_compression_ratio = 0

        if arg is None:
            self.metrics = {}
        elif isinstance(arg, Metrics):  # Add all metrics, with the additional default labels
            self.metrics = {}
            self.add(arg)
        elif isinstance(arg, dict):  # add a metrics specified as a dict, with the additional default labels
            m = Metrics()
            m.metrics = arg.copy()
            self.metrics = {}
            self.add(m)
        elif isinstance(arg, list):
            self.metrics = {}
            for item in arg:
                self.add(item)

    #def __len__(self):  # will also me used as __nonzero__
    #    return len(self.metrics)

    def items(self):
        return self.metrics.items()

    def __len__(self):
        length = 0
        for metric_entries in self.metrics.itervalues():
            for time_entries in metric_entries['entries'].itervalues():
                if time_entries is not None:
                    length += len(time_entries)
        return length

    def as_dict(self):
        return self.metrics

    def __iadd__(self, other):
        #print('Metric: adding %r' % other)
        self.add(other)
        return self

    def pop(self):
        """ return a Metric() object with all the current metrics and clear this metric."""
        metrics = Metrics()
        metrics.metrics = self.metrics
        self.clear()
        return metrics

    def clear(self):
        """ removes all the metrics """
        self.last_time = 0
        self.metrics = {}

    def add(self, metric_name, value=None, type=None , doc=None, time=None, **labels):
        """ Add a metric, a metric entry, or add all the metrics from another Metrics object.

        Parameters:

            metric_name (Metric,  str): If `metric_name` is a `Metrics` instance, all the metric
               and metric entries are added to this object, with the labels in `labels` added to
               every metric. If if it a *str*, a new metric is created or information is added to
               the existing metric.

            value (float or int): If not None, a new entry with `time`, `value` and `labels` is added to the metric.

            time (int): time (ms since epoch) to be added to the entry stored when `value` is not None. If time is Node, the current time is used.

            \**labels: labels to be added to the entry stored when `value` is not None

            doc: documentation associated with the metric. Different docs cannot be associated with
                a single metric. If not specified, there will not be a ``# HELP`` entry in the string output.

            type: type associated with the metric. Different types cannot be associated with
                a single metric. If not specified, there will not be a ``# TYPE`` entry in the string output.
        """

        if metric_name is None:
            return self
        # If we pass a Metrics object, merge the metrics into this one.
        elif isinstance(metric_name, Metrics):
            for met_name, met in metric_name.metrics.iteritems():
                self.add(met_name, doc=met['doc'], type=met['type'])
                for label_set, time_entries in met['entries'].iteritems():
                    new_labels = dict(list(label_set) + labels.items())
                    for time, value in time_entries.iteritems():
                        self.add(met_name, value=value, time=time, **new_labels)
            return self
        elif not isinstance(metric_name, basestring):
            #print('Adding metric list')
            for m in metric_name:
                #print('   Adding metric %r' % m)
                self.add(m, value=value, type=type, doc=doc, time=time, **labels)
            return self
        # get the metric from the local dict, or create an empty one
        if metric_name not in self.metrics:
            self.metrics[metric_name] = dict(type=None, doc=None, entries={})
        metric = self.metrics[metric_name]
        #if metric['entries']:
        #    return
        # Assign documentation if some is provided. It must be unique to the metric.
        if metric['doc'] and doc and metric['doc'] != doc:
            raise RuntimeError('Cannot assign different docs to metric %s' % metric_name)
        elif doc:
            metric['doc'] = doc

        # Assign metric type if some is provided. It must be unique to the metric.
        type = type or self.default_type
        if metric['type'] and type and metric['type'] != type.upper():
            raise RuntimeError('Cannot assign different types to metric %s' % metric_name)
        elif type:
            metric['type'] = type.upper()

        # Add entric (value and labels) to the metric
        if value is None:
            return self

        new_time = int(time or (time_.time() * 1000))
        #if self.last_time and new_time < self.last_time:
        #   self.log.warning('%r: out-of-order on metric %s. old time =%i, new time=%i'% (self, metric_name, self.last_time, new_time))
        self.last_time = max(self.last_time, new_time)

        # Combine the labels with the default labels (in a dict to avoid multiple instance of the same label)
        # convert values into strings
        # pack the (key,string_values) pairs into a frozenset, which can be used as a dict key
        new_labels = frozenset((k, str(v)) for k, v in
            dict(self.default_labels.items() + labels.items()).items())
        metric_entries = metric['entries']
        if new_labels not in metric_entries:
            metric_entries[new_labels] = {}
        time_entries = metric_entries[new_labels]
        if self.latest_only:
            time_entries.clear()
        if new_time in time_entries:
           self.log.warning('%r: metric %s at time %i already exist. The old entry will be rewritten' % (self, metric_name, new_time))
        time_entries[new_time] = value
        return self

    def __str__(self):
        return self.get_str()

    def get_last_time(self):
        return self.last_time

    def get_str(self, after=0):
        """ Return a string repreentation of the metrics.

        labels are sorted for nicer display.
        Within a set of samples, samples are sorted by time.

        Parameters:

            after (int or str): Include samples only after the specified time. If an int, this is
                the unix time * 1000. if a string, only the samples since the last call with the tag
                `after` are returned.
        """
        s = []
        if isinstance(after, str):
            after_time = self.last_time_table.setdefault(after, 0)
        else:
            after_time = after or 0
        for metric_name, m in self.metrics.iteritems():
            if m['doc']:
                s.append('# HELP %s %s\n' % (metric_name, m['doc']))
            if m['type']:
                s.append('# TYPE %s %s\n' % (metric_name, m['type']))
            for label_set, time_entries in m['entries'].iteritems():
                if label_set:
                    labels_string = '{' + ','.join('%s="%s"' % (k, v) for k, v in sorted(label_set)) + '}'
                else:
                    labels_string = ''
                for time, value in sorted(time_entries.iteritems()):
                    if time > after_time:
                        s.append('%s%s %.16g %i\n' % (metric_name, labels_string, value, time))
                        #s.append('%s%s %f\n' % (metric_name, labels, entry['value']))
        if isinstance(after, str):
            self.last_time_table[after] = self.last_time
        return ''.join(s)


    def get_gzip(self, after=0):
        f = BytesIO()
        g = gzip.GzipFile(mode="w", fileobj=f)
        uncompressed_metrics = self.get_str(after=after)
        g.write(uncompressed_metrics)
        g.close()
        compressed_metrics = f.getvalue()
        if not uncompressed_metrics:
            self.last_compression_ratio = 0
        else:
            self.last_compression_ratio = 1 - float(len(compressed_metrics)) / (len(uncompressed_metrics))
        return compressed_metrics
