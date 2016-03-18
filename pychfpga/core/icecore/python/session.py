'''
YAML session loader
~~~~~~~~~~~~~~~~~~~

Using this code, we can create a hardware map and associated data. If
'session.yaml' is a file containing the following::

    # Hardware Map
    ---

    validity: [ !!timestamp "2001-12-14t21:59:43.10-05:00", ~ ]

    # Create a hardware map with the following contents
    hardware_map: !HardwareMap

        # SQUID controllers
        - &mgngsq4-06-01 !SQUIDController { serial: 06-01 }

        # Mezzanines
        - &fmc2_001 !MGMEZZ04
            serial: FMC2_001
            squid_controller: *mgngsq4-06-01

        # Dfmuxes
        - !Dfmux
            hostname: iceboard004.local
            mezzanines: [ ~, *fmc2_001 ]

...then a session (containing a hardware map) can be instantiated as follows::

    >>> from pydfmux.core import session, dfmux

    >>> s = session.load(file('session.yaml'))
    >>> hwm = s['hardware_map']

    >>> print hwm.query(dfmux.Dfmux).one()
        Dfmux(u'iceboard004.local')

The yaml module also permits parsing a string; extensions in this file permit
loading data from external JSON or YAML files.
'''

__all__ = [
    'YAMLLoader',
    'HWMConstructor',
    'HWMCSVConstructor',
    'IncludedYAMLValue',
    'load_session',
    'set_yaml_loader_class',
]

import yaml
import json
import csv
import os
import sys
import mimetypes
import logging.config
import hardware_map


class HWMCSVConstructor(object):
    '''When parsing a !tagged CSV filename, generate instances of 'cls'.

    The arguments to 'cls' are taken from columns in the CSV, after applying
    any transforms. For a description of transforms, see the
    HWMConstructor DocStrings.
    '''

    def __init__(self, constructor, transforms):
        self._constructor = constructor
        self._transforms = transforms

    def __call__(self, loader, node):
        fn = os.path.join(os.path.dirname(loader.name), node.value)

        classes = []

        with file(fn, 'rU') as f:
            dr = csv.DictReader(f, dialect='excel-tab')

            for m in dr:
                for t in self._transforms:
                    t(loader, m)
                c = self._constructor(loader)(**m)
                classes.append(c)

        if hasattr(loader, 'hwm'):
            loader.hwm.add_all(classes)
            loader.hwm.commit()

        return classes


class HWMConstructor(object):
    '''When parsing a !tagged YAML dictionary, generate an instance of 'cls'.

    The arguments to 'cls' are taken from the YAML dictionary, after applying
    any transforms. Transforms are selected by keys in the YAML dictionary, and
    are functions of the form:

        def transform(x):
            x['foo'] = x['foo'] + 1

    This transform cause the YAML

        !some_tag { foo: 1 }

    to be instantiated as 'cls(foo=2)'. We use this to work around impedance
    mismatches between sensibly serialized HWM and the ORM.
    '''

    def __init__(self, constructor, transforms):
        self._constructor = constructor
        self._transforms = transforms

    def __call__(self, loader, node):
        # 'deep=True' is crucial and pretty much undocumented.
        m = loader.construct_mapping(node, deep=True)

        # Apply transformations, if any.
        for t in self._transforms:
            t(loader, m)

        c = self._constructor(loader)(**m)

        if hasattr(loader, 'hwm'):
            loader.hwm.add(c)
            loader.hwm.commit()

        return c


class IncludedYAMLValue(object):
    '''Container for YAML data coming from an !include snippet.

    There are two intended uses for this class:

    1. To allow structured, human-written HardwareMaps, by allowing one
       file to include another. In this mode, the save() method should
       not be used since it strips formatting from the file (e.g.
       comments).

    2. To allow bits of the hardware map to be updated by software,
       using the save() method. These files don't contain comments and
       can be reformatted and rewritten. The save() method does this.
    '''

    def __new__(cls, filename, value):
        '''Produce an IncludedYAMLValue class with the correct inheritence.

        Note there are two IncludedYAMLValue classes; the one above is
        user-facing, but a thin wrapper around the one below.
        '''

        class IncludedYAMLValue(value.__class__):
            __doc__ = cls.__doc__  # preserve DocStrings

            def __repr__(self):
                return 'IncludedYAMLValue(%r)' % value

            @property
            def filename(self):
                return filename

            def save(self):
                '''Save the data contained in this file back to JSON/YAML.

                Note that comments (and other unparsed information) are
                clobbered. This method is not suitable for round-tripping
                complete HWMs.
                '''
                with file(self.filename, 'w') as f:

                    mimetype = mimetypes.guess_type(self.filename[0])

                    if mimetype == 'application/json':
                        json.dump(value.__class__(self), f, indent=4)
                    else:
                        yaml.dump(value.__class__(self), f,
                                  indent=4, default_flow_style=False)

        # Instantiate and return an IncludedYAMLValue with the right value.
        return IncludedYAMLValue(value)


