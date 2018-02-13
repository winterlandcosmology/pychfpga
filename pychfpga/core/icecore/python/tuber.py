'''
Tuber object interface
'''

import inspect
# import urllib2
# import urlparse
# import os
import sys
import collections
import logging
import time
import textwrap
import warnings
# import hashlib

import tornado.concurrent
import tornado.ioloop
import tornado.httpclient
import tornado.gen

import async

#tornado.httpclient.AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient", max_clients=300) #, max_buffer_size=200000)  # So we can probe many boards at once (Default is 10)
tornado.httpclient.AsyncHTTPClient.configure(None, max_clients=300, max_buffer_size=200000)  # So we can probe many boards at once (Default is 10)

json_cache = {}

# Prefer simplejson (it's compatible, but faster)
try:
    import simplejson as json
except ImportError:
    import json

__all__ = [
    "TuberError", "TuberRemoteError",
    "TuberCategory", "TuberObject",
]


class TuberError(Exception):
    pass


class TuberRemoteError(TuberError):
    pass


def _tuber_json_object_hook(d):
    '''Convert JSON dictionaries into Python objects.'''
    return collections.namedtuple('TuberResult', d.keys())(*d.values())


class Context(async.Parallelizable):
    '''A context container for TuberCalls. Permits calls to be aggregated.

    Using this interface, you can write code like:

        >>> from pydfmux import Dfmux, macro, asynchronously, Return

        >>> @pydfmux.macro(Dfmux)
        ... def my_macro(d):
        ...     with d.tuber_context() as ctx:
        ...         p = ctx.get_mezzanine_power(2)
        ...         f = yield ctx.get_frequency(ctx.UNITS.HZ, 1, 1, 1)
        ...         ctx.set_frequency(f+1, ctx.UNITS.HZ, 1, 1, 1)
        ...         yield asynchronously(ctx)
        ...     raise Return((yield p))

        >>> my_macro(d)
        True

    Commands are dispatched to the board strictly in-order, but are
    automatically bundled up to reduce traffic. In this example, the first two
    calls are dispatched together, since the result 'p' is not used until
    later.

    There are a couple of important considerations:

        * Calls made on 'ctx' return Futures, which can be converted into
          their results via 'yield'.

        * The final 'yield asynchronously(ctx)' ensures the context queue is
          flushed. It's only necessary if you don't yield the results of the
          final call in the context. If you need this yield and leave it out,
          you'll see a warning and your code will dispatch synchronously (i.e.
          other work in the asynchronous framework won't get done in the
          meantime.)

    Note that you will *not* catch exceptions unless you check for them
    explicitly. For example, in this code:

        >>> with d.tuber_context() as ctx:
        ...     p = ctx.get_mezzanine_power(3)

    the function call is executed, and generates an exception. The exception is
    embedded in 'p' and will not be raised unless you call 'p.result()'!
    '''

    def __init__(self, obj, **ctx_kwargs):
        self.calls = []
        self.obj = obj
        self.ctx_kwargs = ctx_kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        '''Ensure the context is flushed.'''
        if self.calls:
            warnings.warn("Dispatch queue not empty when leaving Context! "
                          "If you want this code to run in parallel, you "
                          "need to use 'yield asynchronously(ctx)' within "
                          "the context.")

            self()  # Let the synchronous __call__ do the hard work.

    @tornado.gen.coroutine
    def __call_async__(self):
        '''Break off a set of calls and return them for execution.'''

        calls = []
        futures = []
        argsize = 0
        while self.calls:
            (n, f, a, k) = self.calls.pop(0)

            calls.append({
                'object': self.obj.tuber_objname,
                'method': n,
                'args': a,
                'kwargs': k
            })
            argsize += sum(sys.getsizeof(arg) for arg in a) + sum(sys.getsizeof(arg) for arg in k.values())
            futures.append(f)

        if calls:
            # Create a HTTP request to complete the call. This is a coroutine,
            # so we queue the call and then suspend execution (via 'yield')
            # until it's complete.

            client = tornado.httpclient.AsyncHTTPClient()
            # client.configure(None, max_clients=50) ## So we can send to 50 boards at once

            log=logging.getLogger(__name__)
            if argsize > 1000:
                h = tuple((call['object'], call['method'], tuple(call['args']), frozenset(call['kwargs'].items())) for call in calls)
                if h in json_cache:
                    log.info('%r: using cashed JSON in request')
                    json_in = json_cache[h]
                else:
                    json_in = json.dumps(calls)
                    json_cache[h] = json_in
            else:
                json_in = json.dumps(calls)
                                                     ## Default is 10
            request = tornado.httpclient.HTTPRequest(
                url=self.obj.tuber_uri,
                method='POST',
                body=json_in,
                connect_timeout=10 * 60,
                request_timeout=10 * 60) # permit calls to be really slow

            t1 = time.time()
            fetch_future = client.fetch(request)
            #if client.queue or len(client.active) > 70:
            #    #log.info("max_clients: %i, %d active, %d queued requests." % (client.max_clients,
            #    #              len(client.active), len(client.queue)))
            #    pass
            response = yield fetch_future
            t2 = time.time()

            # Say something about the call
            # l = logging.getLogger(__name__)
            # l.debug('%r: %.500s => %.500s (%f sec)' % (
            #     self, calls, response.body, t2-t1))

            json_out = json.loads(
                response.body,
                object_hook=_tuber_json_object_hook)

            # Resolve futures
            for (f, r) in zip(futures, json_out):
                if hasattr(r, 'error') and r.error:
                    f.set_exception(TuberRemoteError(r.error.message))
                else:
                    f.set_result(r.result)

    def __getattr__(self, name):

        (meta, metap, metam) = self.obj._tuber_get_meta()

        # Properties are available immediately.
        if name in metap:
            return getattr(self.obj, name)

        # Queue methods calls.
        if name in metam:
            future = tornado.concurrent.Future()

            def caller(*args, **kwargs):

                # Add extra arguments where they're provided
                kwargs = kwargs.copy()
                kwargs.update(self.ctx_kwargs)

                self.calls.append((name, future, args, kwargs))

                # Schedule a queue flush in the future so this call is
                # guaranteed to get dispatched
                io_loop = tornado.ioloop.IOLoop.current()
                io_loop.add_callback(self.__call_async__)

                return future

            return caller

        raise AttributeError("'%s' is not a valid method or property!" % name)


