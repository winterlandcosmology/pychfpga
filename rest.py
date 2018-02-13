"""
Base REST Clients and Servers classes for building REST-based applications.
"""
from __future__ import print_function

import logging
import signal
import traceback
import inspect
import requests
import functools
import socket
import argparse
import yaml
import re

import log
from log import NameSpace
from pychfpga import load_yaml_config

import tornado.ioloop
import tornado.web
from tornado.gen import sleep
from tornado.gen import moment
from tornado.ioloop import IOLoop
from tornado.web import HTTPError
#from tornado_profile import TornadoProfiler

def coroutine(func, replace_callback=True):
    """ Standard Tornado coroutine decorator, with the coroutine flag added in case we use tornado < 4.5"""
    wrapped = tornado.gen.coroutine(func, replace_callback=replace_callback)
    if not hasattr(wrapped, '__tornado_coroutine__'):
        wrapped.__tornado_coroutine__ = True
    return wrapped


def is_coroutine_function(func):
    """Return whether *func* is a coroutine function, i.e. a function
    wrapped with `~.gen.coroutine`.

    .. versionadded:: 4.5
    """
    return getattr(func, '__tornado_coroutine__', False)

def coroutine_return(*args, **kwargs):
    """ return a value from a coroutine.

    Is used to return values from a co-routine because a
    coroutine is a generator (a function that *yields* values) and you cannot use return in a
    generator in python 2.7.

    Just like the return statement, `coroutine_return` normally accept a single positional argument.
    However, for convenience, if we pass it only keyword arguments, these arguments will be returned
    as a dict object. This is useful because REST methods return dicts.

    Parameters: args, kwargs: All the positional and keywords arguments to return. Either one
        positional argument or only keyword arguments are accepted.

    Returns:
        Nothing
    """
    if kwargs and not args:  # if we only have keyword arguments
        raise tornado.gen.Return(kwargs)  # return keyword arguments as a dict
    else:
        raise tornado.gen.Return(*args, **kwargs)  # just pass everything

class RESTClient(object):
    """ This is a Requests-based client (non asynchronous)
    """
    TIMEOUT = 60 # seconds
    DEFAULT_HOST = 'localhost'
    DEFAULT_PORT = 54321

    def __init__(self, hostname=DEFAULT_HOST, port=DEFAULT_PORT):
        self.hostname = hostname
        self.port = port
        self.log = logging.getLogger(__name__).getChild(self.__class__.__name__)

    def __repr__(self):
        return '%s(%s:%s)' % (self.__class__.__name__, self.hostname, self.port)

    def print(self, msg):
        print(msg)

    def print_error(self, msg):
        print(msg)

    def error(self, msg):
        self.print_error(msg)
        raise

    def url(self, endpoint):
        return 'http://%s:%d/%s' % (self.hostname, self.port, endpoint)

    def get(self, endpoint):
        try:
            return requests.get(self.url(endpoint), timeout=self.TIMEOUT).json()
        except requests.exceptions.ConnectionError:
            self.error("Can't connect to REST server at %s:%d for GET request" % (self.hostname, self.port))

    def post(self, endpoint, **kvs):
        try:
            return requests.post(self.url(endpoint), json=kvs, timeout=self.TIMEOUT).json()
        except requests.exceptions.ConnectionError:
            self.error("Can't connect to REST server at %s:%d for PORT request" % (self.hostname, self.port))

