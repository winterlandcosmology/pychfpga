"""Burst-read wrapper around :class:`UCorrPacketProcessor`."""

import time
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
        config_getter,
        pre_func=None,
        receive_window=3,
        verbose=0,
    ):
        if config_getter is None:
            raise TypeError("config_getter is required")
        self.pre_func = pre_func
        super().__init__(
            packet_receiver,
            config_getter=config_getter,
            receive_window=receive_window,
            verbose=verbose,
        )
        self.current_row = 0

    def flush(self, flush_timeout=None):
        """Drain UDP, clear assembly state, and reset the read cursor."""
        super().flush(flush_timeout)
        self.current_row = 0
        self.skip_first_incomplete = True

    def read_corr_frames(
        self,
        number_of_results=1,
        soft_integ_period=1,
        flush=False,
        data_timeout=0.1,
        flush_timeout=0.001,
        verbose=None,
        skip_first_incomplete=None,
        **kwargs,
    ):
        """Capture ``number_of_results`` integrated frames into preallocated arrays.

        Returns ``(integ_ts, data, counts, sats)`` of shapes
        ``(N,)``, ``(N, NBINS, NPROD)``, ``(N,)``, ``(N, NBINS, NPROD)``.

        Parameters:
            number_of_results (int): Number of software-integrated frames to capture.
            soft_integ_period (int): Number of hardware frames to accumulate in
                software before yielding a result.
            flush (bool): If True, drain the UDP buffer before starting capture.
            data_timeout (float): Socket timeout while waiting for packets.
            flush_timeout (float): Socket timeout used while flushing.
            verbose (int): Verbosity level for optional prints.
            skip_first_incomplete (bool or None): If True, drop the first emitted
                row when it is incomplete. ``None`` (default) follows ``flush``.
        """
        if verbose is not None:
            self.verbose = verbose
            self.pr.verbose = verbose
            if verbose >= 2:
                print(f"UCorrFrameReceiver: verbose={verbose}")

        if self.pre_func:
            self.pre_func(self)
        if soft_integ_period != self.soft_integ_period:
            self.set_soft_integ_period(soft_integ_period)
        self.update_config()
        if data_timeout is not None:
            self.pr.settimeout(data_timeout)

        integ_ts, data, counts, sats = self.create_output_buffer(n_ts=number_of_results)

        rows_completed = 0

        self.reset_profile()
        stats_fn = getattr(self.pr, "udp_socket_stats", None)
        udp0 = stats_fn() if callable(stats_fn) else None

        if flush:
            t_flush = time.perf_counter()
            self.flush(flush_timeout)
            self.profile["flush_s"] = time.perf_counter() - t_flush
        if skip_first_incomplete is None:
            skip_first_incomplete = bool(flush)
        self.skip_first_incomplete = bool(skip_first_incomplete)

        while rows_completed < number_of_results:
            stop = self.out_rows.stop
            if self.current_row < stop:
                # Format one integration at a time. A 2-row scatter is ~3x a 1-row
                # scatter and stalls receive_packets long enough for the rx pool
                # and kernel RCVBUF to overflow (the every-other-run drop pattern).
                n = min(1, number_of_results - rows_completed, stop - self.current_row)
                rows = slice(self.current_row, self.current_row + n)
                result = self.get_formatted_data(rows)
                if result is None:
                    break
                it_, d_, c_, s_ = result
                dest = slice(rows_completed, rows_completed + n)
                integ_ts[dest] = it_
                data[dest] = d_
                counts[dest] = c_
                sats[dest] = s_
                rows_completed += n
                self.current_row += n
            if rows_completed >= number_of_results:
                break
            while True:
                t_recv = time.perf_counter()
                self.pr.receive_packets(verbose=self.verbose)
                self.profile["recv_s"] += time.perf_counter() - t_recv
                self.profile["recv_n"] += int(self.pr.n_stored)
                self.profile["recv_calls"] += 1
                if self.pr.n_stored:
                    self.process_udp_packets(update_config=False)
                    self.current_row = self.out_rows.start
                    if self.n_out:
                        break

        udp1 = stats_fn() if callable(stats_fn) else None
        if rows_completed != number_of_results:
            warnings.warn(
                f"read_corr_frames: received {rows_completed} integ(s), expected {number_of_results}; "
                "remaining rows are zero-filled (timeouts, no packets passing filters, or slow source).",
                UserWarning, stacklevel=2,
            )
        if self.verbose >= 1:
            p = self.profile
            drop = None
            if udp0 is not None and udp1 is not None:
                drop = udp1[1] - udp0[1]
            print(
                f"UCorrFrameReceiver: profile recv {p['recv_s']*1e3:.0f} ms "
                f"({int(p['recv_n'])} pkts / {int(p['recv_calls'])} calls) "
                f"process {p['process_s']*1e3:.0f} ms ({int(p['process_calls'])} calls, "
                f"init_dest {p['init_dest_s']*1e3:.0f} ms move {p['init_dest_move_s']*1e3:.0f} ms, "
                f"store {p['store_s']*1e3:.0f} ms) "
                f"format {p['format_s']*1e3:.0f} ms ({int(p['format_calls'])} calls)"
                + (f" flush {p.get('flush_s', 0)*1e3:.0f} ms" if p.get("flush_s") else "")
                + (f" udp_drops +{drop} rxq={udp1[0]}" if drop is not None else "")
                + f" counts={counts.tolist()} expected={self.expected_packets}"
            )
        return integ_ts, data, counts, sats
