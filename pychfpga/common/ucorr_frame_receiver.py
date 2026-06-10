"""Burst-read wrapper around :class:`UCorrPacketProcessor`."""

import itertools
import warnings
from typing import Generator

from .ucorr_packet_processor import UCorrPacketProcessor


class UCorrFrameReceiver(UCorrPacketProcessor):
    """Convenience wrapper around :class:`UCorrPacketProcessor` for burst reads.


    The receiver is fast enough to capture data that is integrated in firmware
    down to a rate of about 10 ms/integrated frame, that is, exceeding 500
    Mbits/s, provided the system provides a sufficiently big UDP buffer to hold
    the data until the receiver method is called to process it (see below).

    The receiver can perform real-time software integration of the data. A
    specific number of software-integrated frames can be returned, or data can
    be saved to disk indefinitely until stopped.

    Parameters:

        packet_receiver (object): Instance to the UDP packet receiver that stores UDP packets to be
            processed. The receiver shall have a get_packets() method if the standalone
            `read_corr_frames` method is to be used.

        config (dict): Dict containing initial information on the correlator
            configuration.

        pre_func (function): Function to be called before data acquisition and processing is performed.

            , or function  to be called to get that dict. The dict shall also be passed
            to subsequent correlator packet request calls if the configuration has changed.

            If `corr_params` is a function it should have the signature ``corr_params(init)`` where
            True on initial setup to force an update of all parameters, and generally False on each
            subsequent packet request unless a re-init is requested by the user. The function can be
            used to setup the correlator data transmission path when init=False if needed by the
            firmware. If ``init=False``, the function can return `None` to indicate that no
            configuration change has occured. Since the function will be called on every
            time-sensitive packet requests, care should be taken to avoid any lengthy or unnecessary
            processing.

            Note that the user can manually call the `update_params` method after changes that impact the receiver.


    Data format
    -----------
    The number of correlation products computed ny the N-square correlator is NPROD = NCHAN*(NCHAN+1)/2.

    Correlator products are sent as N_CLKBINS=NCLK*NPROD_PER_CMAC packets from each correlator core in the array, each packet containing N_PROD/NCLK complex products.

    Complex value data results

    Each correlator frame has a (18+18) bit resolution, which is then
    integrated for some time. Assuming the worst case of a saturatet 1
    Gb/s link sending the maximum value if 2**17, we would get 175.8
    correlator frames/s,  with soft integrator values of increase by 2**24.46/s. If we integrate for
    we

    A float32 can represent integers values exactly up to 2**24, which
    leaves room for less than one second of integration in the worst
    case. We cannot thereofre use a complex64 value (float32+float32),
    and therefore use a complex128 format.


    System Requirements
    -------------------

    Jumbo Frames
    ............


    UCAP packets are small and do not require Jumbo frames.

    System UDP Buffers
    ..................


    Correlator packets are sent if very fast bursts and need to be stored in the OS networking stack until Python gets around to read them, otherwise packet loss will occur.

    Insufficient OS buffering is the main cause for UDP packet loss (not the computer speed, not the switches, not Python).

    The UDP buffers shall be increased to reduce packet loss to a minimum::

        sudo sysctl -w net.core.rmem_max=262144000
        sudo sysctl -w net.core.rmem_default=262144000
        sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 262144000'
        sudo sysctl -w net.ipv4.udp_rmem_min=262144000

    To check UDP buffers::

        sysctl -a | grep mem

    The following command can be used to monitor packet dropped by the OS in real time due to insufficient UDP buffer space::

        watch -cd -n .5 "cat  /proc/net/udp"
    """

    def __init__(
        self,
        packet_receiver,
        config=None,
        pre_func=None,
        receive_window=3,
        verbose=0,
    ):
        self.pre_func = pre_func
        super().__init__(packet_receiver, config=config, receive_window=receive_window, verbose=verbose)
        # Singleton iterator: an islice that stops mid-batch must not abandon unyielded
        # rows — a fresh iterator would let the next pr.receive_packets() overwrite them.
        self.data_iter: Generator = self.get_data_iter()

    def flush(self, flush_timeout=None):
        """Drain UDP, clear assembly state, and restart the streaming iterator."""
        super().flush(flush_timeout)
        self.data_iter = self.get_data_iter()

    def get_data_iter(self):
        """Continuously receive UDP packets, process them, and yield ``(integ_ts, data, sats, counts)`` rows."""
        while True:
            self.pr.receive_packets(verbose=self.verbose)
            if not self.pr.n_stored:
                continue
            self.process_udp_packets()
            result = self.get_formatted_data()
            if result is None:
                continue
            integ_ts, data, sats, counts = result
            for it, d, s, c in zip(integ_ts, data, sats, counts):
                yield it, d, s, c

    def read_corr_data_iter(
        self,
        number_of_results=None,
        soft_integ_period=1,
        flush=False,
        data_timeout=0.1,
        flush_timeout=0.001,
        config=None,
        verbose=0,
    ):
        """Yield ``number_of_results`` ``(integ_ts, data, sats, counts)`` tuples.

        The loop pulls batches of packets from the UDP receiver, decodes them
        into integration slots via ``get_data_iter``, and yields each completed
        (data, counts, sats) tuple. Optional flushing and alignment allow you to
        discard partial integrations and start on a clean boundary.

        Parameters:


            number_of_results (int): Number of results to yield. If None or zero, the generator runs indefinitely.

            soft_integ_period (int): Number of hardware frames to accumulate in
                software before yielding a result. Results are aligned to
                multiples of ``soft_integ_period`` when alignment is enabled.

            flush (bool): If True, drain the UDP buffer before starting capture.
                Use True on the first call in a burst series and False on
                subsequent calls to avoid losing in-flight packets.

            align (bool): If True, discard the first partial integration so the
                first yielded result starts on a clean integration boundary.
                This is forced on when ``flush`` is True.

            data_timeout (float): Socket timeout while waiting for packets. Set
                to slightly less than the expected inter-burst gap so the
                buffer can be processed promptly; too large delays processing
                until the buffer fills.

            flush_timeout (float): Socket timeout used while flushing. Must be
                shorter than the gap between packet bursts so flushing stops.

            verbose (int): verbosity level. 0=Quiet, 1=basic info, 2=detailed info, 3 = debugging info
        """
        # self.verbose = verbose
        # self.soft_integ_period = soft_integ_period,
        # self.skip_partial_integs = skip_partial_integs,

        if self.pre_func:
            self.pre_func(self)
        if config is not None:
            self.update_config(config)
        if soft_integ_period != self.soft_integ_period:
            self.update_config({"soft_integ_period": int(soft_integ_period)})
        if flush:
            self.flush(flush_timeout)
        self.pr.settimeout(data_timeout)
        return itertools.islice(self.data_iter, number_of_results) if number_of_results else self.data_iter

    def read_corr_frames(self, number_of_results=1, **kwargs):
        """Capture ``number_of_results`` integrated frames into preallocated arrays.

        Returns ``(integ_ts, data, sats, counts)`` of shapes
        ``(N,)``, ``(N, NBINS, NPROD)``, ``(N, NBINS, NPROD)``, ``(N,)``.
        """
        if (config := kwargs.get("config")) is not None:
            self.update_config(config)
        integ_ts, data, sats, counts = self.create_output_buffer(n_ts=number_of_results)
        integ_ts.fill(0)
        data.fill(0)
        sats.fill(0)
        counts.fill(0)
        n_filled = 0
        for i, (it, d, s, c) in enumerate(self.read_corr_data_iter(number_of_results=number_of_results, **kwargs)):
            integ_ts[i], data[i], sats[i], counts[i] = it, d, s, c
            n_filled = i + 1
        if n_filled != number_of_results:
            warnings.warn(
                f"read_corr_frames: received {n_filled} integ(s), expected {number_of_results}; "
                "remaining rows are zero-filled (timeouts, no packets passing filters, or slow source).",
                UserWarning, stacklevel=2,
            )
        return integ_ts, data, sats, counts