class AsyncMixin(object):
    """ Mixin class that provides common methods useful to asynchrohous servers or clients

    Includes:
        - register a periodic heartbeat
        - handle keyboard interrupt
        - handle shutdown
        - initiate periodic or delayed calls
        - start the ioloop
        - run a coroutine synchronously

    """

    def add_periodic_callback(self, callback, period):
        return tornado.ioloop.PeriodicCallback(callback, period).start()

    def call_later(self, delay, callback):
        """ Calls a callback function after a delay """
        IOLoop.current().call_later(delay, callback)

    def add_heartbeat(self, heartbeat_string='.', period=1000):
        """ Add a periodic callback that prints the specified string at specified inetrvals.

        Parameters:
            heartbeat_string (str): string to print
            period (float): interval between prints in milliseconds. Default is 1000 ms.
        """
        def heartbeat_callback():
            # print(heartbeat_string, end='')
            # sys.stdout.flush()
            self.log.info('%r: *** Alive ***' % self)
        self.add_periodic_callback(heartbeat_callback, period)

    def add_shutdown_handler(self):
        @coroutine
        def shutdown():
            print("Received SHUTDOWN signal")
            yield self.shutdown()
            IOLoop.current().stop()

            # A bit of a hack: close all sockets opened by the HTTP server so we can restart a new
            # server in the same iPython session
            for sock in self.http_server._sockets.values():
                #print('Closing socket ', sock.getsockname())
                sock.close()

        def handler(sig, frame):
            IOLoop.current().add_callback_from_signal(shutdown)
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    @coroutine
    def shutdown(self):
        pass

    # def __getattr__(self, name):
    #     if name.startswith('sync_'):
    #         return functools.partial(self.run_sync, name[5:])
    # def __dir__(self):
    #     attrs = dir(type(self)) + vars(self).keys()
    #     for name, method in vars(type(self)).items():
    #         if callable(method) and not name.startswith('_'):
    #             attrs.append('sync_' + name)
    #     return attrs

    def run_sync(self, method_name, *args, **kwargs):
        """ Runs `method_name` in the currenta ioloop and returns when completed"""
        return IOLoop.current().run_sync(functools.partial(getattr(self, method_name), *args, **kwargs))


    def run(self):
        """ Start the current ioloop and run it until something makes it stop """
        try:
            IOLoop.current().start()
        finally:
            IOLoop.current().stop() # make sure the loop is stopped in case the code was interrupted

