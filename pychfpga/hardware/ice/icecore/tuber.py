"""
Tuber object interface


JFC: November 2020

Experimental re-haul, deployed on CHIME on Nov 2020 (8 crates/ 128 boards).

This updated module can be seen as a PEP-like proposal for future Tuber standardization.

Changes:
    - Use next_asyncio to allow  loop.run_until_complete() to  operate in already existing loops. Now we can truly run an async function even if it was called from a sync one.
    - tie ClientSessions to the current loop, and make sure we use the right one. In an interactive context, we often use multiple loops, and running a query in a loop while the session is in another loop just crashes.

    - methods, not just properties are populated as instance attributes (as opposed to class attributes), to ensure we could connect to IceBoards with different firmware.
    - Methods and properties are applied to the instance all at once when tuber_get_meta is called. This is a small delay compared to the metadata access time. Having a gradually-populated instance makes the timing of the instance less predictable
    - __dir__ is now trivial. Does not try to rebuild the attribute list, which is hard to do correctly. We just ensure we call tuber_get_meta is the instnces  attributes have not been set
    - __getattr__ is also now trivial. Also just ensures _tuber_get_meta() is called as needed. Added infinite recursion protections to protect against subclass errors.
    - Moved Tuber towards an fundamentally async module, with sync support for compatibility, instead of the way around.
    - the instance is populated with async tuber methods. prefix and suffix can be added to the names. (e.g. Suffix _async is useful and recommended for mixed sync/async code)
    - instance can be populated with additional wrapped methods that can be called synchronously
    - almost completely eliminated usage of tworoutines. We created small run_async() and sync_wrap() functions to support running async code in a sync function. This allows explicit use of async functions. Eliminates the non-intuitive tworoutine syntax.
    - All web operations now live in TuberObject, not spread between COntext and TuberObject.
    - Context is an awaitable, i.e. we can get a list of result for each previous calls with ``results = await ctx``.
    - This being said, we don't use Context anymore. TuberObject is self-contained. Context makes use of TuberObject. TuberObject is a specialization that CHIME does not use.
    - Likewise, TuberCategory is another specialization not used by CHIME. It has not been updated to use the new TuberObject.

    - removed simplejson: slower than json in Python 3. We use  ujson for encoding, which is even faster.
    - We added a json cache. Not useful for small requests, so it is not used
      by default. For large requests with lots of data (i.e. sending 12 MBytes
      bitstreams in base64), the memory usage for large arrays is N*4/3
      bitstream size, where N is the size of the array. For CHIME (128 boards) , this means
      that 2 Gbytes of memory is used just for the bitstreams.
    - The json cache is enable by a context manager, which sets as contextvars.ContextVar() that is seen only by the current and downstream awaits, and not by other instance calls that may occur while the current call is in progress.
    - See https://artem.krylysov.com/blog/2015/09/29/benchmark-python-json-libraries/

    - better atexit cleanup
    - patch current interpreter loop to allow ipython tab completion. Need to do this better. Why is the on-the-fly patch not working?
    - PEP-0257 recommends triple-double-quotes for docstrings

    icecore changes:

        - Separted base hardware classes from SQLAlchemy/Hardware map objects. CHIME uses static maps that do not require database functionalities. System configuration is handled and stored centrally in databases at the experiment level.
        - pychfpga implement hardware maps as python lists; SQUAlchemy is no
          longer used. List comprehensions make simpler queries in our
          application.
        - chFPGA application-specific functions do not rely on
          the ARM; it is memory-mapped and dumb, so there is a lot of smarts
          and state in the Python objects.  We eliminated the complex
          handler work-around that was needed to prevent SQLAlchemy from
          randomly and continuously corrupting these stateful objects.
        - Moved all additional features added in my version of icecore back into my application.

      2020-11-04 JFC: Proposed changes, to be discussed, and todo:

    - Make tuber_objname an attribute
    - use ujson for decoding, not just encoding. drop json.
    - Add timeouts
    - I could provide my own caching ujson-based encoder/decoder objects if we don't want caching as a base feature. Shuld be added as class attributes overriden by subclasses.
    - Need to check  Context with legacy apps. TuberCategory needs to be debugged or removed if not a core feature.

      2026-11 JFC: Tuber is now async-only.

    - Removed tworoutine, nest_asyncio, run_async() and sync_wrap(). Callers await the
      ``tuber_*_async`` methods. Sync programs wrap their whole body in one ``asyncio.run()``.
    - Removed TuberCategory (only used by unused/ ORM code) and the sync Context protocol.
    - ``__getattr__``/``__dir__``/``__doc__`` no longer fetch metadata synchronously. Await
      ``_tuber_get_meta_async()`` first (IceBoard.open_platform_async does this).
    - Each loop's ClientSession is closed when that loop is closed.

"""
# Standard library packages
import asyncio
import atexit
import textwrap
import contextvars
import logging

