:mod:`rest` module: base library for implementing REST client and servers
=========================================================================

.. automodule:: rest


.. rubric:: Coroutine support

.. autosummary::

   rest.coroutine
   rest.coroutine_return
   rest.RESTClient
   rest.AsyncMixin
   rest.AsyncRESTClient
   rest.JsonRequestHandler
   rest.AsyncRESTServer
   rest.SocketContext
   rest.RunSyncWrapper
   rest.endpoint



Synchronous operation of asynchronous code
******************************************

.. autoclass:: rest.RunSyncWrapper
	:members:

TCP socket handler
******************

.. autoclass:: rest.SocketContext
	:members:
