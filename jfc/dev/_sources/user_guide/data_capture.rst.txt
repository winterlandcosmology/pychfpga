------------
Data capture
------------

The firmware can periodically send snapshots of the raw ADC samples or of the channelized data to
the host over the 1G control link. This page assumes an initialized array ``ca`` (see
:doc:`interactive_operation`) and a host configured for jumbo frames (see :doc:`networking`).

Start the capture
=================

.. code-block:: python

    await ca.start_data_capture_async(source="funcgen", period=1)

``source`` selects what is captured:

- ``"funcgen"`` (or ``0``): data after the function generator, i.e. ADC samples or an injected
  waveform
- ``"scaler"`` (or ``1``): channelized data at the scaler output

Other arguments are passed to each board's
:meth:`~pychfpga.fpga_firmware.chfpga.chfpga.chFPGA.start_data_capture`: ``period`` (seconds
between bursts), ``channels``, ``data_type``, ``mode`` (UCAP capture mode on RFSoC platforms) and
``send_delay``. ``send_delay_per_slot`` staggers the boards in a crate so their bursts do not
collide in the switch.

Read the frames
===============

.. code-block:: python

    r = await ca.get_data_receiver_async()
    ts, data, counts = r.read_raw_frames(ncap=4, flush=True)

:meth:`~pychfpga.common.raw_frame_receiver.RawFrameReceiver.read_raw_frames` returns timestamps,
data and per-frame counts as NumPy arrays. With the default ``chan_first=True``, ``data`` has
shape ``(channels, bursts, samples)``.

Stop the capture
================

.. code-block:: python

    ca.ib.stop_data_capture()

For continuous capture and storage to HDF5, run the ``raw_acq`` server instead (see
:doc:`server_operation`).
