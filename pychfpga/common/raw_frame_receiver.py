import warnings

import numpy as np

from .raw_data_packet_processor import RawDataPacketProcessor


class RawFrameReceiver(RawDataPacketProcessor):
    """Convenience wrapper around :class:`RawDataPacketProcessor` for burst reads."""

    def __init__(self, packet_receiver, config_getter, verbose=0, **kwargs):
        """
        Initialize the raw frame receiver.

        Parameters:
            packet_receiver (UDPPacketReceiver): UDP packet receiver providing the packet buffer.
            config_getter: Callable returning a stable raw capture :class:`State`.
            verbose (int): Verbosity level for optional prints (0 quiet; ``>= 2`` enables
                ``process_packets`` / flush progress lines).
            **kwargs (Any): Additional parameters forwarded to RawDataPacketProcessor
                (e.g., ring_buffer_size, decode_strict).
        """
        super().__init__(packet_receiver, config_getter=config_getter, verbose=verbose, **kwargs)

        self.current_row = 0
        self.update_config()

    def flush(self, flush_timeout = None):
        """Clear UDP and assembly state (matches a clean capture)."""
        super().flush(flush_timeout)
        self.current_row = 0
        self.skip_first_incomplete = True

    def read_raw_frames(
        self,
        ncap: int = 1,
        split=None,
        source=None,
        flush=False,
        flush_timeout=None,
        chan_first: bool = True,
        format=None,
        complex_override=None,
        verbose=None,
        skip_first_incomplete=None,
    ):
        """
        Read a fixed number of raw frames into preallocated arrays.

        Parameters:
            ncap (int): Number of **bursts** to capture. The number of rows returned might be different depending `split` and the specific output format for the specified source
            split (bool): If True, split each burst into per-firmware-frame rows. Has an effect only on real data (i.e. timestream typically returned by source 0). Has no effect on complex data
            source (int): Capture source index (0=funcgen/ADC, 1=scaler/FFT). If ``None``, uses ``config.source``.
            flush (bool): If True, flush UDP and assembly state before capture.
            chan_first (bool): If True (default), ``data`` is ``(nchan, ncap, nsamples)``.
                If False, ``data`` is ``(ncap, nchan, nsamples)``.
            verbose (int): Verbosity level for optional prints.
            skip_first_incomplete (bool or None): If True, drop the first emitted
                burst when it is incomplete. ``None`` (default) follows ``flush``.

        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray]: ``ts``, ``data``, ``counts``.
        """
        if verbose is not None:
            self.verbose = verbose
            self.pr.verbose = verbose
            if verbose >= 2:
                print(f"RawFrameReceiver: verbose={verbose}")

        if source is None:
            source = int(self.config.get("source", 0))
        else:
            source = int(source)
        self.update_config()  # Make sure we pick up format changes since receiver creation so we allocate the correct output buffers below
        ts, data, counts = self.create_output_buffer(n_ts=ncap, source=source, split=split, format=format, complex_override=complex_override)
        # ts.fill(0)
        # data.fill(0)
        # counts.fill(0)

        raw_rows_completed = 0
        fmt_rows_completed = 0


        if flush:
            print(f"Flushing receiver state")
            self.flush(flush_timeout)
        if skip_first_incomplete is None:
            skip_first_incomplete = bool(flush)
        self.skip_first_incomplete = bool(skip_first_incomplete)

        while raw_rows_completed < ncap:
            stop = self.out_rows.stop
            if self.current_row < stop:
                n = min(ncap - raw_rows_completed, stop - self.current_row)
                #print(f"Taking  {n} rows from raw buffer {self.current_row} to {self.current_row + n}")
                rows = slice(self.current_row, self.current_row + n)
                ts_, data_, counts_ = self.get_formatted_data(rows, source=source, split=split, format=format, complex_override=complex_override)
                raw_rows_completed += n
                n_fmt = ts_.shape[0]  # number of formatted rows to store
                fmt_rows = slice(fmt_rows_completed, fmt_rows_completed + n_fmt)
                ts[fmt_rows], data[fmt_rows], counts[fmt_rows] = ts_, data_, counts_
                fmt_rows_completed += n_fmt
                self.current_row += n
            #print(f"{raw_rows_completed=}  {fmt_rows_completed=} {ncap=} {self.current_row=} {self.n_out=}")
            if raw_rows_completed >= ncap:
                break
            while True:  # Wait nutil we have new data
                #print(f"Receiving packets. current_row is {self.current_row}, {self.n_out=} rows in buffer")
                self.pr.receive_packets(verbose=self.verbose)
                if self.pr.n_stored:
                    self.process_udp_packets(update_config=False)
                    self.current_row = self.out_rows.start
                    #print(f"Received {self.n_out} rows from raw buffer")
                    if self.n_out:
                        break

        if chan_first:
            data = data.swapaxes(0, 1)
        return ts, data, counts
