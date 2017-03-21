#!/usr/bin/env python

from __future__ import absolute_import, division, print_function

import sys

import tornado
import tornado.web

class Handler(tornado.web.RequestHandler):

    def initialize(self, port):
        self.port = port

    def post(self, path):
        body = self.request.body
        print(self.port, path, body)
        self.write(body)

#
# fake Kotekan server, for testing
#
if __name__ == '__main__':

    port = int(sys.argv[1])
    loop = tornado.ioloop.IOLoop.instance()
    url = tornado.web.url
    app = tornado.web.Application([
        url(r'/(.*)', Handler, dict(port=port)),
    ])
    app.listen(port)
    loop.start()