class TuberCategory(object):
    '''Pull Tuber functions into ORM objects based on categories.

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

        def tuber_context(self):
            obj = decorator.getobject(self)
            kwargs = {n: f(self) for (n, f) in decorator.arg_mappers.items()}
            return Context(obj, **kwargs)

        cls.tuber_context = tuber_context

        def __getattr__(self, name):
            '''This is a fall-through replacement for __getattr__.

            We assume we're capturing a function call that's missing
            arguments. We fill in these arguments and dispatch the call.
            '''

            parent = decorator.getobject(self)
            m = getattr(parent, name)

            if isinstance(m, async.Parallelizable):
                class CategoryProto(async.Parallelizable):

                    @tornado.gen.coroutine
                    def __call_async__(p, *args, **kwargs):
                        kwargs = kwargs.copy()
                        kwargs.update({
                            n: f(self)
                            for (n, f) in decorator.arg_mappers.items()
                        })
                        result = yield async.asynchronously(
                            getattr(parent, name), *args, **kwargs)
                        raise tornado.gen.Return(result)

                # Don't cache this with the class; it's bound to a particular
                # instance.
                p = CategoryProto()
                p.__doc__ = inspect.getdoc(m)
                return p

            raise AttributeError()

        cls.__getattr__ = __getattr__

        def __dir__(self):
            '''Retrieve a list of class properties/methods that are relevant.

            We try to grab the original list of attributes from the ORM,
            and then augment it with any Tuber functions in our category.
            '''

            o = decorator.getobject(self)
            # d = dir(super(self.__class__, self)) # This is wrong
            d = set(self.__dict__.keys())
            d |= set.union(*[set(dir(cls)) for cls in type(self).mro()])
            (meta, metap, metam) = o._tuber_get_meta()
            for m in meta.methods:
                if hasattr(metam[m], 'categories') and \
                        decorator.category in metam[m].categories:
                    d.add(m)
            return list(d)

        cls.__dir__ = __dir__

        return cls


class TuberObject(object):
    '''A base class for TuberObjects.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject.
    '''

    @async.async
    def _post(self, command, timeout=20):
        """ Sends an asynchronous POST request as the JSON-encoded `command` object and return the JSON-decoded reply

        Exceptions:

            tornado.httpclient.HTTPError: if HTTP requests fails
            ValueError: If JSON encoding or decoding fails

        """
        t0 = time.time()
        log = logging.getLogger(__name__)
        client = tornado.httpclient.AsyncHTTPClient()
        #log.info('%r: got a HTTP client instance after %f seconds' % (self, time.time()- t0))
        #log.info("max_clients: %i, %d active, %d queued requests." % (client.max_clients,
        #                      len(client.active), len(client.queue)))
        log.info('%r: HTTP POST: Tuber request body is %i bytes' % (self, len(json.dumps(command))))
        request = tornado.httpclient.HTTPRequest(
            url=self.tuber_uri,
            method='POST',
            body=json.dumps(command),
            connect_timeout=timeout,
            request_timeout=timeout)
        #log.info('%r: got a HTTP request instance after %f seconds' % (self, time.time()- t0))

        response = yield client.fetch(request)
        t1 = time.time()
        # h = hashlib.md5(response.body).hexdigest()
        log.info('%r:  HTTP POST: got response after %f seconds. hash=%s' % (self, time.time()- t0, hash(response.body)))
        if response.body in json_cache:
            log.info('%r: HTTP POST: got the hashed-decoded response (decode time: %.3fs, total request: %.3fs)' % (self, time.time()- t1, time.time() - t0))
            async.async_return(json_cache[response.body])

        try:
            json_out = json.loads(response.body, object_hook=_tuber_json_object_hook)
            json_cache[response.body] = json_out
        except ValueError:
            logger = logging.getLogger(__name__)
            logger.debug('%.32r: Tuber HTTP request returned invalid JSON data "%s"' % (self, response.body))
            raise
        log.info('%r: HTTP POST: got the decoded response (decode time: %.3fs, total request: %.3fs)' % (self, time.time()- t1, time.time() - t0))
        async.async_return(json_out)
        # async.async_return(response.body)


    @async.async
    def ping(self, timeout=0.1):
        """
        Returns a boolean inticating whether a tuber object is available at
        the specified ARM hostname.
        """
        logger = logging.getLogger(__name__)
        # try:
        #     result = yield self._post(dict(object=self.tuber_objname, method='_sleep', args=[0], kwargs={}), timeout=0.1)
        # except HTTPError:
        #     logger.debug('%.32r: ping returned an HTTP error. Board is considered to be absent.' % self)
        #     async.async_return(False)
        # except ValueError:
        #     logger.debug('%.32r: ping returned a valid HTTP reply but bad JSON data "%s". Board is considered to be absent.' % (self, response.body))
        #     async.async_return(False)
        # try:
        #     async.async_return(not result['error'])
        # except KeyError:
        #     logger.debug('%.32r: ping returned valid JSON reply ("%r") but does not have the required fields . Board is considered to be absent.' % (self, body))
        #     async.async_return(False)


        #     , ValueError, KeyError):
        # client = tornado.httpclient.AsyncHTTPClient()
        # request = tornado.httpclient.HTTPRequest(
        #     url=self.tuber_uri,
        #     method='POST',
        #     body='{"object": "%s",'
        #          ' "method": "_sleep",'
        #          ' "args": [0],'
        #          ' "kwargs": {}}' % self.tuber_objname,
        #     connect_timeout=timeout,
        #     request_timeout=timeout)
        logger.info('%r: Pinging %s' % (self, self.tuber_uri))
        try:
            response = yield self._post.async(
                {'object': self.tuber_objname, 'method': '_sleep', 'args': [0], 'kwargs':{}},
                timeout=timeout)
        # response = yield client.fetch(request, raise_error=False)
        except (IOError, tornado.httpclient.HTTPError):
            logger.debug('%.32r: Tuber Ping returned an HTTP error. Board is considered to be absent.' % self)
            async.async_return(False)
        except ValueError:
            logger.debug('%.32r: Tuber Ping returned a HTTP response with invalid JSON data. Board is considered to be absent.' % (self))
            async.async_return(False)
        except Exception as e:
            logger.error('%r: unhandled exception in ping: %s: %r' %(self,type(e), e ))
            async.async_return(False)
        try:
            async.async_return(not response.error)
        except KeyError:
            logger.debug('%.32r: Tuber Ping returned valid JSON reply ("%r") but does not have the required error field. Board is considered to be absent.' % (self, response))
            async.async_return(False)

    def tuber_context(self):
        return Context(self)

    @property
    def tuber_uri(self):
        '''Retrieve the URI associated with this TuberResource.'''
        raise NotImplementedError("Subclass needs to define tuber_uri!")

    @property
    def tuber_objname(self):
        '''Retrieve the Tuber Object associated with this TuberResource.'''
        return self.__class__.__name__

    @property
    def __doc__(self):
        '''Construct DocStrings using metadata from the underlying resource.'''

        (meta, _, _) = self._tuber_get_meta()
        return "%s:\t%s\n\n%s" % (
            meta.name,
            meta.summary,
            meta.explanation
        )

    def __dir__(self):
        '''Provide a list of what's here. (Used for tab-completion.)'''

        (meta, _, _) = self._tuber_get_meta()
        # We need to gather all class attributes from the MRO chain so we
        # don't hide other superclasses
        class_attributes = [item for class_ in type(self).mro()
                            for item in class_.__dict__.keys()]
        instance_attributes = self.__dict__.keys()
        return list(set(class_attributes +
                        instance_attributes +
                        meta.properties +
                        meta.methods)
                    )

    @async.async
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
        if hasattr(self, '_tuber_meta'):
            async.async_return((
                self._tuber_meta,
                self._tuber_meta_properties,
                self._tuber_meta_methods))

        log = logging.getLogger(__name__)
        #log.info('%r: Getting Tuber metadata' % (self))
        t0 = time.time()


        yield async.async_moment
        self._tuber_meta_properties = {}
        self._tuber_meta_methods = {}
        self._tuber_meta = _tuber_json_object_hook({"properties": [], "methods": []})

        if not self.tuber_uri:
            log.warning("%r: Invalid tuber URI." % (self))
            async.async_return((
                self._tuber_meta,
                self._tuber_meta_properties,
                self._tuber_meta_methods))

        # json_in = json.dumps({'object': self.tuber_objname})
        # json_out = json.loads(
        #     urllib2.urlopen(self.tuber_uri, json_in).read(),
        #     object_hook=_tuber_json_object_hook)

        #log.info('%r: getting list of properties and methods' % (self))

        json_out = yield self._post.async({'object': self.tuber_objname})
        t1 = time.time()
        meta = json_out.result

        if not meta:
            log.warning("%r: Tuber returned an empty metadata list for object '%s'." % (self, self.tuber_objname))
            async.async_return((
                self._tuber_meta,
                self._tuber_meta_properties,
                self._tuber_meta_methods))


        # Retrieve all properties
        # json_in = json.dumps([{
        #     'object': self.tuber_objname,
        #     'property': p} for p in meta.properties])
        # json_out = json.loads(
        #     urllib2.urlopen(self.tuber_uri, json_in).read(),
        #     object_hook=_tuber_json_object_hook
        # )
        #log.info('%r: getting properties and method info' % (self))
        meta_properties=meta.properties; meta_methods=meta.methods
        json_out = yield self._post.async(
            [{'object': self.tuber_objname,'property': p}
             for p in (meta_properties + meta_methods)])
        t2 = time.time()
        #log.info('%r: got properties and method info after %f seconds' %(self, time.time() - t1))
        #for j in json_out:
        #    print(j.result)
        props = {p:r.result for p, r in zip(meta_properties, json_out[:len(meta_properties)])}
        # for p, r in zip(meta.properties, json_out):
        #     props[p] = r.result

        # Retrieve all methods
        # json_in = json.dumps([{
        #     'object': self.tuber_objname,
        #     'property': p} for p in meta.methods])
        # json_out = json.loads(
        #     urllib2.urlopen(self.tuber_uri, json_in).read(),
        #     object_hook=_tuber_json_object_hook)
        # log.info('%r: getting method info' % (self))

        # json_out = yield self._post.async(
        #     [{'object': self.tuber_objname,'property': p} for p in meta.methods])

        methods = {m:r.result for m, r in zip(meta_methods,json_out[len(meta_properties):])}
        # for m, r in zip(meta.methods, json_out):
        #     methods[m] = r.result

        self._tuber_meta_properties = props
        self._tuber_meta_methods = methods
        self._tuber_meta = meta

        t3 = time.time()
        log.info('%r: Retrieved Tuber metadata (%.3fs to get list, %.3fs to get method/prop info, %.3fs total' % (self, t1-t0, t2-t1, t3-t0))

        async.async_return((
            self._tuber_meta,
            self._tuber_meta_properties,
            self._tuber_meta_methods))

    def __getattr__(self, name):
        '''Remote function call magic.

        This function is called to get attributes (e.g. class variables and
        functions) that don't exist on "self". Since we build up a cache of
        descriptors for things we've seen before, we don't need to avoid
        round-trips to the board for metadata in the following code.
        '''

        # Refuse to __getattr__ a couple of special names used elsewhere.
        # These are mostly hints for SQLAlchemy or IPython.
        if name.startswith(('_sa', '_tuber', '_repr', '_ipython')) \
                or name in ('trait_names', '_getAttributeNames',
                            'getdoc', '__wrapped__', '__call__'):
            raise AttributeError()

        # Make sure this request corresponds to something in the underlying
        # TuberObject.
        (meta, metap, metam) = self._tuber_get_meta()
        if name not in meta.methods and name not in meta.properties:
            raise AttributeError(
                "'%s' is not a valid method or property!" % name)

        if name in meta.properties:
            # Fall back on properties.
            setattr(self, name, metap[name])
            return getattr(self, name)

        if name in meta.methods:
            d = metam[name]

            # Generate a callable prototype
            class TuberProto(async.Parallelizable):

                @tornado.gen.coroutine
                def __call_async__(p, obj, *args, **kwargs):
                    with obj.tuber_context() as ctx:
                        f = getattr(ctx, name)(*args, **kwargs)
                        yield async.asynchronously(ctx)
                    raise tornado.gen.Return((yield f))

            p = TuberProto()

            # Add dynamically generated DocStrings
            p.__doc__ = textwrap.dedent('''
                {name}({args_short})

                {args_long}

                {explanation}''').format(
                name=d.name,
                args_short=', '.join([a.name for a in d.args]),
                args_long='\n'.join([
                    "    {:<16} {}".format(
                        arg.name + ":",
                        arg.description
                    ) for arg in d.args]),
                explanation='\n'.join(textwrap.wrap(d.explanation))
            )
            # Associate as a class method.
            setattr(self.__class__, name, p)
            return getattr(self, name)

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