# PyPi packages
import aiohttp
import json
import ujson


__all__ = [
    "TuberError", "TuberRemoteError",
    "TuberObject",
]


class TuberError(Exception):
    pass


class TuberStateError(TuberError):
    pass


class TuberNetworkError(TuberError):
    pass

class TuberRemoteError(TuberError):
    pass


class TuberResult:
    def __init__(self, d):
        self.__dict__.update(d)

    def __iter__(self):
        return iter(self.__dict__.values())

    def __repr__(self):
        """Return a nicely formatted representation string"""
        return f"TuberResult({','.join((f'{name}={val!r}' for (name, val) in self.__dict__.items()))})"


def valid_dynamic_attr(name):
    """
    Return True if the input attribute name can be assigned to a dynamically
    created attribute (i.e. an attribute that would be returned by a call to
    `_tuber_get_meta_async()`), False otherwise.  See documentation of the
    `TuberObject.__getattr__` method for a discussion of why this function
    is necessary.
    """
    # These are mostly hints for SQLAlchemy or IPython.

    if name.startswith((
            '__', '_sa', '_tuber_meta', '_repr',
            '_ipython', '_orm', '_calls',
    )):
        return False

    if name in {'trait_names', '_getAttributeNames', 'getdoc', '_tuber_get_meta'}:
        return False

    return True


