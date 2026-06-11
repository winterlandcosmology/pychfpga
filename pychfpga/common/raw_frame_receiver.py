import warnings

import numpy as np

from .raw_data_packet_processor import RawDataPacketProcessor


class RawFrameReceiver(RawDataPacketProcessor):
    """Convenience wrapper around :class:`RawDataPacketProcessor` for burst reads."""

    def __init__(self, packet_receiver, config, verbose=0, ignore_first_burst=True, **kwargs):
        """
        Initialize the raw frame receiver.

        Parameters:
            packet_receiver (UDPPacketReceiver): UDP packet receiver providing the packet buffer.
            raw_capture_state (dict): Serialized ``chfpga.config.raw_capture``.
            verbose (int): Verbosity level for optional prints (0 quiet; ``>= 2`` enables
                ``process_packets`` / flush progress lines).
            ignore_first_burst (bool): If True (default), the first yielded burst is skipped
                only when its timestamp is exactly 0. This handles known startup behavior
                where the first post-reconfiguration burst can be incomplete.
            **kwargs (Any): Additional parameters forwarded to RawDataPacketProcessor
                (e.g., ring_buffer_size, decode_strict).
        """
        super().__init__(packet_receiver, config=config, verbose=verbose, **kwargs)

        self.ignore_first_burst = bool(ignore_first_burst)
        self.current_row = 0

    def update_config(self, config=None):
        super().update_config(config)

    def flush(self, flush_timeout = None):
        """Clear UDP and assembly state (matches a clean capture)."""
        super().flush(flush_timeout)
        self.current_row = 0

    def read_raw_frames(
        self,
        ncap: int = 1,
        split=None,
        source=0,
        flush=False,
        flush_timeout=None,
        chan_first: bool = True,
        config=None,
        format=None,
        verbose=None,
    ):
        """
        Read a fixed number of raw frames into preallocated arrays.

        Parameters:
            ncap (int): Number of **bursts** to capture. The number of rows returned might be different depending `split` and the specific output format for the specified source
            split (bool): If True, split each burst into per-firmware-frame rows. Has an effect only on real data (i.e. timestream typically returned by source 0). Has no effect on complex data
            source (int): Capture source index (0=ADC, 1=FFT).
            flush (bool): If True, flush UDP and assembly state before capture.
            chan_first (bool): If True (default), ``data`` is ``(nchan, ncap, nsamples)``.
                If False, ``data`` is ``(ncap, nchan, nsamples)``.
            config: Configuration passed to :meth:`update_config`. If not specified, the config specified when the constructor was called is used. No action is taken if no change is detected compared to the previous config. Some changes will cause buffered data to be lost. 
            verbose (int): Verbosity level for optional prints.

        Returns:
            Tuple[np.ndarray, np.ndarray, np.ndarray]: ``ts``, ``data``, ``counts``.
        """
        if verbose is not None:
            self.verbose = verbose
            self.pr.verbose = verbose
            if verbose >= 2:
                print(f"RawFrameReceiver: verbose={verbose}")

        if config is not None:
            self.update_config(config)


        ts, data, counts = self.create_output_buffer(n_ts=ncap, source=source, split=split, format=format)
        # ts.fill(0)
        # data.fill(0)
        # counts.fill(0)

        raw_rows_completed = 0
        fmt_rows_completed = 0


        if flush:
            print(f"Flushing receiver state")
            self.flush(flush_timeout)

        while raw_rows_completed < ncap:
            if self.current_row < self.n_out:
                n = min(ncap - raw_rows_completed, self.n_out - self.current_row)
                print(f"Taking  {n} rows from raw buffer {self.current_row} to {self.current_row + n}")
                rows = slice(self.current_row, self.current_row + n)
                ts_, data_, counts_ = self.format_data(rows, source=source, split=split, format=format)
                raw_rows_completed += n
                n_fmt = ts_.shape[0]  # number of formatted rows to store
                fmt_rows = slice(fmt_rows_completed, fmt_rows_completed + n_fmt)
                ts[fmt_rows], data[fmt_rows], counts[fmt_rows] = ts_, data_, counts_
                fmt_rows_completed += n_fmt
                self.current_row += n
            print(f"{raw_rows_completed=}  {fmt_rows_completed=} {ncap=} {self.current_row=} {self.n_out=}")
            if raw_rows_completed >= ncap:
                break
            while True:  # Wait nutil we have new data
                print(f"Receiving packets. current_row is {self.current_row}, {self.n_out=} rows in buffer")
                self.pr.receive_packets(verbose=self.verbose)
                self.current_row = 0
                if self.pr.n_stored:
                    self.process_udp_packets()
                    print(f"Received {self.n_out} rows from raw buffer")
                    if self.n_out:
                        break

        if chan_first:
            data = data.swapaxes(0, 1)
        return ts, data, counts
