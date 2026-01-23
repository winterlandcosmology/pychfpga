from typing import Iterator, Optional, Sequence, Tuple, List

from .udp_packet_receiver import UDPPacketReceiver

import numpy as np

class RawDataProcessor(object):
    """
    Fast packet-to-ring-buffer processor for raw UDP FPGA captures.

    The class decodes packet headers in-place from the shared packet buffer,
    filters packets by cookie, size, mode, timestamp, and stream ID, and writes
    payloads into a fixed-size ring buffer indexed by timestamp. Consumers read
    complete timestamp slots in FIFO order via the generator interface.
    """

    PACKET_COOKIE = 0xA0
    PACKET_COOKIE_MASK = 0xFE
    def __init__(self, packet_receiver: UDPPacketReceiver, platform_id: str, bytes_per_sample: int, samples_per_frame: int,
                 format: str = '8', stream_ids: Optional[Sequence[int]] = None, split_frames: bool = True,
                 ring_buffer_size: int = 3, target_mode: Optional[int] = None,
                 verbose: int = 0) -> None:
        """
        Initialize the processor and precompute views into the UDP receiver buffer.

        Parameters:
            packet_receiver (UDPPacketReceiver): UDPPacketReceiver instance providing the packet ring buffer.
            platform_id (str): Platform identifier used to select constants and layout.
            bytes_per_sample (int): Bytes per ADC sample.
            samples_per_frame (int): ADC samples per frame.
            format (str): Output format string (e.g., "8", "14", "16", "32", "64", with optional "+").
            stream_ids (Optional[Sequence[int]]): Stream IDs to accept; defaults are platform-specific.
            split_frames (bool): If True, formats output so the last data dimension is split into two:
                                 (..., FRAMES_PER_BURST, NBYTES_PER_BURST // FRAMES_PER_BURST)
            ring_buffer_size (int): Number of timestamp slots held in the internal ring buffer.
            target_mode (Optional[int]): Target mode selector for CRS boards (0-3); not used on ICE.
            verbose (int): Verbosity level (0 quiet, 1 basic, 2 detailed, 3 debug).
        """
        self.pr = packet_receiver
        self.verbose = verbose

        from ..fpga_firmware.chfpga import chFPGA
        # Generic chFPGA class for accessing platform constants
        self._chfpga = chFPGA
        if stream_ids and not (isinstance(stream_ids, list) or isinstance(stream_ids, np.ndarray)):
            raise TypeError('stream_ids must be a list or numpy array')

        # TODO: double check all the constants and shapes for CRS board

        self.PLATFORM_ID = platform_id
        self.BYTES_PER_SAMPLE = bytes_per_sample
        self.SAMPLES_PER_FRAME = samples_per_frame
        if platform_id == self._chfpga._PLATFORM_ID_CRS:
            self.stream_ids = stream_ids or np.arange(8)
            self.NUMBER_OF_SUBFRAMES = 4
            self.FRAMES_PER_BURST = 2
            if target_mode:
                raise ValueError(f"target_mode parameter is required for CRS boards (PLATFORM_ID: {self.PLATFORM_ID }). Please spesify the mode in range 0-3.")
            if 0 <= target_mode < 4:
                self.TARGET_MODE = target_mode
            else:
                raise ValueError(f"target_mode for CRS boards (PLATFORM_ID: {self.PLATFORM_ID }) must be in range 0-3.")
        elif platform_id == self._chfpga._PLATFORM_ID_MGK7MB_REV2:
            self.stream_ids = stream_ids or np.arange(16)
            self.NUMBER_OF_SUBFRAMES = 1
            self.FRAMES_PER_BURST = 1
            if target_mode:
                raise ValueError(f"target_mode parameter is not applicable to the ICE board (PLATFORM_ID: {self.PLATFORM_ID })")
            self.TARGET_MODE = 0
        else:
            raise NotImplementedError(f"Not implemented for platform {self.PLATFORM_ID}")

        self.BYTES_PER_FRAME = self.SAMPLES_PER_FRAME * self.BYTES_PER_SAMPLE
        self.BYTES_PER_SUBFRAME = self.BYTES_PER_FRAME // self.NUMBER_OF_SUBFRAMES

        # Removed "flags" from prober header to match CRS board "subframe". Flags were in the same byte before.
        # Subframe_mode is a byte, for MSB's of which are irrelevant. The last four bits are mode and subframe
        # numbers - two bits per each
        self.header_dtype = np.dtype(dict(
            names=['cookie', 'stream_id', 'frame_number', 'slot_chan', 'mode_subframe', 'ts'],
            offsets=[0, 1, 1, 2, 3, 2],
            formats=['u1', '>u2', 'u1', 'u1', 'u1', '>u8']))

        self.packet_dtype = np.dtype([
            ('header', self.header_dtype, (1,)),
            ('data', np.uint8, (self.BYTES_PER_SUBFRAME,))])

        self.PACKET_SIZE = self.packet_dtype.itemsize

        # Various views of the buffer to allow quick and easy access to the packet contents
        # Variables that start with buf_ are views of the data in the UDPPackageReceiver buffer
        self.buf_struct = self.pr.buf[:,:self.PACKET_SIZE].view(self.packet_dtype)[:,0]

        self.buf_cookie = self.buf_struct['header']['cookie'][:, 0]
        self.buf_frame_stream_id = self.buf_struct['header']['stream_id'][:, 0]
        self.buf_frame_number = self.buf_struct['header']['frame_number'][:, 0]
        self.buf_slot_chan = self.buf_struct['header']['slot_chan'][:, 0]
        self.buf_mode_subframe = self.buf_struct['header']['mode_subframe'][:, 0]
        self.buf_ts = self.buf_struct['header']['ts'][:, 0]
        self.buf_data = self.buf_struct['data']
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF) # keep the last 6 bytes (so 12 F's in total)
        self.sid_mask = 0x0FFF

        # Initializing arrays to which specific data (such as stream ID, frame number, etc.) about each valid packet
        # in buffer will be saved. When processing packets, only indices that correspond to the indices of valid packets
        # in the buffer will be overwritten, but array size must match the full size of buffer
        # The following arrays have the pkt_ prefix because they include specific info about each packet in the buffer
        NPACKETS = self.pr.n_packets
        self.pkt_stream_id = np.empty((NPACKETS,), dtype=np.uint16)
        self.pkt_ts = np.empty((NPACKETS,), dtype=np.uint64)
        self.pkt_mode = np.empty((NPACKETS,), dtype=np.uint8)
        self.pkt_frame = np.empty((NPACKETS,), dtype=np.uint8)
        self.pkt_subframe = np.empty((NPACKETS,), dtype=np.uint8)
        self.pkt_six = np.empty((NPACKETS,), dtype=np.uint16) # six = stream ID index
        # buffer for precomputing offsets when writing packets to raw data
        self.pkt_off = np.empty((NPACKETS,), dtype=np.uint32)

        self.TS_SLOTS = ring_buffer_size

        try:
            self.NCHAN = len(self.stream_ids)  # number of stream IDs (channels) to capture
        except TypeError:
            raise ValueError(f"Stream IDs {self.stream_ids} must be an indexable and sized object")

        self.six_map = np.full((1 << 12,), -1,
                               dtype=np.int64)  # maps a stream ID into a stream index, default of -1 means invalid stream ID

        self.six_map[self.stream_ids] = np.arange(self.NCHAN)

        self.TS_EMPTY = np.uint64(0xFFFFFFFFFFFFFFFF)
        self.ts_array = np.full(self.TS_SLOTS, self.TS_EMPTY, dtype=np.uint64)

        self.raw_data = np.zeros((self.TS_SLOTS, self.NCHAN, self.FRAMES_PER_BURST * self.BYTES_PER_FRAME),
                                 dtype=np.int8)

        self.raw_data_count = np.zeros((self.TS_SLOTS, self.NCHAN), dtype=np.int64)
        self.raw_data_complete = np.zeros((self.TS_SLOTS, self.NCHAN), dtype=np.uint8)

        self.configure_output(format=format, split_frames=split_frames)

        # Variables with ybuf_ prefix are dedicated to temporarily hold the yielded data. The generators will then
        # yield a read-only view of these buffers.
        self.ybuf_ts: int = 0
        self.ybuf_data_count = np.empty_like(self.raw_data_count[0, ...])

        # operational variables
        self._write_slot = 0
        self._read_slot = 0



    def get_data_iter(self, verbose: Optional[int] = None) -> Iterator[Tuple[np.uint64, np.ndarray, np.ndarray]]:
        """
        Yield formatted data for complete timestamps from the packet buffer.

        The generator scans the current packet buffer, extracts headers via
        precomputed NumPy views, filters packets, and writes payloads into the
        ring buffer. When a timestamp slot becomes complete, it is yielded in
        FIFO order. The yielded data is a read-only view; copy if it must
        outlive the next call.

        Parameters:
            verbose (Optional[int]): Optional verbosity override for this call.

        Yields:
            Tuple of (timestamp, formatted_data_view, per_channel_counts).
        """
        verbose = verbose or self.verbose

        # Select the buffer index that has the right cookie
        # bix_mask is needed to get numpy views of the buffer
        bix_mask = (
                ((self.buf_cookie & self.PACKET_COOKIE_MASK) == self.PACKET_COOKIE) &
                (self.pr.pkt_len == self.PACKET_SIZE)
        )
        bix = np.flatnonzero(bix_mask)


        if not bix.size:
        #     if verbose >= 3: print(f"get_data_iter: no packets matching the cookie and packet size")
            return

        # Extract header information
        # ts
        np.bitwise_and(self.buf_ts, self.ts_mask, out=self.pkt_ts, where=bix_mask)
        # stream_id
        np.bitwise_and(self.buf_frame_stream_id, self.sid_mask, out=self.pkt_stream_id, where=bix_mask)
        # frame
        np.right_shift(self.buf_frame_stream_id, 8, out=self.pkt_frame, where=bix_mask)
        np.bitwise_and(self.pkt_frame, 0xFF, out=self.pkt_frame, where=bix_mask)
        # mode
        np.right_shift(self.buf_mode_subframe, 2, out=self.pkt_mode, where=bix_mask)
        np.bitwise_and(self.pkt_mode, 0x3, out=self.pkt_mode, where=bix_mask)
        # subframe
        np.bitwise_and(self.buf_mode_subframe, 0x3, out=self.pkt_subframe, where=bix_mask)

        if verbose >= 2:
            print(f"get_data_iter: found {bix.size} packets matching the cookie and packet size")

        valid_ts = self.ts_array[self.ts_array != self.TS_EMPTY]
        min_ts = valid_ts.min() if valid_ts.size else np.uint64(0)

        # mode filter
        bix = bix[self.pkt_mode[bix] == self.TARGET_MODE]
        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering target mode")
        # timestamp filter
        bix = bix[self.pkt_ts[bix] >= min_ts]
        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering old timestamps")
        # 3) stream id  filter
        bix = bix[self.six_map[self.pkt_stream_id[bix]] >= 0]

        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering stream IDs")

        bix = bix[
            (self.pkt_frame[bix] < self.FRAMES_PER_BURST) &
            (self.pkt_subframe[bix] < self.NUMBER_OF_SUBFRAMES)
            ]


        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering valid frame and subframe indices")

        # Updating bix_mask after filtering bix
        bix_mask.fill(False)
        bix_mask[bix] = True
        #
        # if verbose >= 3:
        #     for bi in bix:
        #         print(
        #             f"get_data_iter: got packet {self.pr.buf[bi, :10].tobytes().hex(':')}\t\t{self.pr.buf[bi, 10:20].tobytes().hex(':')} ...")
        #         print(
        #             f"get_data_iter: read values {self.pkt_stream_id[bi]=}, {self.pkt_frame[bi]=}, {self.pkt_mode[bi]=}, {self.pkt_subframe[bi]=}, {self.pkt_ts[bi]=}")


        if not bix.size:
            if verbose >= 2: print(f"get_data_iter: no packets matching the filters (mode, timestamp, stream id)")
            return

        self.pkt_off[bix_mask] = (
                self.pkt_frame[bix_mask] * self.BYTES_PER_FRAME +
                self.pkt_subframe[bix_mask] * self.BYTES_PER_SUBFRAME
        ).astype(self.pkt_off.dtype, copy=False)

        ts_vals = self.pkt_ts[bix_mask]
        ts_sorted_ix = np.argsort(ts_vals, kind="mergesort")

        bix = bix[ts_sorted_ix]
        ts_vals = ts_vals[ts_sorted_ix]

        # Find group boundaries where timestamp changes, so we process several channels for the same timestamp at once
        ts_unique, starts, counts = np.unique(ts_vals, return_index=True, return_counts=True)
        self.pkt_six[bix_mask] = self.six_map[self.pkt_stream_id[bix_mask]]  # get stream index from stream id

        subframe_ix = np.arange(self.BYTES_PER_SUBFRAME, dtype=self.pkt_off.dtype)

        # Process data by timestamp groups
        for ts, s, count in zip(ts_unique, starts, counts):
            tix = bix[s:s + count]
        # for ts in np.unique(self.pkt_ts[bix]):
            if ts not in self.ts_array: # if timestamp is not in the list, add it
                next_slot = (self._write_slot + 1) % self.TS_SLOTS

                if next_slot == self._read_slot:
                    if self.ts_array[next_slot] != self.TS_EMPTY:
                        if verbose >= 3:
                            print(f"get_data_iter: force yielding slot {next_slot} to free the buffer")
                        # Yield incomplete data to free space
                        yield from self._yield_data(verbose=verbose)
                    else:
                        # Current _read_slot is empty, advance
                        if verbose >= 3:
                            print(f"get_data_iter: current slot {next_slot} is empty, likely the whole buffer is empty, "
                                  f"since bort read_slot and write_slot point to the same slot.")
                        self._read_slot = (self._read_slot + 1) % self.TS_SLOTS

                # invariant check
                if self.ts_array[next_slot] != self.TS_EMPTY:
                    raise RuntimeError(
                        "Ring invariant broken: next write slot is occupied. "
                        "This should never happen; investigate pointer/ts_array invariants."
                    )

                if self.raw_data_complete[next_slot].any() or self.raw_data_count[next_slot].any():
                    raise RuntimeError("Slot metadata not cleared for an empty slot — invariant broken.")


                self._write_slot = next_slot
                dest_slot = next_slot
                self.ts_array[dest_slot] = ts

            else:
                # timestamp already exist, add new data to it.
                dest_slot_arr = np.flatnonzero(self.ts_array == ts)

                if dest_slot_arr.size != 1:
                    raise RuntimeError(f"Timestamp {int(ts)} maps to {dest_slot_arr.size} slots (expected 1).")
                dest_slot = int(dest_slot_arr[0])

                if verbose >= 3:
                    print(f"get_data_iter: the current timestamp is already present in the buffer slot {dest_slot}")

            # tix = bix[self.pkt_ts[bix] == ts] # select buf indices with matching timestamps; tix = timestamp index

            if verbose >= 3:
                print(f"get_data_iter: writing data to slot {dest_slot}")

            _si = self.pkt_six[tix]
            _off = self.pkt_off[tix]
            self.raw_data[dest_slot, _si[:, None], _off[:, None] + subframe_ix] = self.buf_data[tix]

            self.raw_data_count[dest_slot, :] += np.bincount(self.pkt_six[tix], minlength=self.NCHAN)
            self.raw_data_complete[dest_slot, :] = self.raw_data_count[dest_slot, :] == self.FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES

            # We yield all the complete slots, but only in order, otherwise it would break the FIFO concept.
            # If the raw_data at self._read_index is not complete, we stop yielding, even if there are compete slots
            # after
            slot_complete = np.all(self.raw_data_complete, axis=1)  # boolean per slot
            while True:
                if verbose >= 3:
                    print(f"get_data_iter: Checking read_slot {self._read_slot} for complete data.")
                if self._read_slot == self._write_slot:
                    # ring buffer is empty
                    if verbose >= 3:
                        print(f"get_data_iter: Reached the current write_slot. Stopping.")
                    break
                elif self.ts_array[self._read_slot] == self.TS_EMPTY:
                    if verbose >= 3:
                        print(f"get_data_iter: Current read_slot {self._read_slot} is empty, advancing index.")
                    self._read_slot = (self._read_slot + 1) % self.TS_SLOTS
                elif slot_complete[self._read_slot]:
                    # yields complete slot and advances read index
                    if verbose >= 3:
                        print(f"get_data_iter: slot {self._read_slot} is complete. Yielding the data and advancing the read_slot index.")
                    yield from self._yield_data(verbose=verbose)
                else:
                    if verbose >= 3:
                        print(f"get_data_iter: Current read_slot {self._read_slot} is neither empty, nor complete. Stopping.")
                    # current read slot is neither empty nor full, stop yielding
                    break


    def _yield_data(self, verbose: Optional[int] = None) -> Iterator[Tuple[np.uint64, np.ndarray, np.ndarray]]:
        """
        Yield the current read slot and advance ring pointers.

        This assumes the caller has determined that the slot is ready (typically
        complete) or must be yielded to free space. Slot metadata is cleared so
        it can be reused.
        """
        verbose = verbose or self.verbose
        ts = self.ts_array[self._read_slot]
        data_count = self.raw_data_count[self._read_slot]

        raw_data_view = self.raw_data[self._read_slot].view()
        raw_data_view.setflags(write=False)
        ybuf_data_view = self._format_data_array(raw_data_view)

        if verbose >= 2:
            print(f'Yielding {self.raw_data_count[self._read_slot]} packets')

        old_read_slot = self._read_slot
        self._read_slot = (self._read_slot + 1) % self.TS_SLOTS

        yield ts, ybuf_data_view, data_count

        # mark slot as empty for future allocations
        self.ts_array[old_read_slot] = self.TS_EMPTY
        self.raw_data_count[old_read_slot, :].fill(0)
        self.raw_data_complete[old_read_slot, :].fill(0)


    def _allocate_ybuf_data(self, ncap: int = 1) -> np.ndarray:
        """
        Allocate the reusable formatted output buffer for the current format.

        The output shape and dtype are derived from raw_data and the format
        specifier, minimizing allocations during streaming.

        Parameters:
            ncap (int): if ncap > 1, another axis is added in front to accommodate multiple data captures
        """
        # Discard the first dimension which is ring buffer slot index
        fmt_data_shape = list(self.raw_data.shape[1:])
        fmt_dtype = self.raw_data.dtype

        if self.format.startswith("14") or self.format.startswith("16"):
            fmt_data_shape[-1] = fmt_data_shape[-1] // 2
        elif self.format.startswith("32"):
            fmt_data_shape[-1] = fmt_data_shape[-1] // 4
        elif self.format.startswith("64"):
            fmt_data_shape[-1] = fmt_data_shape[-1] // 8

        if '+' in self.format:
            fmt_data_shape[-1] = fmt_data_shape[-1] // 2
            if self.format.startswith("64"):
                fmt_dtype = np.complex128
            else:
                fmt_dtype = np.complex64

        if ncap > 1:
            fmt_data_shape.insert(0, ncap)

        return np.empty(shape=fmt_data_shape, dtype=fmt_dtype)


    def configure_output(self, format: Optional[str] = None, split_frames: Optional[bool] = None) -> None:
        """
        Update output formatting options.

        This can change the output data layout and dtype. If a format change
        requires a different buffer shape, the formatted output buffer is
        reallocated.

        Parameters:
            format (Optional[str]): Format string to set; None keeps current.
            split_frames (Optional[bool]): Whether to split frames in the output; None keeps current.
        """
        need_realloc = False
        if format is not None and format != getattr(self, "format", None):
            self.format = format
            need_realloc = True

        if split_frames is not None:
            self.split_frames = split_frames

        if need_realloc or not hasattr(self, "ybuf_data"):
            self.ybuf_data = self._allocate_ybuf_data()


    def _format_data_array(self, data: np.ndarray) -> np.ndarray:
        """
        Format a raw data view into the preallocated output buffer.

        The method reinterprets the raw byte stream according to self.format,
        handles complex interleaving for "+" formats, and optionally reshapes
        into per-channel views when split_frames is enabled.

        Parameters:
            data (np.ndarray): Raw byte view from a ring buffer slot.

        Returns:
            Read-only view of the formatted output buffer.
        """
        # Assumes self.ybuf_data was preallocated using self._allocate_ybuf_data and has correct shape and dtype
        out = self.ybuf_data

        if self.format.startswith("14"):
            data = data.view('>i2')
        elif self.format.startswith("16"):
            data = data.view('>i2')
        elif self.format.startswith("32"):
            data = data.view('>i4')
        elif self.format.startswith("64"):
            data = data.view('>i8')

        if '+' in self.format:
            # Assumes out is complex type
            out.real[...] = data[..., ::2]
            out.imag[...] = data[..., 1::2]
        elif self.format.startswith("14"):
            # There is no 14+14 format
            np.right_shift(data.view('>i2'), 2, out=out)
        else:
            out[...] = data[:]

        out_view = out.view()
        if self.split_frames:
            # Change shape of the view by assigning the shape directly. If reshaping requires copying data, then
            # numpy will throw an error. That means the algorithm has failed and should be fixed.
            out_view.shape = (self.NCHAN, self.FRAMES_PER_BURST, out.shape[-1] // self.FRAMES_PER_BURST)

        # TODO: 32+32d should be explained and reworked under the current implementation
        # if self.format == "32+32d":
        #     data = data.reshape((nchan, ncap * frames_per_channel, -1))  # split
        #     s = data.shape
        #     data = data.reshape(s[0], -1, 2, s[2]).reshape(s[0], -1, s[2] * 2, order='F')

        out_view.setflags(write=False)
        return out_view

    def flush(self, flush_timeout: Optional[float] = None) -> None:
        """Flush UDP packets and reset the internal ring buffer state.

        This clears any partially received data so the next read starts from a
        clean timestamp slot.

        Parameters:
            flush_timeout (Optional[float]): Timeout used while flushing the UDP socket.
        """
        flush_kwargs = {}
        if flush_timeout is not None:
            flush_kwargs["timeout"] = flush_timeout
        self.pr.flush(**flush_kwargs)
        if self.verbose >=2:
            print('Flushing RawDataProcessor ring buffer')
        self.ts_array.fill(self.TS_EMPTY)
        self.raw_data.fill(0)
        self.raw_data_count.fill(0)
        self.raw_data_complete.fill(0)
        self._read_slot = 0
        self._write_slot = 0
