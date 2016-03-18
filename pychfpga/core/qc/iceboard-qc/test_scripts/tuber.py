'''
Tuber object interface
'''

import functools
import urllib2
import urlparse
import os
import collections

#
# Error classes
#


class TuberError(Exception):
    pass


class TuberRemoteError(TuberError):
    pass

#
# Libraries
#

try:
    import simplejson as json
except ImportError:
    import json


def _tuber_json_object_hook(d):
    '''
    Convert JSON dictionaries into Python objects. This greatly clarifies
    syntax: for example,

        >>> d.units['HZ']
        u'Hz'

    ...becomes

        >>> d.units.HZ
        u'Hz'
    '''
    return collections.namedtuple('TuberResult', d.keys())(*d.values())


class TuberCategory(object):
    '''This decorator pulls Tuber functions into ORM objects based on categories.

    Here's an example. If "ib" is an IceBoard object, and you have the
    ordinary IceBoard function set_mezzanine_power(), you can run the
    following:

        >>> ib.set_mezzanine_power(True, 1)

    You also have the following ORM object:

        >>> m = ib.mezz1
        >>> print m.mezz_number
        1

    Rather than accessing mezzanine methods through the IceBoard, it
    seems more logical to do things like this:

        >>> m.set_mezzanine_power(True)

    The TuberCategory decorator allows this kind of call. Borrowing from
    FMCMezzanine again, we invoke the TuberCategory decorator as follows:

        @tuber.TuberCategory("Mezzanine", lambda m: m.iceboard,
            {"mezzanine": lambda m: m.mezz_number })
        class FMCMezzanine(HWMResource):
            [...]

    The decorator intercepts Tuber functions that claim to be members
    of the "Mezzanine" category, and using the FMCMezzanine object's
    mezz_number property, fills in "mezzanine" parameters before
    dispatching the function call back to the mezzanine's "iceboard"
    property.

    "Categories" are exported by C code. For example, try the following:

        $ curl -d '{"object":"IceBoard","property":"set_mezzanine_power"}' \
                http://iceboard004.local/tuber|json_pp

    This shell command asks the IceBoard to describe its 'set_mezzanine_power'
    call. The response includes:

        "categories": [ "IceBoard", "Mezzanine" ]'
    '''

    def __init__(self, category, getobject, **arg_mappers):
        self.category = category
        self.getobject = getobject
        self.arg_mappers = arg_mappers

    def __call__(decorator, cls):

        def __getattr__(self, name):
            '''This is a fall-through replacement for __getattr__.

            We assume we're capturing a function call that's missing
            arguments. We fill in these arguments and dispatch the call.
            '''

            # Retrieve an arbitrary instance of the object so we can return a
            # DocString-preserving version. Since 'proto' is cached, we can't
            # use __m for the actual function call (since it might be attached
            # to the wrong object.) As a necessary side-effect, this raises an
            # AttributeException if the attribute doesn't actually exist on the
            # upstream object.
            __m = getattr(decorator.getobject(self), name)

            # Fill in the parameters we know about; leave the rest.
            @functools.wraps(__m)
            def proto(self, *args, **kwargs):

                # Retrieve the object and method. This must happen within proto
                # to ensure we get 'method' from the right object.
                o = decorator.getobject(self)
                method = getattr(o, name)

                # Convert the argument map to an { argument : value }
                # dictionary for the Tuber function. Apply it.
                kwargs.update({n: f(self) for (n, f) in decorator.arg_mappers.iteritems()})

                return method(*args, **kwargs)

            # Set the class method; return the bound version
            setattr(self.__class__, name, proto)
            return getattr(self, name)

        def __dir__(self):
            '''Retrieve a list of class properties/methods that are relevant.

            We try to grab the original list of attributes from the ORM,
            and then augment it with any Tuber functions in our category.
            '''

            o = decorator.getobject(self)
            d = dir(super(self.__class__, self))
            (meta, metap, metam) = o._tuber_get_meta()
            for m in meta.methods:
                if hasattr(metam[m], 'categories') and decorator.category in metam[m].categories:
                    d.append(m)
            return d

        # Augment the class and return it.
        cls.__getattr__ = __getattr__
        cls.__dir__ = __dir__
        cls.hold = lambda self: decorator.getobject(self).hold()
        cls.flush = lambda self: decorator.getobject(self).flush()

        return cls


