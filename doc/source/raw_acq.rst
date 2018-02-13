:mod:`raw_acq` module: Raw data acquisition server and client
=============================================================

.. automodule:: raw_acq

.. autosummary::

	raw_acq.GainCalc
	raw_acq.GainEstimator
	raw_acq.RawAcqUDPReceiver
	raw_acq.hdf5TimestreamData
	raw_acq.dataWriter
	raw_acq.RawAcqReceiver
	raw_acq.RawAcqAsyncRESTServer
	raw_acq.RawAcqAsyncRESTClient

Data acquisition REST Server
*****************************
.. autoclass:: raw_acq.RawAcqAsyncRESTServer
	:members:

.. autoclass:: raw_acq.hdf5TimestreamData
	:members:


Data acquisition REST Client
*****************************
.. autoclass:: raw_acq.RawAcqAsyncRESTClient
	:members:


Python-based UDP packet receiver
********************************
.. autoclass:: raw_acq.RawAcqReceiver
	:members:

.. autoclass:: raw_acq.RawAcqUDPReceiver
	:members:

Gain computation
****************

.. autoclass:: raw_acq.GainCalc
	:members:

.. autoclass:: raw_acq.GainEstimator
	:members:





.. .. automethod:: chFPGA_controller.__init__(*see below*)