class Context():
    """A context container for TuberCalls. Permits calls to be aggregated.

    Using this interface, you can write code like:

        #>>> from pydfmux import Dfmux, macro, asynchronously, Return

        #>>> @pydfmux.macro(Dfmux)
        #... def my_macro(d):
        #...     with d.tuber_context() as ctx:
        #...         p = ctx.get_mezzanine_power(2)
        #...         f = yield ctx.get_frequency(ctx.UNITS.HZ, 1, 1, 1)
        #...         ctx.set_frequency(f+1, ctx.UNITS.HZ, 1, 1, 1)
        #...         yield asynchronously(ctx)
        #...     raise Return((yield p))

        #>>> my_macro(d)
        #True

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

        #>>> with d.tuber_context() as ctx:
        #...     p = ctx.get_mezzanine_power(3)

    the function call is executed, and generates an exception. The exception is
    embedded in 'p' and will not be raised unless you call 'p.result()'!

    Adjust the `connect_timeout` and `request_timeout` attributes of the ctx
    object to change the connect and request timeouts. The default value
    (1800 seconds) allows calls to the board to be quite slow.
    """

    def __init__(self, obj, **ctx_kwargs):
        self.calls = []
        self.obj = obj
        self.ctx_kwargs = ctx_kwargs

    # Asynchronous context protocol (use ``async with obj.tuber_context() as ctx``)
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure the context is flushed."""
        await self._execute_calls()

    def __await__(self):
        """ Make the context object awaitable such that ``await ctx`` flushes the call stack"""
        return self._execute_calls().__await__()

    def _add_call(self, **request):

        future = asyncio.Future()

        # Ensure call is made in the correct context
        objname = request.setdefault('object', self.obj.tuber_objname)
        assert objname == self.obj.tuber_objname, \
            f'Got call to {objname} in context for {self.obj.tuber_objname}'

        self.calls.append((request, future))

        return future

    async def _execute_calls(self):
        """Break off a set of calls and return them for execution."""

        calls = []
        futures = []
        while self.calls:
            (c, f) = self.calls.pop(0)

            calls.append(c)
            futures.append(f)

        if not calls:
            return None

        json_out = await self.obj._tuber_request_async(calls)
        # Resolve futures
        results = []
        for (f, r) in zip(futures, json_out):
            if getattr(r, 'error', None):
                f.set_exception(TuberRemoteError(r.error.message))
            else:
                results.append(r.result)
                f.set_result(r.result)

        # Return a list of results
        return results

    def __getattr__(self, name):
        """
        Return a function that, when called,  will add the name and arguments
        passed to is into the list of pending calls.
        """

        # Refuse to __getattr__ a couple of special names used elsewhere.
        if not valid_dynamic_attr(name):
            raise AttributeError(f"'{name}' is not a valid method or property!")

        # Queue methods calls.
        def caller(*args, **kwargs):

            # Add extra arguments where they're provided
            kwargs = kwargs.copy()
            kwargs.update(self.ctx_kwargs)

            # ensure that a new unique future is returned
            # each time this function is called
            future = self._add_call(method=name, args=args, kwargs=kwargs)

            return future

        setattr(self, name, caller)
        return caller


class TuberObject:
    """A base class allowing access to properties and methods provided by a remote IceBoard from an instance of this class.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject, and customize it
    overriding appropriate configuration class properties in addition to
    `tuber_objname` and `tuber_uri`.
    """
    _tuber_log = logging.getLogger(__name__)
    # User defined class attributes. Can be overriden by subclasses.
    # Parametrize the TCP connection limits.
    _tuber_client_connections_total = 0  # total number of simultaneous TCP connections (0 = no limit)
    _tuber_client_connections_per_host = 4  # total number of simultaneous TCP connections per host (None = no limit)
    _tuber_connection_timeout = 1.8
    _tuber_request_timeout = 1.8

    # Customize remote method names
    _tuber_async_method_names = ('tuber_', '_async')
    # tuber_objname: Set as a property


    # Internal class attributes
    # Cache for the methods and properties offered by the ARM processor
    _tuber_meta = {}
    _tuber_meta_properties = {}
    _tuber_meta_methods = {}
    _tuber_meta_method_functions = {}  # pre-processed functions
    _tuber_client_sessions = {}  # One ClientSession per event loop, closed when that loop is closed.
    _tuber_use_json_cache = contextvars.ContextVar('_tuber_use_json_cache', default=False)  # exist only in the context of a specific async call and its own async calls.  Other methods running concurrently on the same instance won't see it.
    _tuber_json_cache = {}

    # Required defaults shadowed by instance attributes
    _tuber_instance_attributes_populated = False


    @property
    def __doc__(self):
        """Construct DocStrings using metadata from the underlying resource, once it is loaded."""
        meta = self._tuber_meta.get(self.tuber_uri)
        if meta is None:
            return type(self).__doc__
        return f"{meta.name}:\t{meta.summary}\n\n{meta.explanation}"

    def __getattr__(self, name):
        """Report a remote attribute that is not available yet.

        Remote properties and methods are added to the instance by
        ``await _tuber_get_meta_async()``. This method is only called for
        attributes that do not exist, so it never fetches anything.

        Raising AttributeError allows Python to continue its attribute
        search in further downstream objects in the MRO.
        """
        if self._tuber_instance_attributes_populated or not valid_dynamic_attr(name):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'. Tuber metadata "
            f"is not loaded yet: await obj._tuber_get_meta_async() first")

    def tuber_context(self):
        """Context in which tuber calls are grouped and deferred until the context is awaited or exited (``async with``)"""
        return Context(self)

    def tuber_use_json_cache(self):
        """Context in which tuber json encodings are cached. Use for large requests only"""
        class JSONCacheContext:
            def __init__(self, obj):
                self.obj = obj
            def __enter__(self):
                self.obj._tuber_use_json_cache.set(True)
            def __exit__(self, exc_type, exc_val, exc_tb):
                self.obj._tuber_use_json_cache.set(False)
        return JSONCacheContext(self)

    @property
    def tuber_uri(self):
        """Retrieve the URI associated with this TuberResource."""
        raise NotImplementedError("Subclass needs to define tuber_uri!")
    # tuber_uri = "http://10.10.10.244/tuber"

    @property
    def tuber_objname(self):
        """Retrieve the Tuber Object associated with this TuberResource."""
        return self.__class__.__name__

    # Configure the max clients to be 4 per TuberObject (IceBoard).

    def _tuber_get_client_session(self):
        """
        Return a client session that was created in the currently running
        ioloop. If none exist, one is created.

        Sessions can be shared between multiple instances of TuberObject.

        The client session objects are tied to the event loop that was current
        when the session client is created. Using it in another loop causes
        problems.
        """
        loop = asyncio.get_running_loop()
        if loop in self._tuber_client_sessions:
            return self._tuber_client_sessions[loop]
        else:
            self._tuber_log.debug(f'Running in new loop {id(loop)}. Creating new session')
            connector = aiohttp.TCPConnector(
                limit=self._tuber_client_connections_total,
                limit_per_host=self._tuber_client_connections_per_host,
                )
            session = aiohttp.ClientSession(
                # json_serialize=simplejson.dumps,
                connector=connector,
                # timeout=aiohttp.ClientTimeout(connect=self._tuber_connection_timeout,
                #                               sock_read=self._tuber_request_timeout)
                )
            self._tuber_client_sessions[loop] = session
            return session

    @classmethod
    def _tuber_close_session(cls, loop):
        """Close the session owned by ``loop``. ``loop`` must be idle (not running, not closed)."""
        session = cls._tuber_client_sessions.pop(loop, None)
        if session is not None and not session.closed:
            loop.run_until_complete(session.close())

    @classmethod
    def _tuber_json_encode(cls, data):
        """ JSONn encoder, with cache support.

        We use ujson here, which is definitively faster than json (which in turn is now faster than simplejson in Python 3)

        Cache is used when context variable ``self._tuber_use_json_cache`` is set to ``True`` in any upstream function in the call / await chain
        """
        if not cls._tuber_use_json_cache.get():
            return ujson.dumps(data, reject_bytes=False)
        else:
            def build_key(x):
                """makes a  hashable key out of x by converting lists and dicts into non-mutable objects"""
                return frozenset((k, build_key(v)) for k, v in x.items()) if isinstance(x, dict) else tuple(build_key(v) for v in x) if isinstance(x, list) else x
            key = build_key(data)
            if key in cls._tuber_json_cache:
                # self._tuber_log.debug(f"Reusing cached JSON encoding.")
                encoded_data = cls._tuber_json_cache[key]
            else:
                encoded_data = cls._tuber_json_cache[key] = ujson.dumps(data, reject_bytes=False)
            return encoded_data

    @classmethod
    def _tuber_json_decode(cls, data):
        """ JSON decoder.

        For small requests, standard json with ``objcet_hook=TuberResult`` is
        faster than ujson followed with recursive dict conversion of the result into
        TuberResults.
        """
        return json.JSONDecoder(object_hook=TuberResult).decode(data)

    async def _tuber_request_async(self, requests):
        """Execute a series of remote requests.

        Parameters:

            requests (list of dict): list of requests, where each request is a dictionary. The list is modified in-place to add the object type.

        Returns:

            Results for each request as a list of `TuberResults` objects, with the attributes:
                .result: result of the command
                .error: present only if there is an error.
        """
        if not requests:
            return []

        # Add and/or check object names
        for r in requests:
            objname = r.setdefault('object', self.tuber_objname)
            assert objname == self.tuber_objname, \
                f'Got call to object {objname} from an instance connected to object {self.tuber_objname}'


        # Create a HTTP request to complete the call. This is a coroutine,
        # so we queue the call and then suspend execution (via 'yield')
        # until it's complete.
        session = self._tuber_get_client_session()
        # print(f'URI={self.tuber_uri}, arg = {calls}')

        try:
            # async with session.post(self.tuber_uri, json=requests) as resp:
            async with session.post(self.tuber_uri, data=self._tuber_json_encode(requests)) as resp:
                results = await resp.json(
                        loads=self._tuber_json_decode,
                        content_type=None)
        except aiohttp.ClientConnectorError as e:
            self._tuber_log.error('Network error')
            raise TuberNetworkError(e)

        return results

    async def _tuber_get_meta_async(self):
        """Retrieve metadata associated with the remote network resource and
        make sure the current class instance is populated with them.

        This data isn't strictly needed to construct "blind" JSON-RPC calls,
        except for user-friendliness:

           * tab-completion requires knowledge of what the board does, and
           * docstrings are useful, but must be retrieved and attached.

        This class retrieves object-wide metadata, which can be used to build
        up properties and values (with tab-completion and docstrings)
        on-the-fly as they're needed.

        """
        self._tuber_log.debug(f'{self!r} Fetching Tuber metadata')
        uri = self.tuber_uri

        # if we don't already have the meta info from the board at this specific URI, fetch it
        if uri not in self._tuber_meta:
            meta = (await self._tuber_request_async([dict()]))[0].result
            prop_list = [r.result for r in await self._tuber_request_async([dict(property=p) for p in meta.properties])]
            meth_list = [r.result for r in await self._tuber_request_async([dict(property=p) for p in meta.methods])]
            props = dict(zip(meta.properties, prop_list))
            methods = dict(zip(meta.methods, meth_list))
            functions = {}
            for name, info in methods.items():
                if self._tuber_async_method_names:
                    new_name = self._tuber_expand_name(name, *self._tuber_async_method_names)
                    fn = self._get_async_method(name, info)
                    functions[new_name] = fn
            self._tuber_meta_properties[uri] = props
            self._tuber_meta_methods[uri] = methods
            self._tuber_meta[uri] = meta
            self._tuber_meta_method_functions[uri] = functions

        # Instrument the current instance with the properties and methods if required
        if not self._tuber_instance_attributes_populated:
            # Add properties to class instance
            for name, value in self._tuber_meta_properties[uri].items():
                setattr(self, name, value)
            # Add methods to class instance
            for name, fn in self._tuber_meta_method_functions[uri].items():
                setattr(self, name, fn.__get__(self))
            self._tuber_instance_attributes_populated = True

        return (self._tuber_meta[uri],
                self._tuber_meta_properties[uri],
                self._tuber_meta_methods[uri])


    @staticmethod
    def _tuber_expand_name(name, prefix='', suffix=''):
        if name.startswith('_'):
            return f'_{prefix}{name[1:]}{suffix}'
        else:
            return f'{prefix}{name}{suffix}'

    @staticmethod
    def _get_async_method(name, method_info):
        """
        """
        async def invoke(self, *args, **kwargs):
            (result, ) = await self._tuber_request_async([dict(method=name, args=args, kwargs=kwargs)])
            if getattr(result, 'error', None):
                raise TuberRemoteError(result.error.message)
            return result.result

        args_short = ', '.join(a.name for a in method_info.args)
        args_long = '\n'.join(f"    {arg.name + ':':<16} {arg.description}"
                              for arg in method_info.args)
        explanation =' \n'.join(textwrap.wrap(method_info.explanation))
        invoke.__doc__ = textwrap.dedent(
            f"""
            {name}({args_short})

            {args_long}

            {explanation}"""
            )
        return invoke

_loop_close = asyncio.BaseEventLoop.close


def _close_loop_and_tuber_session(loop):
    """``loop.close()`` that first closes the Tuber session created in that loop.

    asyncio.run(), pytest-asyncio and other loop owners call close() once the
    loop is finished, so each session lives exactly as long as its loop.
    """
    if not loop.is_running() and not loop.is_closed():
        TuberObject._tuber_close_session(loop)
    _loop_close(loop)


asyncio.BaseEventLoop.close = _close_loop_and_tuber_session


@atexit.register
def _close_idle_tuber_sessions():
    """Close sessions of loops still open at exit (e.g. a Jupyter kernel loop)."""
    for loop in list(TuberObject._tuber_client_sessions):
        if not loop.is_running() and not loop.is_closed():
            TuberObject._tuber_close_session(loop)


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
