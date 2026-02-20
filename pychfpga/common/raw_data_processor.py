from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

import numpy as np

from .udp_packet_receiver import UDPPacketReceiver


class RawDataProcessor(object):
    """
    Fast packet-to-ring-buffer processor for raw UDP FPGA captures.

    Packet payloads are written to an internal timestamp-indexed ring buffer.
    Data formatting/decoding occurs only when moving data from ring slots to
    the reusable yield buffer.

    Notes
    -----
    The decode path is intentionally copy-lean:
    - incoming packet bytes are stored in ``raw_data`` (ring buffer),
    - output is written into preallocated ``ybuf_data``,
    - formatting relies on NumPy views/reshape/slicing/bit operations.
    """

    PACKET_COOKIE = 0xA0
    PACKET_COOKIE_MASK = 0xFE
    ALLOWED_FORMATS = ["8", "14", "16", "32", "64", "32+32", "64+64"]

    def __init__(
        self,
        packet_receiver: UDPPacketReceiver,
        platform_id: str,
        bytes_per_sample: int,
        samples_per_frame: int,
        format: Optional[str] = "8",
        stream_ids: Optional[Sequence[int]] = None,
        ring_buffer_size: int = 3,
        target_mode: Optional[int] = None,
        verbose: int = 0,
        pkt_frames_per_burst: Optional[int] = None,
        capture_engine: Optional[str] = None,
        effective_decode_profile: Optional[Dict[str, Any]] = None,
        decode_strict: bool = True,
    ) -> None:
        """
        Build a packet parser and ring/yield buffering pipeline.

        Parameters
        ----------
        packet_receiver
            UDP packet source holding the shared packet buffer.
        platform_id
            Firmware platform identifier (CRS, ICE, ...).
        bytes_per_sample
            Native payload sample width used by the transport framing.
        samples_per_frame
            Number of transport samples in one packet frame.
        format
            Output scalar/complex format. Supported values:
            ``"8"``, ``"14"``, ``"16"``, ``"32"``, ``"64"``,
            ``"32+32"``, ``"64+64"``.
        stream_ids
            Stream IDs to accept. Other streams are discarded.
        ring_buffer_size
            Number of timestamp slots retained in the internal ring buffer.
        target_mode
            UCAP transport mode (0..3). Required for UCAP paths when it
            cannot be inferred from ``effective_decode_profile``.
        verbose
            Verbosity level for debug prints.
        pkt_frames_per_burst
            Number of packet frames expected per burst. Used for PROBER; UCAP
            derives this from ``target_mode``.
        capture_engine
            Capture transport engine selector: ``"PROBER"`` or ``"UCAP"``.
            If omitted, inferred from ``effective_decode_profile`` or platform.
        effective_decode_profile
            Optional host-derived decode context pushed by ``chFPGA``.
            When provided, this object controls logical framing/transform via:
            ``capture_engine``, ``source``, ``scaler_cap_data_type``,
            ``pkt_frames_per_burst``, ``packets_per_logical_frame``,
            ``logical_frames_per_burst``, ``number_of_subframes``,
            ``target_mode``, ``decode_transform``, ``default_format``,
            ``fft_bins_per_word``.
        decode_strict
            Strictness control for multi-packet logical decode.
            - ``True``: invalid profile/layout and incomplete logical groups
              raise explicit errors.
            - ``False``: tolerate some layout mismatches by falling back to
              coarser framing where possible.
        """
        self.pr = packet_receiver
        self.verbose = verbose
        self.decode_strict = decode_strict
        self.effective_decode_profile = effective_decode_profile or {}

        from ..fpga_firmware.chfpga import chFPGA

        self._chfpga = chFPGA
        if stream_ids is not None and not isinstance(stream_ids, (list, tuple, np.ndarray)):
            raise TypeError("stream_ids must be a list, tuple or numpy array")

        self.PLATFORM_ID = platform_id
        self.BYTES_PER_SAMPLE = bytes_per_sample
        self.SAMPLES_PER_FRAME = samples_per_frame

        self.CAPTURE_ENGINE = (
            capture_engine
            or self.effective_decode_profile.get("capture_engine")
            or ("UCAP" if platform_id == self._chfpga._PLATFORM_ID_CRS else "PROBER")
        )

        if platform_id == self._chfpga._PLATFORM_ID_CRS:
            self.stream_ids = stream_ids or np.arange(8)
            self.NUMBER_OF_SUBFRAMES = int(
                self.effective_decode_profile.get(
                    "number_of_subframes",
                    4 if self.CAPTURE_ENGINE == "UCAP" else 1,
                )
            )

            profile_target_mode = self.effective_decode_profile.get("target_mode")
            if target_mode is None:
                target_mode = profile_target_mode

            if self.CAPTURE_ENGINE == "UCAP":
                if target_mode is None:
                    raise ValueError(
                        f"target_mode parameter is required for UCAP capture on CRS (PLATFORM_ID: {self.PLATFORM_ID})."
                    )
                if not 0 <= int(target_mode) < 4:
                    raise ValueError(
                        f"target_mode for UCAP capture on CRS (PLATFORM_ID: {self.PLATFORM_ID}) must be in range 0-3."
                    )
                self.TARGET_MODE = int(target_mode)
                self.PKT_FRAMES_PER_BURST = {0: 2, 1: 4, 2: 8, 3: 16}[self.TARGET_MODE]
            else:
                self.TARGET_MODE = 0
                self.PKT_FRAMES_PER_BURST = int(
                    pkt_frames_per_burst
                    or self.effective_decode_profile.get("pkt_frames_per_burst")
                    or 1
                )

        elif platform_id == self._chfpga._PLATFORM_ID_MGK7MB_REV2:
            self.stream_ids = stream_ids or np.arange(16)
            self.NUMBER_OF_SUBFRAMES = int(self.effective_decode_profile.get("number_of_subframes", 1))

            if self.CAPTURE_ENGINE == "UCAP":
                profile_target_mode = self.effective_decode_profile.get("target_mode")
                if target_mode is None:
                    target_mode = profile_target_mode
                if target_mode is None or not 0 <= int(target_mode) < 4:
                    raise ValueError("UCAP target_mode must be in range 0-3.")
                self.TARGET_MODE = int(target_mode)
                self.PKT_FRAMES_PER_BURST = {0: 2, 1: 4, 2: 8, 3: 16}[self.TARGET_MODE]
            else:
                self.TARGET_MODE = 0
                desired_pkt_frames = (
                    pkt_frames_per_burst
                    if pkt_frames_per_burst is not None
                    else self.effective_decode_profile.get("pkt_frames_per_burst")
                )
                if desired_pkt_frames is None:
                    desired_pkt_frames = 1
                if not 1 <= int(desired_pkt_frames) <= 4:
                    raise ValueError(
                        f"pkt_frames_per_burst must be in range [1, 4] for ICE boards, got {desired_pkt_frames}."
                    )
                self.PKT_FRAMES_PER_BURST = int(desired_pkt_frames)

            if target_mode is not None and self.CAPTURE_ENGINE != "UCAP":
                raise ValueError(
                    f"target_mode parameter is not applicable to ICE PROBER capture (PLATFORM_ID: {self.PLATFORM_ID})"
                )
        else:
            raise NotImplementedError(f"Not implemented for platform {self.PLATFORM_ID}")

        self.BYTES_PER_FRAME = self.SAMPLES_PER_FRAME * self.BYTES_PER_SAMPLE
        self.BYTES_PER_SUBFRAME = self.BYTES_PER_FRAME // max(1, self.NUMBER_OF_SUBFRAMES)

        self.SCALER_CAP_DATA_TYPE = self.effective_decode_profile.get("scaler_cap_data_type")
        self.CAPTURE_SOURCE = self.effective_decode_profile.get("source")
        self.FFT_BINS_PER_WORD = int(self.effective_decode_profile.get("fft_bins_per_word", 1) or 1)
        self.PACKETS_PER_LOGICAL_FRAME = int(
            self.effective_decode_profile.get("packets_per_logical_frame", 1) or 1
        )
        self.DECODE_TRANSFORM = self.effective_decode_profile.get("decode_transform", "identity")

        if self.DECODE_TRANSFORM == "unsupported":
            raise NotImplementedError(
                f"Unsupported scaler capture mode {self.SCALER_CAP_DATA_TYPE} (reserved/unhandled)."
            )

        if self.PACKETS_PER_LOGICAL_FRAME < 1:
            raise ValueError("packets_per_logical_frame must be >= 1")

        if self.PKT_FRAMES_PER_BURST % self.PACKETS_PER_LOGICAL_FRAME != 0:
            if self.decode_strict:
                raise ValueError(
                    "pkt_frames_per_burst is not a multiple of packets_per_logical_frame "
                    f"({self.PKT_FRAMES_PER_BURST} vs {self.PACKETS_PER_LOGICAL_FRAME})."
                )

        self.LOGICAL_FRAMES_PER_BURST = int(
            self.effective_decode_profile.get("logical_frames_per_burst", 0) or 0
        )
        if self.LOGICAL_FRAMES_PER_BURST <= 0:
            self.LOGICAL_FRAMES_PER_BURST = max(
                1,
                self.PKT_FRAMES_PER_BURST // self.PACKETS_PER_LOGICAL_FRAME,
            )

        self.header_dtype = np.dtype(
            dict(
                names=["cookie", "stream_id", "frame_number", "slot_chan", "mode_subframe", "ts"],
                offsets=[0, 1, 1, 2, 3, 2],
                formats=["u1", ">u2", "u1", "u1", "u1", ">u8"],
            )
        )

        self.packet_dtype = np.dtype(
            [
                ("header", self.header_dtype, (1,)),
                ("data", np.uint8, (self.BYTES_PER_SUBFRAME,)),
            ]
        )

        self.PACKET_SIZE = self.packet_dtype.itemsize
        self.buf_struct = self.pr.buf[:, : self.PACKET_SIZE].view(self.packet_dtype)[:, 0]

        self.buf_cookie = self.buf_struct["header"]["cookie"][:, 0]
        self.buf_frame_stream_id = self.buf_struct["header"]["stream_id"][:, 0]
        self.buf_frame_number = self.buf_struct["header"]["frame_number"][:, 0]
        self.buf_slot_chan = self.buf_struct["header"]["slot_chan"][:, 0]
        self.buf_mode_subframe = self.buf_struct["header"]["mode_subframe"][:, 0]
        self.buf_ts = self.buf_struct["header"]["ts"][:, 0]
        self.buf_data = self.buf_struct["data"]
        self.ts_mask = np.uint64(0xFFFFFFFFFFFF)
        self.sid_mask = 0x0FFF

        n_packets = self.pr.n_packets
        self.pkt_stream_id = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_ts = np.empty((n_packets,), dtype=np.uint64)
        self.pkt_mode = np.empty((n_packets,), dtype=np.uint8)
        self.pkt_frame = np.empty((n_packets,), dtype=np.uint8)
        self.pkt_subframe = np.empty((n_packets,), dtype=np.uint8)
        self.pkt_six = np.empty((n_packets,), dtype=np.uint16)
        self.pkt_off = np.empty((n_packets,), dtype=np.uint32)

        self.TS_SLOTS = ring_buffer_size

        try:
            self.NCHAN = len(self.stream_ids)
        except TypeError as exc:
            raise ValueError(f"Stream IDs {self.stream_ids} must be an indexable and sized object") from exc

        self.six_map = np.full((1 << 12,), -1, dtype=np.int64)
        self.six_map[np.asarray(self.stream_ids, dtype=np.int64)] = np.arange(self.NCHAN)

        self.TS_EMPTY = np.uint64(0xFFFFFFFFFFFFFFFF)
        self.ts_array = np.full(self.TS_SLOTS, self.TS_EMPTY, dtype=np.uint64)

        self.raw_data = np.zeros(
            (self.TS_SLOTS, self.NCHAN, self.PKT_FRAMES_PER_BURST * self.BYTES_PER_FRAME),
            dtype=np.int8,
        )

        self.raw_data_count = np.zeros((self.TS_SLOTS, self.NCHAN), dtype=np.int64)
        self.raw_data_complete = np.zeros((self.TS_SLOTS, self.NCHAN), dtype=np.uint8)

        if format is None:
            format = self.effective_decode_profile.get("default_format", "8")

        self.configure_output(format=format)

        self.ybuf_ts: int = 0
        self.ybuf_data_count = np.empty_like(self.raw_data_count[0, ...])

        self._write_slot = 0
        self._read_slot = 0

        self.last_decode_meta: Dict[str, Any] = {}
        self.decode_profile_signature = tuple(
            (k, self.effective_decode_profile.get(k))
            for k in sorted(self.effective_decode_profile.keys())
        )

    def _get_format_spec(self):
        """Return input/output dtypes and scalar layout for current ``format``."""
        base = self.format.split("+", 1)[0]
        is_complex = "+" in self.format

        if base == "8":
            in_dtype = np.dtype(">i1")
            out_dtype = np.dtype(np.int8)
            scalar_bytes = 1
        elif base in ("14", "16"):
            in_dtype = np.dtype(">i2")
            out_dtype = np.dtype(np.int16)
            scalar_bytes = 2
        elif base == "32":
            in_dtype = np.dtype(">i4")
            out_dtype = np.dtype(np.int32)
            scalar_bytes = 4
        elif base == "64":
            in_dtype = np.dtype(">i8")
            out_dtype = np.dtype(np.int64)
            scalar_bytes = 8
        else:
            raise NotImplementedError(f"Data formatting not implemented for format {self.format}")

        if is_complex:
            out_dtype = np.dtype(np.complex128 if base == "64" else np.complex64)

        return in_dtype, out_dtype, scalar_bytes, is_complex, base

    def _legacy_nframes_for_format(self) -> int:
        _, _, _, is_complex, base = self._get_format_spec()
        nframes = self.PKT_FRAMES_PER_BURST
        if base in ("14", "16"):
            nframes //= 2
        elif base == "32":
            nframes //= 4
        elif base == "64":
            nframes //= 8
        if is_complex:
            nframes //= 2
        return max(1, int(nframes))

    def get_data_iter(self, verbose: Optional[int] = None) -> Iterator[Tuple[np.uint64, np.ndarray, np.ndarray]]:
        """
        Parse available packets and yield completed timestamp slots.

        The parser is engine-aware:
        - UCAP: validates mode/subframe and filters on ``TARGET_MODE``.
        - PROBER: ignores mode/subframe nibble for filtering.
        """
        verbose = verbose or self.verbose

        bix_mask = ((self.buf_cookie & self.PACKET_COOKIE_MASK) == self.PACKET_COOKIE) & (self.pr.pkt_len == self.PACKET_SIZE)
        bix = np.flatnonzero(bix_mask)

        if not bix.size:
            return

        np.bitwise_and(self.buf_ts, self.ts_mask, out=self.pkt_ts, where=bix_mask)
        np.bitwise_and(self.buf_frame_stream_id, self.sid_mask, out=self.pkt_stream_id, where=bix_mask)

        np.right_shift(self.buf_frame_number, 4, out=self.pkt_frame, where=bix_mask)
        np.bitwise_and(self.pkt_frame, 0x0F, out=self.pkt_frame, where=bix_mask)

        np.right_shift(self.buf_mode_subframe, 2, out=self.pkt_mode, where=bix_mask)
        np.bitwise_and(self.pkt_mode, 0x3, out=self.pkt_mode, where=bix_mask)
        np.bitwise_and(self.buf_mode_subframe, 0x3, out=self.pkt_subframe, where=bix_mask)

        if self.CAPTURE_ENGINE != "UCAP":
            self.pkt_mode[bix_mask] = 0
            self.pkt_subframe[bix_mask] = 0

        if verbose >= 2:
            print(f"get_data_iter: found {bix.size} packets matching the cookie and packet size")

        valid_ts = self.ts_array[self.ts_array != self.TS_EMPTY]
        min_ts = valid_ts.min() if valid_ts.size else np.uint64(0)

        if self.CAPTURE_ENGINE == "UCAP":
            bix = bix[self.pkt_mode[bix] == self.TARGET_MODE]
            if verbose >= 2:
                print(f"get_data_iter: {bix.size} left after filtering target mode")

        bix = bix[self.pkt_ts[bix] >= min_ts]
        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering old timestamps")

        bix = bix[self.six_map[self.pkt_stream_id[bix]] >= 0]
        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering stream IDs")

        if self.CAPTURE_ENGINE == "UCAP":
            bix = bix[(self.pkt_frame[bix] < self.PKT_FRAMES_PER_BURST) & (self.pkt_subframe[bix] < self.NUMBER_OF_SUBFRAMES)]
        else:
            bix = bix[self.pkt_frame[bix] < self.PKT_FRAMES_PER_BURST]

        if verbose >= 2:
            print(f"get_data_iter: {bix.size} left after filtering valid frame and subframe indices")

        bix_mask.fill(False)
        bix_mask[bix] = True

        if not bix.size:
            if verbose >= 2:
                print("get_data_iter: no packets matching the filters")
            return

        self.pkt_off[bix_mask] = (
            self.pkt_frame[bix_mask] * self.BYTES_PER_FRAME
            + self.pkt_subframe[bix_mask] * self.BYTES_PER_SUBFRAME
        ).astype(self.pkt_off.dtype, copy=False)

        ts_vals = self.pkt_ts[bix_mask]
        ts_sorted_ix = np.argsort(ts_vals, kind="mergesort")

        bix = bix[ts_sorted_ix]
        ts_vals = ts_vals[ts_sorted_ix]

        ts_unique, starts, counts = np.unique(ts_vals, return_index=True, return_counts=True)
        self.pkt_six[bix_mask] = self.six_map[self.pkt_stream_id[bix_mask]]

        subframe_ix = np.arange(self.BYTES_PER_SUBFRAME, dtype=self.pkt_off.dtype)

        expected_packets_per_channel = self.PKT_FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES

        for ts, s, count in zip(ts_unique, starts, counts):
            tix = bix[s:s + count]
            if ts not in self.ts_array:
                next_slot = (self._write_slot + 1) % self.TS_SLOTS

                if next_slot == self._read_slot:
                    if self.ts_array[next_slot] != self.TS_EMPTY:
                        if verbose >= 3:
                            print(f"get_data_iter: force yielding slot {next_slot} to free the buffer")
                        yield from self._yield_data(verbose=verbose)
                    else:
                        if verbose >= 3:
                            print(f"get_data_iter: slot {next_slot} is empty, advancing read pointer")
                        self._read_slot = (self._read_slot + 1) % self.TS_SLOTS

                if self.ts_array[next_slot] != self.TS_EMPTY:
                    raise RuntimeError(
                        "Ring invariant broken: next write slot is occupied."
                    )

                if self.raw_data_complete[next_slot].any() or self.raw_data_count[next_slot].any():
                    raise RuntimeError("Slot metadata not cleared for an empty slot.")

                self._write_slot = next_slot
                dest_slot = next_slot
                self.ts_array[dest_slot] = ts
            else:
                dest_slot_arr = np.flatnonzero(self.ts_array == ts)
                if dest_slot_arr.size != 1:
                    raise RuntimeError(f"Timestamp {int(ts)} maps to {dest_slot_arr.size} slots (expected 1).")
                dest_slot = int(dest_slot_arr[0])

            if verbose >= 3:
                print(f"get_data_iter: writing data to slot {dest_slot}")

            _si = self.pkt_six[tix]
            _off = self.pkt_off[tix]
            self.raw_data[dest_slot, _si[:, None], _off[:, None] + subframe_ix] = self.buf_data[tix]

            self.raw_data_count[dest_slot, :] += np.bincount(self.pkt_six[tix], minlength=self.NCHAN)
            self.raw_data_complete[dest_slot, :] = self.raw_data_count[dest_slot, :] == expected_packets_per_channel

            slot_complete = np.all(self.raw_data_complete, axis=1)
            while True:
                if verbose >= 3:
                    print(f"get_data_iter: checking read_slot {self._read_slot}")

                if self._read_slot == self._write_slot:
                    break
                if self.ts_array[self._read_slot] == self.TS_EMPTY:
                    self._read_slot = (self._read_slot + 1) % self.TS_SLOTS
                    continue
                if slot_complete[self._read_slot]:
                    yield from self._yield_data(verbose=verbose)
                    continue
                break

    def _yield_data(self, verbose: Optional[int] = None) -> Iterator[Tuple[np.uint64, np.ndarray, np.ndarray]]:
        """
        Yield one completed slot after formatting ``raw_data`` into ``ybuf_data``.

        In strict mode, multi-packet logical transforms (for example scaler
        mode 4/5 profiles) require complete packet coverage before yielding.
        """
        verbose = verbose or self.verbose
        ts = self.ts_array[self._read_slot]
        data_count = self.raw_data_count[self._read_slot]

        if self.decode_strict and self.PACKETS_PER_LOGICAL_FRAME > 1:
            if not np.all(self.raw_data_complete[self._read_slot]):
                raise RuntimeError(
                    "Incomplete multi-packet logical frame in strict decode mode. "
                    f"counts={data_count.tolist()}, expected={self.PKT_FRAMES_PER_BURST * self.NUMBER_OF_SUBFRAMES}"
                )

        raw_data_view = self.raw_data[self._read_slot].view()
        raw_data_view.setflags(write=False)
        ybuf_data_view = self._format_data_array(raw_data_view)

        self.last_decode_meta = {
            "decode_transform": self.DECODE_TRANSFORM,
            "packets_per_logical_frame": self.PACKETS_PER_LOGICAL_FRAME,
            "pkt_frames_per_burst": self.PKT_FRAMES_PER_BURST,
            "logical_frames_per_burst": self.LOGICAL_FRAMES_PER_BURST,
            "slot_complete": bool(np.all(self.raw_data_complete[self._read_slot])),
        }

        if verbose >= 2:
            print(f"Yielding {self.raw_data_count[self._read_slot]} packets")

        old_read_slot = self._read_slot
        self._read_slot = (self._read_slot + 1) % self.TS_SLOTS

        yield ts, ybuf_data_view, data_count

        self.ts_array[old_read_slot] = self.TS_EMPTY
        self.raw_data_count[old_read_slot, :].fill(0)
        self.raw_data_complete[old_read_slot, :].fill(0)

    def _allocate_ybuf_data(self, ncap: int = 1) -> np.ndarray:
        """Allocate output buffer according to format and logical frame count."""
        _, out_dtype, scalar_bytes, is_complex, _ = self._get_format_spec()

        total_bytes_per_channel = self.PKT_FRAMES_PER_BURST * self.BYTES_PER_FRAME
        if total_bytes_per_channel % scalar_bytes != 0:
            raise ValueError(
                f"Raw data size ({total_bytes_per_channel} bytes) is not divisible by scalar width {scalar_bytes}."
            )

        scalar_count = total_bytes_per_channel // scalar_bytes
        if is_complex:
            if scalar_count % 2:
                raise ValueError("Complex format requires an even number of scalar values.")
            sample_count = scalar_count // 2
        else:
            sample_count = scalar_count

        if self.effective_decode_profile:
            nframes = self.LOGICAL_FRAMES_PER_BURST
        else:
            nframes = self._legacy_nframes_for_format()

        if nframes <= 0:
            raise ValueError(
                f"Invalid number of logical frames per burst: {nframes}."
            )

        if sample_count % nframes != 0:
            if self.decode_strict:
                raise ValueError(
                    f"Cannot split {sample_count} samples into {nframes} logical frames without copying."
                )
            nframes = 1

        nsamples_per_frame = sample_count // nframes
        return np.empty(
            shape=(self.NCHAN, ncap * nframes, nsamples_per_frame),
            dtype=out_dtype,
        )

    def configure_output(self, format: Optional[str] = None) -> None:
        """Configure output format and resize ``ybuf_data`` if needed."""
        need_realloc = False
        if format is not None and format != getattr(self, "format", None):
            if format not in self.ALLOWED_FORMATS:
                raise ValueError(f"Format {format} is not supported. Allowed formats: {self.ALLOWED_FORMATS}")
            self.format = format
            need_realloc = True

        if need_realloc or not hasattr(self, "ybuf_data"):
            self.ybuf_data = self._allocate_ybuf_data()

    def _format_identity(self, out: np.ndarray, scalar_view: np.ndarray, is_complex: bool, base_format: str) -> None:
        """Default scalar/complex conversion without scaler-specific remapping."""
        if is_complex:
            out.real[...] = scalar_view[..., ::2]
            out.imag[...] = scalar_view[..., 1::2]
        elif base_format == "14":
            np.right_shift(scalar_view, 2, out=out)
        else:
            out[...] = scalar_view

    def _format_scaler_mode4(self, out: np.ndarray, scalar_view: np.ndarray) -> None:
        """Interleave even/odd packet-parity groups into one logical bin order."""
        if "+" in self.format:
            raise ValueError("Mode 4 decode requires scalar integer format (e.g. '16').")
        if self.PACKETS_PER_LOGICAL_FRAME != 2:
            raise RuntimeError("Mode 4 decode expects packets_per_logical_frame=2.")

        nchan, nframes, scalar_per_logical = scalar_view.shape
        if scalar_per_logical % 2:
            raise ValueError("Mode 4 expects even scalar count per logical frame.")

        per_packet_scalars = scalar_per_logical // 2
        src = scalar_view.reshape(nchan, nframes, 2, per_packet_scalars)

        out_pairs = out.reshape(nchan, nframes, -1, 2)
        even_pairs = src[:, :, 0, :].reshape(nchan, nframes, -1, 2)
        odd_pairs = src[:, :, 1, :].reshape(nchan, nframes, -1, 2)

        out_pairs[..., 0::2, :] = even_pairs
        out_pairs[..., 1::2, :] = odd_pairs

    def _format_scaler_mode5(self, out: np.ndarray, scalar_view: np.ndarray) -> None:
        """Reconstruct full-resolution scaler stream spanning ``2*B`` packets."""
        if "+" in self.format:
            raise ValueError("Mode 5 decode requires scalar integer format (e.g. '32').")

        packets = self.PACKETS_PER_LOGICAL_FRAME
        nchan, nframes, scalar_per_logical = scalar_view.shape
        if scalar_per_logical % packets:
            raise ValueError(
                f"Mode 5 cannot split {scalar_per_logical} scalars into {packets} packet slices."
            )

        per_packet_scalars = scalar_per_logical // packets
        if per_packet_scalars % 2:
            raise ValueError("Mode 5 expects interleaved Re/Im pairs per packet.")

        src = scalar_view.reshape(nchan, nframes, packets, per_packet_scalars)
        src_pairs = src.reshape(nchan, nframes, packets, per_packet_scalars // 2, 2)
        out_pairs = out.reshape(nchan, nframes, -1, 2)

        if packets != 2 * self.FFT_BINS_PER_WORD:
            raise RuntimeError(
                f"Mode 5 expects packets_per_logical_frame=2*B. Got packets={packets}, B={self.FFT_BINS_PER_WORD}."
            )

        stride = 2 * self.FFT_BINS_PER_WORD
        for frame_ix in range(packets):
            lane_ix = frame_ix // 2
            parity_ix = frame_ix & 1
            out_pairs[..., lane_ix + parity_ix * self.FFT_BINS_PER_WORD :: stride, :] = src_pairs[..., frame_ix, :, :]

    def _format_scaler_mode6(self, out: np.ndarray, scalar_view: np.ndarray) -> None:
        """Decode saturation markers into signed flags ``{-1, 0, +1}``."""
        if "+" in self.format:
            raise ValueError("Mode 6 decode requires scalar integer format.")

        out.fill(0)
        sat_max = np.iinfo(scalar_view.dtype).max
        sat_min = np.iinfo(scalar_view.dtype).min
        out[scalar_view == sat_max] = 1
        out[scalar_view == sat_min] = -1

    def _format_data_array(self, data: np.ndarray) -> np.ndarray:
        """Apply configured decode transform while copying into ``ybuf_data``."""
        out = self.ybuf_data
        out_view = out.view()

        in_dtype, _, _, is_complex, base_format = self._get_format_spec()
        scalar_view = data.view(in_dtype)

        nframes = out.shape[-2]
        nsamples = out.shape[-1]
        scalars_per_frame = nsamples * (2 if is_complex else 1)

        try:
            scalar_view = scalar_view.reshape(self.NCHAN, nframes, scalars_per_frame)
        except ValueError as exc:
            raise RuntimeError(
                "Unable to reshape raw capture data into configured output shape. "
                f"raw_shape={data.shape}, requested={(self.NCHAN, nframes, scalars_per_frame)}, format={self.format}"
            ) from exc

        if self.DECODE_TRANSFORM == "scaler_mode4":
            self._format_scaler_mode4(out=out, scalar_view=scalar_view)
        elif self.DECODE_TRANSFORM == "scaler_mode5":
            self._format_scaler_mode5(out=out, scalar_view=scalar_view)
        elif self.DECODE_TRANSFORM == "scaler_mode6":
            self._format_scaler_mode6(out=out, scalar_view=scalar_view)
        else:
            self._format_identity(out=out, scalar_view=scalar_view, is_complex=is_complex, base_format=base_format)

        out_view.setflags(write=False)
        return out_view

    def flush(self, flush_timeout: Optional[float] = None) -> None:
        """Flush UDP receiver and clear all ring-buffer slot state."""
        flush_kwargs = {}
        if flush_timeout is not None:
            flush_kwargs["timeout"] = flush_timeout
        self.pr.flush(**flush_kwargs)
        if self.verbose >= 2:
            print("Flushing RawDataProcessor ring buffer")
        self.ts_array.fill(self.TS_EMPTY)
        self.raw_data.fill(0)
        self.raw_data_count.fill(0)
        self.raw_data_complete.fill(0)
        self._read_slot = 0
        self._write_slot = 0
