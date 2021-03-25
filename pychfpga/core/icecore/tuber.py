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



"""
# Standard library packages
import asyncio
import atexit
import textwrap
import contextvars


# PyPi packages
import aiohttp
import nest_asyncio
import json
import ujson

# local packages
from . import tworoutine



__all__ = [
    "TuberError", "TuberRemoteError",
    "TuberCategory", "TuberObject",
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
        return 'TuberResult({0})'.format(
            ','.join('{0}={1!r}'.format(name, val)
                     for name, val in self.__dict__.items()))


def valid_dynamic_attr(name):
    """
    Return True if the input attribute name can be assigned to a dynamically
    created attribute (i.e. an attribute that would be returned by a call to
    `_tuber_get_meta()`), False otherwise.  See documentation of the
    `TuberObject.__getattr__` method for a discussion of why this function
    is necessary.
    """
    # These are mostly hints for SQLAlchemy or IPython.

    if name.startswith((
            '__', '_sa', '_tuber_meta', '_repr',
            '_ipython', '_orm', '_tworoutine__', '_calls',
    )):
        return False

    if name in {'trait_names', '_getAttributeNames', 'getdoc', '_tuber_get_meta'}:
        return False

    return True


def run_async(awaitable):
    """
    Run the current loop until the awaitable is completed.

    Is used to run a coroutine (async function) from a sync function, while
    allowing all already-scheduled tasks to continue to run in the background.

    The coroutine is run in the current event loop, which is retrieved (or
    created) with get_event_loop(). Native asyncio does *NOT* allow to call
    ``run_until_complete`` when a loop is already running. We use `nest_asyncio`
    to patch asyncio in order to allow this.
    """
    # loop = asyncio.get_event_loop()  # get the current loop, running or not
    # nest_asyncio.apply(loop)  # make sure we can run in an already running loop
    # loop.set_debug(True)
    # print(f'Runnings async function {awaitable} in loop {loop} ID={id(loop)}')
    try:
        nest_asyncio.apply()  # make sure we can run in an already running loop
        return asyncio.run(awaitable)
        # return loop.run_until_complete(awaitable)
    except Exception as e:
        print(f'run_async got an excepttion: {e}')
        raise
def sync_wrap(fn):
    """
    Wraps an async coroutine function into a function that can be run synchronously (without the await statement).

    This assumes there is a event loop.
    """
    def sync_fn(*args, **kwargs):
        return run_async(fn(*args, **kwargs))
    return sync_fn

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

    # Synchronous context protocol
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        run_async(self._execute_calls())

    # Asynchronous context protocol
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
            raise AttributeError("'%s' is not a valid method or property!" % name)

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


class TuberCategory:
    """Pull Tuber functions into ORM objects based on categories.

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
    """

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
            """This is a fall-through replacement for __getattr__.

            We assume we're capturing a function call that's missing
            arguments. We fill in these arguments and dispatch the call.

            See `TuberObject.__getattr__` for details.
            """

            # Refuse to __getattr__ a couple of special names used elsewhere.
            if not valid_dynamic_attr(name):
                raise AttributeError("'%s' is not a valid method or property!" % name)

            parent = decorator.getobject(self)

            # Raise an Attribute error if the parent board isn't set
            if parent is None:
                raise AttributeError("'%s' is not a valid method or property!" % name)
            m = getattr(parent, name)

            if isinstance(m, tworoutine.tworoutine):

                @tworoutine.tworoutine
                async def acall(*args, **kwargs):
                    mapped_args = {
                        n: f(self)
                        for (n, f) in decorator.arg_mappers.items()
                    }

                    return await (~m)(*args, **kwargs, **mapped_args)

                return acall

            raise AttributeError("'%s' is not a valid method or property!" % name)

        cls.__getattr__ = __getattr__

        def __dir__(self):
            """Retrieve a list of class properties/methods that are relevant.

            We try to grab the original list of attributes from the ORM,
            and then augment it with any Tuber functions in our category.

            See `TuberObject.__dir__` for more details.
            """

            d = set(dir(self.__class__))
            d.update(dir(super(self.__class__, self)))

            o = decorator.getobject(self)
            # Return just the static attributes if the board object isn't set
            # This can occur during construction of the ORM.
            if o is None:
                return sorted(d)
            (meta, metap, metam) = o._tuber_get_meta()

            for m in meta.methods:
                if hasattr(metam[m], 'categories') and \
                        decorator.category in metam[m].categories:
                    d.add(m)
            return sorted(d)

        cls.__dir__ = __dir__

        return cls


class TuberObject:
    """A base class allowing access to properties and methods provided by a remote IceBoard from an instance of this class.

    This is a great way of using Python to correspond with network resources
    over a HTTP tunnel. It hides most of the gory details and makes your
    networked resource look and behave like a local Python object.

    To use it, you should subclass this TuberObject, and customize it
    overriding appropriate configuration class properties in addition to
    `tuber_objname` and `tuber_uri`.
    """

    # User defined class attributes. Can be overriden by subclasses.
    # Parametrize the TCP connection limits.
    _tuber_client_connections_total = 0  # total number of simultaneous TCP connections (0 = no limit)
    _tuber_client_connections_per_host = 4  # total number of simultaneous TCP connections per host (None = no limit)
    _tuber_connection_timeout = 1.8
    _tuber_request_timeout = 1.8

    # Customize remote method names
    _tuber_wrapped_method_names = None
    _tuber_async_method_names = ('tuber_', '_async')
    # tuber_objname: Set as a property


    # Internal class attributes
    # Cache for the methods and properties offered by the ARM processor
    _tuber_meta = {}
    _tuber_meta_properties = {}
    _tuber_meta_methods = {}
    _tuber_meta_method_functions = {}  # pre-processed functions
    _tuber_client_sessions = {} # Client session objects in use by each event loop.
    _tuber_use_json_cache = contextvars.ContextVar('_tuber_use_json_cache', default=False)  # exist only in the context of a specific async call and its own async calls.  Other methods running concurrently on the same instance won't see it.
    _tuber_json_cache = {}

    # Required defaults shadowed by instance attributes
    _tuber_getattr_in_progress = False
    _tuber_instance_attributes_populated = False


    @property
    def __doc__(self):
        """Construct DocStrings using metadata from the underlying resource."""
        (meta, _, _) = self._tuber_get_meta()
        return f"{meta.name}:\t{meta.summary}\n\n{meta.explanation}"

    def __dir__(self):
        """Ensures that the remote properties and methods are loaded in this
        instance before returning the the list of the instance attributes.
        Used for tab-completion.
        """
        if self.tuber_uri not in self._tuber_meta:
            self._tuber_get_meta()
        return super().__dir__()

    def __getattr__(self, name):
        """Look up a yet-unknown attribute after ensuring that the instance is
        populated with the remote properties and methods.

        We set ``self._tuber_getattr_in_progress`` while the `__getattr__` is in progress to be extra sure we
        avoid infinite recursion in case some attribute access fails during this process.

        This method is not called if the attribute exist, so there is no performance impact once the tuber objects are populated.

        To minimize the performance impact when the parent class is checking for an unknown attribute,
        we raise an AttributeError immediately if the attributes are already populated by checking
        if ``self._tuber_instance_attributes_populated`` is True, which is quick.

        Note that raising AttributeError allows Python to continue its attribute
        search in further downstream objects in the MRO.
        """
        try:
            if self._tuber_instance_attributes_populated or self._tuber_getattr_in_progress:
                raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
            else:
                self._tuber_getattr_in_progress = True
                self._tuber_get_meta()
                return getattr(self, name) # this __getattr__ won't be called again if the attribute has been added.
        finally:
            self._tuber_getattr_in_progress = False

    def tuber_context(self):
        """Context in which tuber calls are grouped and deferred until the context is awaited or exited"""
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
        loop = asyncio.get_event_loop()
        if loop in self._tuber_client_sessions:
            return self._tuber_client_sessions[loop]
        else:
            print(f'Running in new loop {id(loop)}. Creating new session')
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
    async def _tuber_close_client_sessions_async(cls):
        """
        Close all client sessions
        """
        await asyncio.gather(*[c.close() for c in cls._tuber_client_sessions.values()])

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
                print(f"Reusing cached JSON encoding.")
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
            print('Network error')
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
        print(f'{self!r} Fetching Tuber metadata')
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
                if self._tuber_wrapped_method_names:
                    new_name = self._tuber_expand_name(name, *self._tuber_wrapped_method_names)
                    wrapped_fn = sync_wrap(self._get_async_method(name, info))
                    functions[name] = wrapped_fn
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

    _tuber_get_meta = sync_wrap(_tuber_get_meta_async)

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

atexit.register(sync_wrap(TuberObject._tuber_close_client_sessions_async))



# Enable nested run_until_complete() on current ipython loop. If we don't do
# this now, ipython, enabling this later causes ipython to crash when we
# tab-complete. Maybe it's because the event loops that is created when
# requesting tab completion is not re-entrant and there are already tasks on
# on this loop when we call our first run_async(), which then patches the
# event loop to be reentrant.
print(f'Enabling nested event loop on current loop ID {id(asyncio.get_event_loop())}')
nest_asyncio.apply()
# # vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
