""" REST Clients and Servers for CHIME
"""
from __future__ import print_function

import json
import requests

class RESTClient(object):
    """ This is a Requests-based client (non asynchronous)
    """
    TIMEOUT = 60 # seconds
    DEFAULT_HOST = 'localhost'
    DEFAULT_PORT = 54321

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port

    def __repr__(self):
        return '%s(%s:%s)' % (self.__class__.__name__, self.host, self.port)

    def print(self, msg):
        print(msg)

    def print_error(self, msg):
        print(msg)

    def error(self, msg):
        self.print_error(msg)
        raise

    def url(self, endpoint):
        return 'http://%s:%d/%s' % (self.host, self.port, endpoint)

    def get(self, endpoint):
        try:
            return requests.get(self.url(endpoint), timeout=self.TIMEOUT).json()
        except requests.exceptions.ConnectionError:
            self.error("Can't connect to REST server at %s:%d for GET request" % (self.host, self.port))

    def post(self, endpoint, **kvs):
        try:
            return requests.post(self.url(endpoint), json=kvs, timeout=self.TIMEOUT).json()
        except requests.exceptions.ConnectionError:
            self.error("Can't connect to REST server at %s:%d for PORT request" % (self.host, self.port))

class AsyncRESTClient(object):
    """Implements a kotekan REST client using a Tornado AsyncHTTPClient .

    All methods are Tornado coroutines so that operations can be performed concurrently on multiple nodes.
    """
    DEFAULT_HOST = 'localhost'
    DEFAULT_PORT = 80

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.client = tornado.httpclient.AsyncHTTPClient()

    def url(self, endpoint):
        return 'http://%s:%d/%s' % (self.host, self.port, endpoint)

    @tornado.gen.coroutine
    def send(self, endpoint, **kws):
        url = self.url(endpoint)
        body = tornado.escape.json_encode(kws)
        resp = yield self.client.fetch(url, method='POST', body=body)
        raise tornado.gen.Return(tornado.escape.json_decode(resp.body))

    @tornado.gen.coroutine
    def get(self, endpoint):
        url = self.url(endpoint)
        resp = yield self.client.fetch(url, method='GET')
        raise tornado.gen.Return(tornado.escape.json_decode(resp.body))


class JsonRequestHandler(tornado.web.RequestHandler):
    """
    Accept and return JSON instead of HTML.
    """

    def prepare(self):
        if not self.request.body: return
        content_type = self.request.headers['Content-Type']
        if content_type == 'application/json':
            try:
                args = tornado.escape.json_decode(self.request.body)
                self.request.arguments.update(args)
            except ValueError:
                self.send_error(400, error="can't parse JSON")
        elif content_type == 'application/x-www-form-urlencoded':
            args = { k:v[-1] for k,v in self.request.arguments.items() }
            self.request.arguments.update(args)

    def set_default_headers(self):
        self.set_header('Content-Type', 'application/json')

    def write_error(self, status_code, **kvs):
        if 'exc_info' in kvs:
            exc_info = kvs.pop('exc_info')
            kvs['error'] = ''.join(traceback.format_exception(*exc_info))
        self.write(kvs)


class RESTServer(object):
    """
    Creates a Tornado Web application that will call the endpoint handlers registered with the RESTserver.endpoint decorator.
    """
    _endpoint_info = [] # ths list is filled by the @endpoint decorator

    def __init__(self, port):

        # Create the endpoints registered with the @endpoint decorator and start the Application
        endpoints = []
        print(self._endpoint_info)
        for method_name, endpoint_name, method_args in self._endpoint_info:
            has_args = len(method_args)>2  # any other arguments beyound the mandatory 'self' and 'handler'?
            print('%s: Creating a REST %s endpoint %s for method %s(%s)' % (self.__class__.__name__, ('GET','POST')[has_args], endpoint_name, method_name, ', '.join(method_args)))
            endpoints.append(self.create_endpoint(method_name, endpoint_name, has_args))
        self.app = tornado.web.Application(endpoints)
        self.app.listen(self.port)

    def create_endpoint(self, method_name, endpoint_name, has_args):
            method = getattr(self, method_name)
            if has_args:
                class Handler(JsonRequestHandler):
                    @tornado.gen.coroutine
                    def post(self):
                       yield method(self, self.request.arguments)
            else:
                class Handler(JsonRequestHandler):
                    @tornado.gen.coroutine
                    def get(self):
                       yield method(self)
            return tornado.web.url(r'/%s' % endpoint_name, Handler)

    def shutdown(self):
        pass


    @classmethod
    def endpoint(cls, fn, endpoint_name=None):
        """ Decorator that create a REST endpoint for the decorated method.

        Methods with arguments other than 'self' will answer to POST requests and will be passed the decoded arguments.where
        otherwise it will be implemented as a GET endpoint.

        Endpoints with arguments oof type 'POST' have target methods with the signature my_method(self, handler,
        args) and receive the decoded arguments `args` which were sent along with the HTTP 'POST'
        request.

        'GET' handlers do not receive arguments and have a signature of my_method(self,
        handler).

        The target method has access to the ChimeMaster instance and other context information
        directly through ChimeMasterApp instance ('self').

        The target method receive the handler as an argument. It is usually used to send back
        replies through the handler.write(...) method.

        Target methods must Tornado co-routines, and should therefore `yield` when doing lengthy
        operations and shall not return values directly with the 'return' statement.

        This handler is meant to be passed to the Tornado Application when it is created.
        """
        method_name = fn.__name__
        if endpoint_name is None:
            endpoint_name = method_name.replace('_','-')
        method_args = inspect.getargspec(fn).args
        cls._endpoint_info.append((method_name, endpoint_name, method_args))
        return fn # return the original function as is, we just wanted to grab its info