class TuberObject(object):
    '''A base class for TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject.
    '''

    _hold_dispatcher = False

    def __init__(self, hostname='localhost'):
        self.hostname = hostname

    @property
    def tuber_uri(self):
        '''Retrieve the URI associated with this TuberResource.'''

        if not self.hostname:
            raise TuberError("Mandatory 'hostname' attribute not specified!")

        return 'http://%s/tuber' % self.hostname

    def hold(self, on_hold=True):
        '''Suspend tuber calls, and then dispatch several at once.'''
        self._hold_dispatcher = on_hold
        if not on_hold:
            return self.flush()

    def flush(self):
        '''Dispatch any suspended calls and collect their results.'''
        # Always, after flushing, assume single-stepping.
        self._hold_dispatcher = False

        # Claim all pending calls. If there's nothing to do, don't try.
        try:
            calls = self._calls
        except AttributeError:
            return
        del self._calls

        json_in = json.dumps(calls)
        json_out = json.loads(
            urllib2.urlopen(self.tuber_uri, json_in).read(),
            object_hook=_tuber_json_object_hook
        )

        # TBD: I would love to postpone error-checking until we make use of
        # the relevant call, but I can't do that since we don't always look!
        # There is potentially a use for the "with" keyword here: could we
        # only permit lazy returns within a context?
        for r in json_out:
            if hasattr(r, 'error') and r.error:
                raise TuberRemoteError(r.error.message)

        return [r.result for r in json_out]

    @property
    def __doc__(self):
        '''Construct DocStrings using metadata retrieved from the underlying resource.'''

        (meta, _, _) = self._tuber_get_meta()
        return "%s:\t%s\n\n%s" % (
            meta.name,
            meta.summary,
            meta.explanation
        )

    def __dir__(self):
        '''Provide a list of what's available here. (Used for tab-completion.)'''

        (meta, _, _) = self._tuber_get_meta()
        return meta.properties + meta.methods

    def _tuber_get_meta(self):
        '''Retrieve metadata associated with the remote network resource.

        This data isn't strictly needed to construct "blind" JSON-RPC calls,
        except for user-friendliness:

           * tab-completion requires knowledge of what the board does, and
           * docstrings are useful, but must be retrieved and attached.

        This class reterieves object-wide metadata, which can be used to build
        up properties and values (with tab-completion and docstrings)
        on-the-fly as they're needed.
        '''

        if not self.hostname:
            meta = _tuber_json_object_hook({"properties": [], "methods": []})
            return (meta, [], [])

        if hasattr(self, '_tuber_meta'):
            return (
                self._tuber_meta,
                self._tuber_meta_properties,
                self._tuber_meta_methods
            )

        json_in = json.dumps({'object': self.__class__.__name__})
        json_out = json.loads(
            urllib2.urlopen(self.tuber_uri, json_in).read(),
            object_hook=_tuber_json_object_hook,
        )
        meta = json_out.result
        props = {}
        methods = {}

        # Retrieve all properties
        json_in = json.dumps([{
            'object': self.__class__.__name__,
            'property': p} for p in meta.properties])
        json_out = json.loads(
            urllib2.urlopen(self.tuber_uri, json_in).read(),
            object_hook=_tuber_json_object_hook
        )
        for p, r in zip(meta.properties, json_out):
            props[p] = r.result

        # Retrieve all methods
        json_in = json.dumps([{
            'object': self.__class__.__name__,
            'property': p} for p in meta.methods])
        json_out = json.loads(
            urllib2.urlopen(self.tuber_uri, json_in).read(),
            object_hook=_tuber_json_object_hook
        )
        for m, r in zip(meta.methods, json_out):
            methods[m] = r.result

        self._tuber_meta_properties = props
        self._tuber_meta_methods = methods
        self._tuber_meta = meta

        return (
            self._tuber_meta,
            self._tuber_meta_properties,
            self._tuber_meta_methods
        )

    #
    # Remote function call magic
    #

    def __getattr__(self, name):
        '''
        This function is called to get attributes (e.g. class variables and
        functions) that don't exist on "self". Since we build up a cache of
        descriptors for things we've seen before, we don't need to avoid
        round-trips to the board for metadata in the following code.
        '''

        # Refuse to __getattr__ a couple of special names used elsewhere.
        # These are mostly hints for SQLAlchemy or IPython.
        if name in ('_sa_instance_state',
                    '_calls', '_tuber_meta',
                    '_ipython_display_', 'trait_names', '_getAttributeNames',
                    'getdoc', '__wrapped__', '__call__',
                    '_repr_html_', '_repr_svg_', '_repr_jpeg_',
                    '_repr_png_', '_repr_json_', '_repr_javascript_',
                    '_repr_latex_', '_repr_pdf_'):
            raise AttributeError

        # Make sure this request corresponds to something in the underlying
        # TuberObject.
        (meta, metap, metam) = self._tuber_get_meta()
        if name not in meta.methods and name not in meta.properties:
            raise AttributeError("'%s' is not a valid Tuber method or property!" % name)

        if name in meta.properties:
            # Fall back on properties.
            setattr(self, name, metap[name])
            return getattr(self, name)

        if name in meta.methods:
            d = metam[name]

            def proto(self, *args, **kwargs):
                if not hasattr(self, '_calls'):
                    self._calls = []
                self._calls.append({
                    'object': self.__class__.__name__,
                    'method': name,
                    'args': args,
                    'kwargs': kwargs
                })

                if not self._hold_dispatcher:
                    return self.flush()[0]

            arg_text = ''
            if hasattr(d, 'args') and d.args:
                for arg in d.args:
                    arg_text += ("%s: %s\n" % (
                        arg.name,
                        arg.description
                    )).expandtabs(12)

            proto.__doc__ = "%s(%s)\n\n%s" % (
                d.name,
                ', '.join([a.name for a in d.args]),
                d.explanation
            )

            setattr(self.__class__, name, proto)
            return getattr(self, name)

# vim: sts=4 ts=4 sw=4 tw=80 smarttab expandtab
