:mod:`wtl.rest` module: base library for implementing REST client and servers
=============================================================================

.. automodule:: wtl.rest


.. rubric:: Coroutine support

.. autosummary::

   wtl.rest.coroutine
   wtl.rest.coroutine_return
   wtl.rest.RESTClient
   wtl.rest.AsyncMixin
   wtl.rest.AsyncRESTClient
   wtl.rest.JsonRequestHandler
   wtl.rest.AsyncRESTServer
   wtl.rest.SocketContext
   wtl.rest.RunSyncWrapper
   wtl.rest.endpoint



Synchronous operation of asynchronous code
******************************************

.. autoclass:: wtl.rest.RunSyncWrapper
	:members:

TCP socket handler
******************

.. autoclass:: wtl.rest.SocketContext
	:members:
