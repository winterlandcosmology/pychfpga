"""Burst-read wrapper around :class:`UCorrPacketProcessor`."""

import warnings

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
        receive_window=3,
        verbose=0,
    ):
        super().__init__(packet_receiver, config=config, receive_window=receive_window, verbose=verbose)
        self.current_row = 0

    def flush(self, flush_timeout=None):
        """Drain UDP, clear assembly state, and reset the read cursor."""
        super().flush(flush_timeout)
        self.current_row = 0

    def read_corr_frames(
        self,
        number_of_results=1,
        soft_integ_period=1,
        flush=False,
        data_timeout=0.1,
        flush_timeout=0.001,
        config=None,
        verbose=None,
        **kwargs,
    ):
        """Capture ``number_of_results`` integrated frames into preallocated arrays.

        Returns ``(integ_ts, data, sats, counts)`` of shapes
        ``(N,)``, ``(N, NBINS, NPROD)``, ``(N, NBINS, NPROD)``, ``(N,)``.

        Parameters:
            number_of_results (int): Number of software-integrated frames to capture.
            soft_integ_period (int): Number of hardware frames to accumulate in
                software before yielding a result.
            flush (bool): If True, drain the UDP buffer before starting capture.
            data_timeout (float): Socket timeout while waiting for packets.
            flush_timeout (float): Socket timeout used while flushing.
            config: Configuration passed to :meth:`update_config`.
            verbose (int): Verbosity level for optional prints.
        """
        if verbose is not None:
            self.verbose = verbose
            self.pr.verbose = verbose
            if verbose >= 2:
                print(f"UCorrFrameReceiver: verbose={verbose}")

        if config is not None:
            self.update_config(config)
        if soft_integ_period != self.soft_integ_period:
            self.update_config({"soft_integ_period": int(soft_integ_period)})
        if data_timeout is not None:
            self.pr.settimeout(data_timeout)

        integ_ts, data, sats, counts = self.create_output_buffer(n_ts=number_of_results)

        rows_completed = 0

        if flush:
            self.flush(flush_timeout)

        while rows_completed < number_of_results:
            if self.current_row < self.n_out:
                n = min(number_of_results - rows_completed, self.n_out - self.current_row)
                rows = slice(self.current_row, self.current_row + n)
                result = self.get_formatted_data(rows)
                if result is None:
                    break
                it_, d_, s_, c_ = result
                out_rows = slice(rows_completed, rows_completed + n)
                integ_ts[out_rows] = it_
                data[out_rows] = d_
                sats[out_rows] = s_
                counts[out_rows] = c_
                rows_completed += n
                self.current_row += n
            if rows_completed >= number_of_results:
                break
            while True:
                self.pr.receive_packets(verbose=self.verbose)
                self.current_row = 0
                if self.pr.n_stored:
                    self.process_udp_packets()
                    if self.n_out:
                        break

        if rows_completed != number_of_results:
            warnings.warn(
                f"read_corr_frames: received {rows_completed} integ(s), expected {number_of_results}; "
                "remaining rows are zero-filled (timeouts, no packets passing filters, or slow source).",
                UserWarning, stacklevel=2,
            )
        return integ_ts, data, sats, counts
