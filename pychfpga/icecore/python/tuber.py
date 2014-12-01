'''
Tuber object interface
'''

from sqlalchemy import Column, String, Integer

###
### Error classes
###

class TuberError(Exception):
    pass

class TuberRemoteError(TuberError):
    pass

###
### Libraries
###

import urllib2, urlparse, os, collections, socket
from hardware_map import HWMResource

try: import simplejson as json
except ImportError: import json

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

class TuberHWMResource(HWMResource):
    '''A base class for HWMResources that correspond to TuberObjects.

    This is a great way of using the HardwareMap to correspond with
    network resources over a HTTP tunnel. It hides most of the gory
    details and makes your networked resource look and behave like a
    local Python object.

    To use it, you should subclass this TuberHWMResource. This does a
    couple of things:

    * Defines a table in the hardware mapper database, with mandatory
      "tuber_uri" and "tuber_objname" columns. You can add your own
      columns too, of course.

    * Inherits the "tuber" calling mechanism, to seamlessly tunnel
      remote calls.

    We used to support the use of shared libraries (via URLs like
    'file:///path/to/the/library.so'). If this is desirable, I'll
    have to re-instate it.
    '''

    _tuber_meta_cache = None
    _hold_dispatcher = False

#    __abstract__ = True
    __tablename__ = 'armfirmware'
    pk = Column(Integer, primary_key=True)
    tuber_uri = Column(String, nullable=False)
    tuber_objname = Column(String, nullable=False)

    @staticmethod
    def ping(uri, timeout = 0.1):
        """
        Returns a boolean inticating whether a tuber object is available at the specified URI.
        """
        try:
            fh=urllib2.urlopen(uri, '{}', timeout=timeout)
            fh.close()
        except ( urllib2.URLError, socket.timeout) :
            return False
        return True

    # def __init__(self, uri, object_name):
    #     self.tuber_uri = uri
    #     self.tuber_objname = object_name

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

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
        try: calls = self._calls
        except AttributeError: return
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

        return [ r.result for r in json_out ]

    @property
    def __doc__(self):
        '''Construct DocStrings using metadata retrieved from the underlying resource.'''
        return "%s:\t%s\n\n%s" % (
            self._tuber_meta.name,
            self._tuber_meta.summary,
            self._tuber_meta.explanation
        )

    def __dir__(self):
        '''Provide a list of what's available here. (Used for tab-completion.)'''

        d = []
        d.extend(self._tuber_meta.properties)
        d.extend(self._tuber_meta.methods)
        return d

    @property
    def _tuber_meta(self):
        '''Retrieve metadata associated with the remote network resource.

        This data isn't strictly needed to construct "blind" JSON-RPC calls,
        except for user-friendliness:

           * tab-completion requires knowledge of what the board does, and
           * docstrings are useful, but must be retrieved and attached.

        This class reterieves object-wide metadata, which can be used to build
        up properties and values (with tab-completion and docstrings)
        on-the-fly as they're needed.
        '''

        # Since ORM validation and default assignment happens during
        # the SQL INSERT, this is easy to (if you forget to hwm.add(...)
        # and hwm.commit() the new object.) Make a fuss.
        if not self.tuber_objname or not self.tuber_uri:
            raise TuberError("Objname (%s) or URI (%s) not specified!" % (
                self.tuber_objname,
                self.tuber_uri)
            )

        # Cache access.
        if self._tuber_meta_cache:
            return self._tuber_meta_cache

        # Not cached yet: load it.
        json_in = json.dumps({'object': self.tuber_objname})
        json_out = json.loads(
            urllib2.urlopen(self.tuber_uri, json_in).read(),
            object_hook=_tuber_json_object_hook
        )

        if json_out.error:
            raise TuberRemoteError(json_out.error)

        self._tuber_meta_cache = json_out.result
        return self._tuber_meta

    ###
    ### Remote function call magic
    ###

    def __getattr__(self, name):
        '''
        This function is called to get attributes (e.g. class variables and
        functions) that don't exist on "self". Because we build up a cache of
        descriptors for things we've seen before, we only see this call when
        we get an erroneous call, or need to build up a new descriptor for
        something we haven't seen yet.
        '''

        # Refuse to access to _some_types_of_name
        #if name[0]=='_': raise AttributeError("Refusing to access '%s'" % name)
        if name in ('_sa_instance_state',
                    '_calls', '_tuber_meta',
                    '_ipython_display_', 'trait_names', '_getAttributeNames',
                    'getdoc', '__wrapped__', '__call__',
                    '_repr_html_', '_repr_svg_', '_repr_jpeg_',
                    '_repr_png_', '_repr_json_', '_repr_javascript_',
                    '_repr_latex_', '_repr_pdf_'):
            raise AttributeError
        
        # Make sure this method is described by metadata
        meta = self._tuber_meta
        if name not in meta.methods and name not in meta.properties:
            raise TuberRemoteError("'%s' is not a valid attribute! Hint: use ipython, and try tab-completion." % name)

        # Retrieve any specific information tuber cares to share
        json_in = json.dumps({
            'object': self.tuber_objname,
            'property': name
        })

        json_out = json.loads(
            urllib2.urlopen(self.tuber_uri, json_in).read(),
            object_hook=_tuber_json_object_hook
        )
        d = json_out.result

        if name in meta.methods:
            def proto(*args, **kwargs):
                if not hasattr(self, '_calls'):
                    self._calls = []
                self._calls.append({
                    'object': self.tuber_objname,
                    'method': name,
                    'args': args,
                    'kwargs': kwargs
                })

                if not self._hold_dispatcher:
                    return self.flush()[0]
            call = proto

            arg_text = ''
            if hasattr(d, 'args') and d.args:
                for arg in d.args:
                    arg_text += ("%s: %s\n" % (
                        arg.name,
                        arg.description
                    )).expandtabs(12)

            call.__doc__ = "%s(%s)\n\n%s" % (
                    d.name,
                    ', '.join([a.name for a in d.args]),
                    d.explanation
            )

            setattr(self, name, call)
            return getattr(self, name)

        # Fall back on properties.
        setattr(self, name, json_out.result)
        return getattr(self, name)

# vim: sts=4 ts=4 sw=4 tw=80 smarttab expandtab