def yaml_include_constructor(loader, node):
    '''!include tag for YAML.

    Since YAML is a superset of JSON, this tag works for JSON data too.
    '''

    fn = os.path.join(os.path.dirname(loader.name), node.value)
    with file(fn) as f:
        o = yaml.load(f)

    # Instantiate an instance of the wrapper class with the correct value and
    # return it.
    return IncludedYAMLValue(fn, o)


def hwm_constructor(loader, node):
    '''A YAML !HardwareMap constructor for core.HardwareMap objects.

    The node annotated with this tag must be an array of objects to be added.
    '''

    # Because parts of the YAML script can query the HWM, we need to populate
    # the HWM greedily rather than building a list of objects and adding them
    # all at the end. To do so, we embed a HWM in the loader and add elements
    # as they're built.
    loader.hwm = hardware_map.HardwareMap()

    # 'deep=True' is crucial and pretty much undocumented.
    loader.construct_sequence(node, deep=True)

    return loader.hwm


def hwm_lookup_constructor(loader, node):
    '''Permit YAML to make direct HWM accesses.

    We don't always have a direct reference in YAML to the ORM object we need
    to use. For example,

        - !Dfmux
            hostname: iceboard004.local
            mezzanines: [ ~, &fmc2_001 !MGMEZZ04 { serial: FMC2_001 } ]

        - &wafer-arg1a !Wafer
            name: arg1a
            bolometers: !CSVBolometers "wafer_arg1a.csv"

        - !ChannelMapping
            bolometer: !HWMLookup [*wafer-arg1a, bolometer: 1A.6.Y]
            readout_channel: !HWMLookup [*fmc2_001, module: 1, channel: 1]

    There are two !HWMLookups in this example: the first looks up bolometers
    we can't otherwise reach by YAML aliases (since they're loaded in CSV),
    and the second looks up readout channels (that are populated by the ORM,
    and aren't referenced in the YAML at all.)
    '''

    s = loader.construct_sequence(node)
    obj = s.pop(0)

    for lookup in s:
        if not isinstance(lookup, dict) or \
                len(lookup.keys()) != 1:
            raise yaml.YAMLError('HWM lookups require single key:value pairs!'
                                 'Got %r' % lookup)

        key = lookup.keys()[0]
        value = lookup[key]

        # Do an ORM lookup.
        try:
            obj = getattr(obj, key)[value]
        except KeyError:
            raise KeyError("%r has no %s %s" % (obj, key, value))

    return obj


def logging_constructor(loader, node):
    '''A YAML constructor for Python logging.config.dictConfig() entries'''

    n = loader.construct_mapping(node, deep=True)
    logging.config.dictConfig(n)
    return n


def register_yaml_object(yaml_tag=None, transforms=[]):
    """ Class decorator that adds the decorated class to the YAML class
    registry.

    If 'yaml_tag' parametr is specified,  this attribute is used as the yaml
    tag (the prefixing '!' must not be included). Otherwise the tag name is
    based on the class name.

    If the 'transforms' attribute is defined, the specified transforms are
    applied when the YAML object is loaded. The transforms are specified as a
    dictionary, where the key is the transform type, and the values is a tuple
    of argument specific to this transform.
    """
    def decorator(cls):
        tag = '!' + (yaml_tag or cls.__name__)
        YAMLLoader.add_constructor(
            tag, HWMConstructor(lambda l: cls, transforms))
        return cls
    return decorator


class YAMLLoader(yaml.SafeLoader):

    def __init__(self, *args, **kwargs):
        super(YAMLLoader, self).__init__(*args, **kwargs)

        # Plumbing
        self.add_constructor(u'!include', yaml_include_constructor)
        self.add_constructor(u'!logging', logging_constructor)
        self.add_constructor(u'!HardwareMap', hwm_constructor)
        self.add_constructor(u'!HWMLookup', hwm_lookup_constructor)


def set_yaml_loader_class(cls):
    '''Override the YAMLLoader class used to create sessions.'''

    global __yaml_loader_class
    __yaml_loader_class = cls

__yaml_loader_class = YAMLLoader


def load_session(stream, store=True):
    '''Load a YAML document into a Session object.'''
    y = yaml.load(stream, Loader=__yaml_loader_class)
    if store:
        set_session(y)
    return y


def set_session(session):
    '''Store a Session object somewhere it's globally accessible.'''
    global __session_handle
    __session_handle = session


def get_session():
    '''Retrieve the session stored via set_session()'''
    return __session_handle

__session_handle = None

# vim: sts=4 ts=4 sw=4 tw=80 smarttab expandtab