class AsyncRESTClient(AsyncMixin):
    """Implements asynchronous methods (coroutines) to operate a remote REST server.

    Is implemented using a Tornado AsyncHTTPClient . All methods are Tornado coroutines so that
    operations can be performed concurrently on multiple nodes.
    """
    DEFAULT_HOST = 'localhost'
    DEFAULT_PORT = 80

    def __init__(self, hostname=DEFAULT_HOST, port=DEFAULT_PORT, server_class=None, heartbeat_string=None, heartbeat_period=5000):
        self.log = log.get_logger(self)
        self.hostname = hostname
        self.port = port

        if server_class and (not hostname or not self._tcp_ping(hostname, port)):
            self.log.info('%32r: Hostname is not specified or is not responding. Creating local server' % (self))
            self.hostname = 'localhost'
            address = ''  # server listens to all interfaces by default
            self.server = server_class(address, self.port)

        self.log.info('%32r: Creating %s at %s:%i' % (self, self.__class__.__name__, self.hostname, port))
        self.client = tornado.httpclient.AsyncHTTPClient()
        if heartbeat_string:
            self.add_heartbeat(heartbeat_string, heartbeat_period)
        self.add_shutdown_handler()

    def __repr__(self):
        return '%s(%s:%s)' % ( self.__class__.__name__, self.hostname, self.port)

    def url(self, endpoint):
        return 'http://%s:%d/%s' % (self.hostname, self.port, endpoint)

    @coroutine
    def post(self, endpoint, raw=False, **kws):
        """ Send POST request to the target endpoint, with all keywords arguments being JSON-encoded"""
        coroutine_return((yield self._fetch(endpoint, 'POST', raw, **kws)))

    @coroutine
    def get(self, endpoint, raw=False):
        coroutine_return((yield self._fetch(endpoint, 'GET', raw)))

    @coroutine
    def _fetch(self, endpoint, method, raw=False, **kws):
        url = self.url(endpoint)
        if method == 'POST':
            body = tornado.escape.json_encode(kws)
        else:
            body = None
        self.log.debug('fetch: Send %s request %s' % (method, endpoint))
        resp = yield self.client.fetch(url, method=method, headers={"Content-Type": "application/json"}, body=body, raise_error=False, request_timeout=30)
        #print('_fetch response: %r' % resp)
        #print('_fetch response body: %r' % resp.body)
        #print('_fetch response error: %s' % resp.error)
        if raw:
            decoded_reply = resp.body
            error = ''
        else:
            if not resp.body:
                decoded_reply = None
                error = ''
            else:
                try:
                    decoded_reply = tornado.escape.json_decode(resp.body)
                    if isinstance(decoded_reply, dict):
                        error = decoded_reply.pop('error','')
                    else:
                        error = ''
                except (TypeError, ValueError):
                    error = '%.32r: Invalid JSON reply string %r' %(self, resp.body)
        if resp.error:
            #print('*** REST client got response error: %s' % resp.error)
            error = str(resp.error) + '\n' + error
        if error:
            error = ('Response=%r\n'%decoded_reply) + error
            #print('****ERROR****:', error, '\n--------------------')
            raise RuntimeError(error)
        coroutine_return(decoded_reply)

    def _tcp_ping(self, hostname, port, timeout=0.3):
        """
        Establish a TCP connection with `addr` and return a boolean indicating whether the connection was successful.

        Parameters:
            hostname (str): hostname to which a TCP connection is made
            port (int): port to which a TCP connection is made
            timeout (float): Time to wait before giving up on the connection

        Return:
            True if the connection is successful, False otherwise.

        Todo:
            Make this a coroutine
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((hostname, port))
            s.close()
            return True
        except (socket.timeout, socket.error): # Windows raises socket.timeout, linux raises socket.error
            self.log.warn('Could not establish a TCP connection with %s:%s' % (hostname, port))
            return False


class JsonRequestHandler(tornado.web.RequestHandler):
    """RequestHandler than can accept both JSON-encoded and standard HTML POST arguments.

    Also overrides the `write_error` method to set the 'error' result with the traceback when
    exceptions occured.

    The user must add the get() or post() method.
    """

    def prepare(self):
        if not self.request.body: return
        # print('Headers=', str(list(self.request.headers)))
        content_type = self.request.headers['Content-Type']
        if content_type == 'application/json':
            # print('post body=', self.request.body)
            try:
                args = tornado.escape.json_decode(self.request.body)
                self.request.arguments.update(args)
            except ValueError:
                self.send_error(400, error="can't parse JSON")
        elif content_type == 'application/x-www-form-urlencoded':
            # print('post body=', self.request.body, 'pre args=', self.request.arguments )
            args = { k:v[-1] for k,v in self.request.arguments.items() }
            self.request.arguments.update(args)

    def set_default_headers(self):
        self.set_header('Content-Type', 'application/json')
        
    @coroutine
    def head(self, *args, **kwargs):
        if kwargs:
            yield self.post(*args, **kwargs)
        else:
            yield self.get(*args)

    def write_error(self, status_code, **kvs):

        log = logging.getLogger(__name__)
        if 'exc_info' in kvs:
            exc_info = kvs.pop('exc_info')
            kvs['error'] = self.format_exception(exc_info, remove_tornado=False)
        log.error('%r: Request Handler caught an exception. HTTP code is %s. Error is %s' % (self, status_code, kvs.get('error', '[No Error]')))
        print('Request Handler exception \n%s' %  kvs.get('error', '[No Error]'))
        self.set_status(200, reason='There was an exception') # Prevent the client from raising an HTTP error. The client will recognize errors by looking at the error field.
        #print('*** REST Server: Adding error field:', kvs['error'], '\n------')
        #self.write('**whoah! an exception***\n')
        self.write(kvs)  # kvs is a dict, so it will be json-encoded
        self.finish()

    def format_exception(self, exc_info, remove_tornado=True):
        """ Format traceback string by indenting them and removing the tornado internals"""
        lines = traceback.format_exception(*exc_info)
        filtered_lines = []
        i = 0
        while i < len(lines):
            if remove_tornado and re.match(r'\s*File ".*/tornado/.*", line ', lines[i]):
                i+=1
            else:
                for line in lines[i].split('\n'):
                    if line:
                        filtered_lines.append('   >  %s\n ' % line)
                i += 1
        return ''.join(filtered_lines)


class AsyncRESTServer(AsyncMixin):
    """
    Creates a Tornado Web application that will call the endpoint handlers registered with the RESTserver.endpoint decorator.

    GET endpoint methods are coroutines and are tagged with the @endpoint decorator:

        @coroutine
        @endpoint
        def my_GET_endpoint_method(self, handler):
            ...

    POST endpoints are similarly defines::

        @coroutine
        @endpoint
        def my_POST_endpoint_method(self, handler, args):
            ...

    Methods with 2 arguments (i.e method(self, handler)) will only answer to GET requests.

    Methods with 3 arguments (i.e method(self, handler, some_arg_name)) will answer to POST
    requests, and the posted arguments will be passed to `some_arg_name`.

    The method can access to the AsyncRESTServer instance ('self') to obtain context information.


    `handler` is the RequestHandler that is handling the current request and can be used for more sophisticated
    processing or error handling.

    Endpoint methods must be Tornado co-routines, and should therefore `yield` when doing lengthy
    IO-bound operations and shall use `coroutine_return` to return values (do not use the 'return' statement).

    If the endpoint handler is successful, its return value is sent to the client.

    If an uncatched exception has occured, a dictionary containing the 'error' key set with the error information (and traceback) is sent back.

    User can signal an error condition by raising an exception or by returning a dictionary with the 'error' key.
    """

    DEFAULT_PORT = 80

    def __init__(self, address='', port=DEFAULT_PORT, heartbeat_string=None, heartbeat_period=5000):
        """ Create a Web server responding to the endpoints defined in the class.

        Parameters:

            address (str): address of the  interface on which the server will respond to
                requests. If left empty, the server will respond to all interfaces. If a hostname is
                given (as opposed to a IP address), all IP addresses associated with this address will
                be used.

            port (int): Port number to which the server will listen to requests. Defaults to port 80.

            heartbeat_string (str): String to print periodically on stdout. If none, the periodic
                hearbeat process is not run.

            heartbeat_period (int): period between heartbeat prints in ms

        """
        self.address = address
        self.port = port

        self.log = log.get_logger(self)
        self.log.info('%32r: Creating %s server at %s:%s' % (self, self.__class__.__name__, address or '*', port))

        # Create the endpoints registered with the @endpoint decorator
        endpoints = [self._create_endpoint(*info) for info in self.get_endpoint_info()]
        self.app = tornado.web.Application(endpoints) # Create the Web application serving those endpoints
        self.http_server = self.app.listen(self.port, address=address or '') # Create the web server on the target port in the current ioloop.
        if heartbeat_string:
            self.add_heartbeat(heartbeat_string, heartbeat_period)
        self.add_shutdown_handler()
        # The server will run when the ioloop is started.

    def __repr__(self):
        return '%s(%s:%s)' % ( self.__class__.__name__, self.address, self.port)

    def get_endpoint_info(self):
        """ Return a list of tuples (method_name, endpoint_name, method_args) describing all the
        endpoints supported by the server"""
        info = []
        for method in vars(type(self)).values():
            method_name, endpoint_name, method_args = getattr(method, 'endpoint_info', (None, None, None))
            if method_name:
                info.append((method_name, endpoint_name, method_args))
        return info

    def _create_endpoint(self, method_name, endpoint_name, method_args):
        """ Create an handler tuple (endpoint, handler) for the method `method_name`.

        Parameters:

            method_name (str): Name of the method in this class that handles the endpoint.
                The method must have been decorated @endpoint so it will have been registered.

            endpoint_name (str): Name to be used for the end point on the HTTP requests. Utually the
                same as the method name, with undrscores replaced by dashes.

            method_args (dict): dictionary listing the method parameters. If there are more than two
                (i.e. self and handler), this will be a POST endpoint, otherwise it will be a GET.

        Returns: A endpoint handler tuple (endpoint_name, handler) that will be passed to the
            :meth:`tornado.web.Application()` to answer to that secific handler by calling the
            target method with the passed argument (if a POST request).

        """
        method = getattr(self, method_name)
        has_args = len(method_args) > 2  # any other arguments beyound the mandatory 'self' and 'handler'?
        self.log.debug('%r: Creating a REST %s endpoint %s for method %s(%s)' % (self, ('GET','POST')[has_args], endpoint_name, method_name, ', '.join(method_args)))
        if has_args:
            class Handler(JsonRequestHandler):
                @coroutine
                def post(self):
                    result = yield method(self, **self.request.arguments)
                    # print('Sending POST reply:', result)
                    self.write(tornado.escape.json_encode(result)) # arg must be a string or a dict that will be json-encoded

        else:
            class Handler(JsonRequestHandler):
                @coroutine
                def get(self):
                    result = yield method(self)
                    if result is not None:
                        self.write(tornado.escape.json_encode(result))
        return tornado.web.url(r'/%s' % endpoint_name, Handler)


    @classmethod
    def endpoint(cls, arg=None):
        """ Decorator that tags the target method as a REST endpoint handler.

        It does that by adding an endpoint_info attribute to the method.

        Can be used either as a argument-less or argumented decorator::

            @endpoint  # uses endpoint name derived from method name
            def my_func(...)

            @endpoint() # uses endpoint name derived from method name
            def my_func(...)

            @endpoint('my_endpoint_name') # uses specified endpoint name
            def my_func(...)

            @endpoint(endpoint_name='my_endpoint_name') # uses specified endpoint name
            def my_func(...)

        """

        def decorator(fn, endpoint_name):
            method_name = fn.__name__
            if endpoint_name is None:
                endpoint_name_ = method_name.replace('_', '-')
            else:
                endpoint_name_ = endpoint_name
            argspecs = inspect.getargspec(fn)
            method_args = argspecs.args + (['**' + argspecs.keywords] if argspecs.keywords else [])
            if len(method_args) < 2:
                raise RuntimeError("Method %s.%s first two parameters must be 'self' and 'handler'" % (cls.__name__, method_name))
            fn.endpoint_info = (method_name, endpoint_name_, method_args)  # add the endpoint info in the function
            return fn # return the original function

        if isinstance(arg, str) or arg is None:  # if we use the decorator without arguments, i.e. @endpoint
            return functools.partial(decorator, endpoint_name=arg)
        else:
            return decorator(arg, endpoint_name=None)

class RunSyncWrapper(object):
    """
    Wraps an instance of object providing asynchronous (coroutine) methods, and automatically run calls to such coroutines in an ioloop until they are completed.

    This allows the asynchronous methods of a class to be as if they were synchronous.

    Parameters:
        async_instance (object): object that contains coroutine methods.
    """
    def __init__(self, async_instance):
        self._async = async_instance

    def __getattr__(self, name):
        obj = getattr(self._async, name)
        if hasattr(obj, '__tornado_coroutine__'):
            return functools.partial(self.run_sync, obj)
        else:
            return obj

    def __dir__(self):
        attrs = dir(type(self)) + vars(self).keys() + dir(self._async)
        return attrs

    def run_sync(self, method, *args, **kwargs):
        """ Runs `method` in a ioloop and returns when completed"""
        return IOLoop.current().run_sync(functools.partial(method, *args, **kwargs))

    def run(self):
        try:
            IOLoop.current().start()
        finally:
            IOLoop.current().stop() # make sure the loop is stopped in case the code was interrupted


endpoint = AsyncRESTServer.endpoint #: Shortcut to :meth:`AsyncRESTServer.endpoint`

class SocketContext(object):
    """
    Provides methods to create a re-entrant context object in which a unique socket is available
    within the context and is closed when the outer context is exited.

    The `socket_references` keeps track of the context depth such that the socket is closed only
    when the counter is decremented back to zero.

    """
    def __init__(self,  hostname, port, timeout=0.5, flush_timeout=0.1, close_socket=True,  **kwargs):
        self.log = log.get_logger(self)
        self.log.debug('Initializing direct LAN Connection at %s:%i' % (hostname, port))
        self.ip_addr = hostname
        self.ip_port = port
        self.timeout = timeout
        self.close_socket = close_socket
        self.flush_timeout = flush_timeout
        self.temp_flush_timeout = flush_timeout
        self.sock = None
        self.socket_references = 0
        super(SocketContext, self).__init__(**kwargs)

    def __repr__(self):
        return '%s(%s:%s)' % (self.__class__.__name__, self.ip_addr, self.ip_port)

    def socket(self, flush=False, flush_timeout=None):
        """
        Return a context object (`self`) in which a socket to the instrument (`self.sock`) is
        connected and is closed when the context is exited.

        Contexes can be nested at will with negligeable performance penalty. The socket will be
        created and closed only on the outer context entry and exit.

        The power supply handler object acts as a socket context handler, so ``self`` is returned.

        """
        self.flush = flush
        self.temp_flush_timeout = flush_timeout
        return self

    def _connect(self):
            self._close()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            self.sock.settimeout(self.timeout)
            try:
                self.sock.connect((self.ip_addr, self.ip_port))
            except (socket.timeout, socket.error):
                self.log.warning('%r: Error connecting to %s:%i' % (self, self.ip_addr, self.ip_port))
                raise IOError('%r: timout while connecting to %s:%i' % (self, self.ip_addr, self.ip_port))

    def _close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_WR)
                self.sock.close()
            except (socket.error, socket.timeout):
                pass
            self.sock = None
            #print('%r: connecttion to %s:%i closed' % (self, self.ip_addr, self.ip_port))

    def __enter__(self):
        if not self.sock:
           self._connect()
        self.socket_references += 1

        # flush the socket if requested
        if self.flush:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(self.temp_flush_timeout or self.flush_timeout)
            while True:
                try:
                    s = self.sock.recv(16384)
                    #print('flushed %r' % s)
                except socket.timeout:
                    break
            self.sock.settimeout(old_timeout)
        return self.sock

    def __exit__(self, exc_type, exc_value, traceback):
        if self.socket_references:
            self.socket_references -= 1
        if self.close_socket and not self.socket_references:
            self._close()


    def send(self, string):
        trial = 0
        while True:
           try:
              self.sock.send(string)
              return
           except (socket.timeout, socket.error):
              trial += 1
              if trial>3:
                  self.log.warning('%r: error sending to %s:%i after %i trials. Raising exception.' % (self, self.ip_addr, self.ip_port, trial))
                  raise IOError('%r: error sending to %s:%i' % (self, self.ip_addr, self.ip_port))
              self.log.warning('%r: error sending to  %s:%i on trial %i. trying to reconnect...' % (self, self.ip_addr, self.ip_port, trial))
              self._connect()


    def recv(self, buffer_size=16384, timeout=None):
        trial = 0
        while True:
            try:
                if timeout:
                     old_timeout = self.sock.gettimeout()
                     self.sock.settimeout(timeout)
                data = self.sock.recv(buffer_size)
                if not data:
                    raise socket.error #('recv returned an empty string')
                return data
            except socket.timeout:
                self.log.warning('timeout')
                raise IOError('%r: timout while waiting for socket data' % (self))
            except socket.error:
                trial += 1
                if trial >3:
                    self.log.warning('%r: unable to receiuve data from %s:%i after %i trials. Raising exception.' % (self, self.ip_addr, self.ip_port, trial))
                    raise IOError('%r: error receiving from %s:%i' % (self, self.ip_addr, self.ip_port))
                self.log.warning('%r: socket error sending to  %s:%i on trial %i. trying to reconnect...' % (self, self.ip_addr, self.ip_port,trial))
                self._connect()
            finally:
                if timeout:
                    self.sock.settimeout(old_timeout)

    def flush(self, timeout=0.1):
        with self.socket(flush=True, flush_timeout=timeout):
            pass


##########################################################################################
# Helper functions for implementing command-line interfaces to the REST client and servers
##########################################################################################


def run_client(args, server_class=None, client_class=None, object_name='', server_config_path=None, parser=None):

    if parser is None:
        parser = argparse.ArgumentParser(description=object_name, epilog='')


    parser.add_argument('args', type=str, nargs='*', default='',  help='[[config] [server_name]] [command {args}]')
    # parser.add_argument('-d', '--debug', action='store_true', help="debug mode")
    parser.add_argument('-p', '--port', default=None, type=int, help="port used by the server")
    parser.add_argument('-n', '--host', default=None, type=str, help="server hostname")
    run = parser.add_mutually_exclusive_group(required=False)
    run.add_argument('--run', action='store_true', help='runs de client/server untilinterrupted by Ctrl-C. Implicit if no command is given')
    run.add_argument('--no-run', action='store_true', help='Do not run the client even if no command is given. Useful in interactive sessions')
    parser.add_argument('--no-start', action='store_true', help='prevents the initializatoin the server with the provided config when it is not initialized')
    args = parser.parse_args()


    sargs = args.args # string arguments
    config = None
    server_name = None
    server = None
    client = None
    command = None

    # Create our own IOLoop so we don't interfere with ipython's own ioloop.
    old_ioloop = IOLoop.current()
    ioloop = IOLoop()
    ioloop.make_current()


    # Setup profiler web server
    #routes = TornadoProfiler().get_routes()
    #profile_app = tornado.web.Application(routes)
    #profile_app.listen(54329)

    def is_client_method(string):
        return ':' not in string and '.' not in string and hasattr(client_class, string)

    # if first arg is not a client method, it must be a config name
    if sargs and not is_client_method(sargs[0]):
        config = load_yaml_config(sargs[0])
        sargs = sargs[1:]

    # if we need a server name, process it.
    server_config = config
    if server_config_path and server_config:
        # If the next string is not a method name, it must be a server name. Grab it.
        if sargs and  not is_client_method(sargs[0]):
            server_name = sargs[0]
            sargs = sargs[1:]
        # Get the config object that contains the server configs

        for name in server_config_path.split('.'):
            server_config = server_config[name]
        if not server_config:
            raise RuntimeError('The config file contains no server configuration block under %s' % server_config_path)
        #Check that a server has been provided unless there is only one choice in the config file
        if not server_name:
            if len(server_config) == 1:
                server_name = server_config.keys()[0]
            else:
                raise RuntimeError('There are more than one server configuration in %s. You must specify a server name' % server_config_path)
        # Get the server config with the specified name
        if server_name not in server_config:
            raise RuntimeError('The server name "%s" does not exist in the configuration under %s' % (server_name, server_config_path))
        server_config = server_config[server_name]

    if sargs:
        if is_client_method(sargs[0]):
            command = sargs[0]
            sargs = sargs[1:]
        else:
            raise RuntimeError('Unknown command "%s".  Client commands are: \n%s' % (command, '\n'.join(dir(client_class))))

    port = args.port or (server_config.get('port', None) if server_config else None) or server_class.DEFAULT_PORT
    hostname = args.host or (server_config.get('hostname', None) if server_config else None) or 'localhost'

    # Create the client. A local server will be created if it does not respond.
    client = RunSyncWrapper(client_class(hostname=hostname, port=port))
    server = RunSyncWrapper(getattr(client, 'server', None)) # get the local server if one was started

    # Confirm that a server is running with the same config is running, or start the server if it is not running
    #print(server_config, args.no_start)
    if server_config and not args.no_start:
        #print('starting client')
        client.start(server_config)

    if command:
        print('Sending command %s(%s) to %r' % (command, ', '.join(sargs), server))
        reply = getattr(client, command)(*sargs)
        print()
        print(yaml.safe_dump(reply))


    if args.run or (server and not command and not args.no_run):
        if server:
            print("REST Server is running. Waiting for REST commands")
        else:
            print("REST client is running.")
        print("Press CTRL-C to exit")
        client.run()
        print("\nI'm done. Bye!")

    print()
    print("If this was run in an interactive ipython session (with run -i module_name args...), the following variables are now accessible:")
    if server:
        print("   server: REST server")
    if client:
        print("   client: REST client")

    old_ioloop.make_current()
    return client, server

