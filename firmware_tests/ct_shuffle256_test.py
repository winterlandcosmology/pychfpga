import logging
import numpy as np
from pychfpga.fpga_array import FPGAArray
from pychfpga.fpga_firmware.chfpga.ct_engine.chan_bin_sel import pack_frame_flags_word
import pytest
import asyncio
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
from .module_test import TestModule
from . import ct_common

'''
Corner-turn shuffle256 crate tests
==================================
These tests verify the full three-level corner turn of a 16-board ICE crate
in mode 'shuffle256', from the backplane links up to byte-exact output
packets, using each board's corner-turn capture probe (ct_prober).

They are designed for REMOTE execution (the crate lives on another network):
every failure message is self-sufficient - it names the board slot, lane,
monitor and the physical cause hint - because no interactive debug session
is possible. Run stages in file order and stop at the first failure:

    PYTEST="<venv>/python -m pytest --xreport-config=config_ice_crate.yaml \
            --log-cli-level=INFO ct_shuffle256_test.py"
    $PYTEST -k test_crate_health           | tee s256_1_health.log
    $PYTEST -k test_stream_alignment       | tee s256_2_alignment.log
    $PYTEST -k test_packet_geometry        | tee s256_3_geometry.log
    $PYTEST -k test_corner_turn_shuffle256 | tee s256_4_algorithm.log
    $PYTEST -k test_frame_flags_shuffle256 | tee s256_5_flags.log
    $PYTEST -k test_gpu_link_feed_crate    | tee s256_6_gpu.log

Connecting the FPGAArray with mode='shuffle256' performs the entire crate
bring-up (init_corner_turn: per-board crossbars, backplane RX enable and TX
power, frequency-map verification, GPU reset, array sync). The tests are
pure observers of that operating point: they never reconfigure the corner
turn, never call board.reset(), and never call the board-local sync()
(which would break the array-wide frame alignment) - the only sync used is
the array-level one, after funcgen reprogramming.

shuffle256 output packet, per output lane k of the board in slot s (1-based)
of crate c: 4-word header (w0 = STREAM_ID16<<16 | 0x14CF with STREAM_ID16 =
0x2000 | c<<8 | (s-1)<<4 | k); per frame 8 bins x 64 data words (each bin =
256 bytes, ascending global channel 0..255, LSB byte first); then per frame
64 data-flags words and 16 frame-flags words (words 2L/2L+1 = source slots
2L+1/2L+2); one packet-flags trailer word (all-zero unless an upstream lane
error occurred). Totals: 597 words (flags on, 1 frame/packet), 1189 (2
frames), 517/1029 flags off. The bin at packet position p is the absolute
frequency bin 16p + (s-1); lane k carries positions {k + 8m}.
'''


class TestCTShuffle256(TestModule):

    CAPTURE_PERIOD = 0.01     # seconds between capture bursts
    S256_BINS_PER_FRAME = 8
    S256_CHANNELS_PER_BIN = 256
    SYNC_MAX_TIME_DIFF_DEFAULT = 100  # ns

    def get_logger(self):
        return self.logger

    def get_constants(_, board):
        # Live geometry that TestModule.board_conn cross-checks against the
        # yaml constants block. CAPTURE_WIDTH is a yaml pin with no live
        # source, so it is left out (as in fft_test).
        constants = NameSpace()
        constants.PLATFORM = board.mb.part_number
        constants.ADC_BITS_PER_SAMPLE = board.ADC_BITS_PER_SAMPLE
        constants.NUM_CHANNELS = board.NUMBER_OF_CHANNELIZERS
        constants.SAMPLES_PER_FRAME = board.ADC_SAMPLES_PER_FRAME
        constants.NUM_BINS = board.NUMBER_OF_FREQUENCY_BINS
        constants.BINS_PER_WORD = board.FFT_BINS_PER_WORD
        constants.CAPTURE_BUS_WIDTH = board.CAPTURE_BUS_WIDTH
        return constants

    def get_widths(_, test_config):
        widths = NameSpace({})
        widths.PLATFORM = test_config.platform[:3]
        widths.MAX_LANE = 7   # corner-turn output lanes are numbered 0-7
        widths.NUM_SLOTS = 16 # MGK7BP16 backplane slots are numbered 1-16
        return widths

    @pytest.fixture(scope='function', autouse=True)
    async def setup(self, xr, request):
        self.data = xr.data
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name)
        self.logger.setLevel(xr.config.test_config.logleveltest)
        # Gate BEFORE connecting: under the single-board/CRS configs these
        # tests must skip without ever touching (or connecting to) hardware.
        if self._connection_mode(xr) != 'shuffle256':
            pytest.skip("shuffle256 crate tests require connection_config mode='shuffle256' "
                        "(run with --xreport-config=config_ice_crate.yaml against a crate)")
        self.board = await self.board_conn(xr, request) # full crate bring-up on first connection
        self.array = self.ca
        self.data.NUM_SAMPLES = self.board.ADC_SAMPLES_PER_FRAME

    @pytest.fixture(scope='function')
    async def crate_setup(self, setup, xr):
        if getattr(self.board, 'crate', None) is None:
            pytest.skip("shuffle256 crate tests require boards connected through an ICE crate")
        if getattr(self.board, 'CT_PROBER', None) is None:
            pytest.skip("Corner-turn capture probe is not implemented in this firmware")
        # Park every board's capture engines once per session (safe: parking
        # only touches the probes, not the corner turn).
        if not xr.data.get('s256_probes_parked'):
            for mb in self.array.ib:
                ct_common.stop_captures(mb)
            xr.data.s256_probes_parked = True
        pre = {mb.slot: (int(mb.CT_PROBER.OVERRUN), int(mb.CT_PROBER._DATA_FIFO_OVERFLOW_STICKY))
               for mb in self.array.ib}
        assert all(v == (0, 0) for v in pre.values()), \
            f"probe error flags set before the test (slot: (overrun, overflow)): {pre}"
        yield
        for mb in self.array.ib:
            ct_common.stop_captures(mb)
        post = {mb.slot: (int(mb.CT_PROBER.OVERRUN), int(mb.CT_PROBER._DATA_FIFO_OVERFLOW_STICKY))
                for mb in self.array.ib}
        assert all(v == (0, 0) for v in post.values()), \
            f"probe error flags set after the test (slot: (overrun, overflow)): {post}"

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _slot_board(self, slot):
        for mb in self.array.ib:
            if mb.slot == slot:
                return mb
        pytest.skip(f"slot {slot} is not populated in this hardware map")

    def _populated_slots(self):
        return {mb.slot for mb in self.array.ib}

    def _stream_id16(self, mb, lane):
        crate, slot0 = mb.get_id()
        return 0x2000 | (int(crate) << 8) | (int(slot0) << 4) | int(lane)

    async def _ca_sync(self):
        limit = self.test_config.get('sync_max_time_difference', self.SYNC_MAX_TIME_DIFF_DEFAULT)
        await self.array.sync_async(max_sync_time_difference=limit)

    async def _require_dataflow(self, mb):
        # NO sync callable: a board-local sync would break the array-wide
        # frame alignment established at crate bring-up.
        cache = self.data.setdefault('s256_dataflow', {})
        await ct_common.require_ct_dataflow(mb, cache, self.get_logger(), sync=None,
                                      key=f'slot{mb.slot}')

    async def _capture(self, mb, lane, ncap=1, format=None):
        await self._require_dataflow(mb)
        return await ct_common.set_capture_ct(
            mb, self.array, ncap=ncap, lane=lane, period=self.CAPTURE_PERIOD, format=format,
            recv_verbose=self.test_config.loglevelrecv, logger=self.get_logger())

    @staticmethod
    def _decode_packet_flags(word):
        '''Decode the packet-flags trailer word into a readable error list.'''
        word = int(word) & 0xFFFFFFFF
        reasons = []
        for lane in range(8):
            if word & (1 << lane):
                reasons.append(f"CB3 input lane {lane} error")
        if word & (1 << 8):
            reasons.append("missing/short frame on a CB3 input")
        if word & (1 << 9):
            reasons.append("overlong frame on a CB3 input")
        if word & (1 << 10):
            reasons.append("CRC error flagged on a CB3 input")
        if word & (1 << 11):
            reasons.append("CB3 data/flags FIFO overflow")
        if word >> 12:
            reasons.append(f"CB2 packet flags (shifted): {word >> 12:#07x} "
                           f"(bits 12-27 = per-CB2-lane errors, 28-31 = missing/long/CRC/ovf)")
        return '; '.join(reasons) or 'no error bits set'

    def _divergence_report(self, slot, lane, mismatch_idx, got, want):
        '''Explain the first data mismatch in corner-turn coordinates.'''
        idx = int(mismatch_idx[0])
        frame, r = divmod(idx, 2048)
        bin_index, g = divmod(r, 256)
        p = lane + 8 * bin_index
        abs_bin = 16 * p + (slot - 1)
        src_slot, src_ch = divmod(g, 16)
        pos = np.asarray(mismatch_idx) % 2048
        slots, counts = np.unique((pos % 256) // 16 + 1, return_counts=True)
        hist = ', '.join(f'slot {int(s)}: {int(c)}' for s, c in zip(slots, counts))
        return (f"[slot {slot} lane {lane}] first mismatch at sample {idx}: "
                f"frame {frame}, packet bin position {p} (absolute bin {abs_bin}), "
                f"global channel {g} = source slot {src_slot + 1} channel {src_ch}; "
                f"got {got[idx]} want {want[idx]}; "
                f"neighbours got={got[max(idx - 2, 0):idx + 3]} want={want[max(idx - 2, 0):idx + 3]}; "
                f"{len(mismatch_idx)} mismatching samples by source slot: {hist}")

    def _gen_s256_waveform(self, waveform, slot, ch):
        '''One deterministic frame-periodic funcgen pattern per (slot, channel).

        'slot_channel_id': the decoded complex value is (channel, slot-1)
        everywhere - every one of the 256 global channels becomes a unique
        constant byte on the wire, pinpointing any routing error.

        'ramp': per-source phase-offset ramps (offset = 16*ch + slot-1,
        unique across all 256 sources) - the value varies per bin, so bin
        ordering errors show up too.
        '''
        num_samples = int(self.data.NUM_SAMPLES)
        samples = np.arange(num_samples)
        if waveform == 'slot_channel_id':
            data = np.where(samples % 2 == 0, ch << 4, (slot - 1) << 4)
        elif waveform == 'ramp':
            data = (samples + 16 * ch + (slot - 1)) % 256
        else:
            raise ValueError(f"Unknown shuffle256 test waveform '{waveform}'")
        return data.astype(np.uint8)

    async def _program_waveform(self, waveform, xr):
        '''Program every board's funcgens with the waveform (session-cached).'''
        if xr.data.get('s256_stimulus') == waveform:
            return
        logger = self.get_logger()
        logger.info(f"Programming all {len(self.array.ib)} boards with waveform '{waveform}'")
        for mb in self.array.ib:
            mb.set_channelizer(data_source=None, fft_bypass=1, scaler_bypass=1,
                               scaler_bypass_data_type=0, offset_binary_encoding=False,
                               local_sync=False)
            for ch in range(16):
                mb.set_funcgen(function='arb', channels=[ch], left_shift=0, right_shift=0,
                               data=self._gen_s256_waveform(waveform, mb.slot, ch))
        await self._ca_sync() # ONE array-wide sync; never a board-local one
        xr.data.s256_stimulus = waveform
        xr.data.pop('s256_reference', None)

    def _shuffle256_reference(self, waveform, xr):
        '''Compute the expected decoded output of every (board, lane) via the
        driver's own crossbar models (get_shuffle_output), session-cached.
        '''
        cached = xr.data.get('s256_reference')
        if cached is not None and cached[0] == waveform:
            return cached[1]
        chan_map = {}
        for mb in self.array.ib:
            for (crate, slot0, ch) in mb.get_channel_ids():
                chan_bytes = self._gen_s256_waveform(waveform, mb.slot, ch)
                re4 = (chan_bytes[0::2].view(np.int8) >> 4).astype(np.float32)
                im4 = (chan_bytes[1::2].view(np.int8) >> 4).astype(np.float32)
                chan_map[(crate, slot0, ch)] = re4 + 1j * im4
        ref = self.array.get_shuffle_output(chan_map)
        xr.data.s256_reference = (waveform, ref)
        return ref

    @staticmethod
    def _apply_offset_binary(ref):
        '''Model the receiver's offset-binary correction on a two's-complement
        reference: the wire carries two's-complement nibbles, but with the
        (never-programmed, power-on default 1) USE_OFFSET_BINARY header flag
        the receiver XORs 0x88 before sign-extending, which flips each
        nibble's sign bit: s -> s-8 for s >= 0, s+8 for s < 0.
        '''
        re = ref.real
        im = ref.imag
        re = np.where(re >= 0, re - 8, re + 8)
        im = np.where(im >= 0, im - 8, im + 8)
        return (re + 1j * im).astype(np.complex64)

    def _split_s256_packet(self, words, geometry):
        '''Split raw shuffle256 packet words into their sections.

        words: (npackets, words_per_packet) int64. Layout: 4-word header,
        all frames' data, all frames' data-flags, one 16-word frame-flags
        block per frame, one trailer word.
        '''
        fpp = geometry['frames_per_packet']
        pos = 4
        header = words[:, :pos]
        data = words[:, pos:pos + fpp * 512]
        pos += fpp * 512
        if geometry['send_flags']:
            data_flags = words[:, pos:pos + fpp * 64].reshape(words.shape[0], fpp, 64)
            pos += fpp * 64
            frame_flags = words[:, pos:pos + fpp * 16].reshape(words.shape[0], fpp, 16)
            pos += fpp * 16
        else:
            data_flags = np.zeros((words.shape[0], fpp, 0), np.int64)
            frame_flags = np.zeros((words.shape[0], fpp, 0), np.int64)
        trailer = words[:, pos]
        assert pos + 1 == geometry['words_per_packet']
        return NameSpace(dict(header=header, data=data, data_flags=data_flags,
                              frame_flags=frame_flags, trailer=trailer))

    def _expected_frame_flags(self, mb, lane, masks):
        '''Compose the expected 16 frame-flags words of output `lane` through
        the driver's flag-layout models against the live registers.

        masks: {source_slot: 16-bit per-channel ADC mask}.
        '''
        cb1_word = {slot: pack_frame_flags_word(
            list(range(16)), {ch: ((mask >> ch) & 1, 0) for ch in range(16)})
            for slot, mask in masks.items()}
        # CB2 post-remap input lane j carries slot j+1's stream (the remap
        # contract verified by test_stream_alignment).
        cb2 = mb.CT.CROSSBAR2
        cb2_in = {j: dict(data_flags=None, frame_flags=[cb1_word.get(j + 1, 0)])
                  for j in range(16)}
        cb2_out = {}
        n_out = int(cb2.NUMBER_OF_OUTPUT_LANES) // int(cb2.NUMBER_OF_BIN_SEL)
        for bs_number, bs in enumerate(cb2.BIN_SEL):
            for sublane, d in bs.map_flags(cb2_in).items():
                cb2_out[n_out * bs_number + sublane] = d
        # CB3: bin-sel input lane L takes CB2 output lane get_lane_map()[L];
        # output lane k is bin selector k, sublane 0.
        cb3 = mb.CT.CROSSBAR3
        lane_map3 = cb3.get_lane_map()
        cb3_in = {L: dict(data_flags=None, frame_flags=cb2_out[lane_map3[L]]['frame_flags'])
                  for L in range(8)}
        return cb3.BIN_SEL[lane].map_flags(cb3_in)[0]['frame_flags']

    # ------------------------------------------------------------------
    # tests (file order == required remote run order)
    # ------------------------------------------------------------------

    async def _measure_crate_health(self, logger, boards, populated):
        '''One full health pass: clear all monitors, let them re-accumulate on
        live traffic for 0.5 s, then read every stage. Returns the failure list.

        The measurement is :meth:`BCTArrayMethods.measure_crate_health`, the
        same method the crate pre-deployment QC suite calls, so both judge
        the crate by the same criteria.
        '''
        return await self.array.measure_crate_health(boards, populated, logger)

    @pytest.mark.ct_shuffle256
    async def test_crate_health(self, crate_setup, xr):
        '''
        Verify the backplane links and both shuffle crossbars are error-free on every board.

        Checks, per board: 64b66b BLOCK_LOCK on every backplane GTX whose
        source slot is populated (RX_RESETDONE is deliberately not used - it
        is timer-based and asserts with no cable); zero CRC errors, RX FIFO
        overflows and frame-length variance on the PCB lanes; all CROSSBAR2
        and CROSSBAR3 per-lane aligner monitors clear (MISSING_FRAME is the
        real lane-absent flag; the DATA_TIMEOUT/HAD_TIMEOUT registers are
        not driven by the firmware and are not consulted); zero packet
        error counters; and INPUT/ALIGN/OUTPUT frame counters advancing.

        History: through 2026-09 this test failed on every bring-up with
        hundreds of live CROSSBAR2 aligner errors. Root causes, both fixed:
        the software-forced SOF window of 55 sat on the measured lane-skew
        tail (raised to 100, see test_alignment_window_margin), and the
        pre-sep24 firmware polluted the packet-flags trailer, saturating
        CROSSBAR3's packet-error counter unconditionally. A settle-and-retry
        poll that lived here during the investigation was removed once the
        first pass became genuinely clean.
        '''
        logger = self.get_logger()
        boards = sorted(self.array.ib, key=lambda m: m.slot)
        populated = self._populated_slots()

        failures = await self._measure_crate_health(logger, boards, populated)
        if failures:
            try:
                self.array.print_crossbar2_frame_info()
            except Exception as exc: # diagnostics must never mask the failure
                logger.warning(f"print_crossbar2_frame_info failed: {exc!r}")
        assert not failures, f"{len(failures)} crate health problem(s):\n" + '\n'.join(failures)

    @pytest.mark.ct_shuffle256
    async def test_alignment_window_margin(self, crate_setup, xr):
        '''
        Measure every CROSSBAR2 input lane's arrival skew against the aligner
        reference (lane 0) and require a guard margin below the programmed
        SOF window.

        Regression tripwire distilled from the 2026-09-21/22 frame-loss
        investigation: at the historical window of 55 the measured skew tails
        (44-57 ct_clk cycles across 240 lanes) sat exactly on the acceptance
        edge ~W+2-L, so the aligner silently discarded ~1% of frames while
        every link monitor read clean. A healthy margin makes that failure
        mode visible long before it costs data.

        DELAY_CAPTURE protocol (single shared counter): select the lane
        FIRST, then pulse LANE_MONITOR_RESET (arms the capture and clears the
        monitor stats), wait, read. value = skew+1; 0xFFFF = never started;
        values near the packet period (~1250) mean the lane leads lane 0.
        '''
        MIN_MARGIN_CYCLES = 20   # required guard below the window edge
        SAMPLES_PER_LANE = 20
        ALIGN_FIFO_LATENCY = 3   # L: align-FIFO write-to-valid latency bound
        logger = self.get_logger()
        boards = sorted(self.array.ib, key=lambda m: m.slot)
        populated = self._populated_slots()
        failures = []
        for mb in boards:
            cb = mb.CT.CROSSBAR2
            w = int(cb.SOF_WINDOW_STOP)
            boundary = w + 2 - ALIGN_FIFO_LATENCY
            worst = {}
            for lane in range(1, 16):
                tx_slot, _ = mb.crate.get_matching_tx((mb.slot, lane))
                if tx_slot not in populated:
                    continue
                cb.LANE_MONITOR_SEL = lane
                vals = []
                for _ in range(SAMPLES_PER_LANE):
                    cb.LANE_MONITOR_RESET = 1
                    cb.LANE_MONITOR_RESET = 0
                    v = int(cb.DELAY_CAPTURE)
                    # exclude never-started (0xFFFF), saturated (0xFFFE) and
                    # early-lane (~packet period) readings
                    if v not in (0xFFFF, 0xFFFE) and v < 600:
                        vals.append(v - 1)   # value = skew + 1
                if vals:
                    worst[lane] = max(vals)
            cb.LANE_MONITOR_SEL = 0
            if not worst:
                failures.append(f"[slot {mb.slot}] DELAY_CAPTURE returned no finite "
                                f"skew samples - no dataflow, or the capture "
                                f"protocol is broken on this firmware")
                continue
            lane_max = max(worst, key=worst.get)
            margin = boundary - worst[lane_max]
            logger.info(f"[slot {mb.slot}] window={w} boundary~{boundary} "
                        f"worst skew={worst[lane_max]} (lane {lane_max}) "
                        f"margin={margin}")
            if margin < MIN_MARGIN_CYCLES:
                failures.append(
                    f"[slot {mb.slot}] SOF-window margin {margin} cycles < "
                    f"{MIN_MARGIN_CYCLES} (window {w}, worst lane {lane_max} "
                    f"skew {worst[lane_max]}): the aligner will silently "
                    f"discard frames when jitter eats the remainder - raise "
                    f"cb2_sof_window_stop or investigate the slow lane")
        assert not failures, \
            f"{len(failures)} window-margin problem(s):\n" + '\n'.join(failures)

    @pytest.mark.ct_shuffle256
    async def test_stream_alignment(self, crate_setup, xr):
        '''
        Verify every CROSSBAR2 input lane carries the right board's stream, time-aligned.

        capture_stream_id()/capture_frame_number() are indexed by *physical*
        (pre-remap) RX lane - LANE_MONITOR_SEL selects the input side of the
        remap, as established by the 2026-09-18 on-hardware discriminator
        (256/256 pre-remap vs 16/256 post-remap across all 16 boards). So the
        stream captured on physical lane lane_map[j] must identify source
        slot j+1, and the captured frame numbers of all 16 lanes must agree
        within a small tolerance - together this proves the backplane mesh,
        the programmed lane map and the array-wide frame alignment, and it
        cross-validates the get_lane_map() driver fix on silicon.
        '''
        logger = self.get_logger()
        spread_limit = int(self.test_config.get('stream_frame_spread', 2))
        failures = []
        for mb in sorted(self.array.ib, key=lambda m: m.slot):
            lane_map = mb.CT.CROSSBAR2.get_lane_map()
            sids = mb.CT.CROSSBAR2.capture_stream_id()
            logger.info(f"[slot {mb.slot}] CROSSBAR2 lane_map={lane_map} "
                        f"stream_ids={[hex(int(s)) for s in sids]}")
            for j in range(16):
                tx_slot, tx_lane = mb.crate.get_matching_tx((mb.slot, int(lane_map[j])))
                if tx_slot != j + 1:
                    failures.append(f"[slot {mb.slot}] CROSSBAR2 lane map entry {j} selects "
                                    f"physical RX lane {lane_map[j]}, which slot {tx_slot} "
                                    f"feeds - expected slot {j + 1}")
                    continue
                if tx_slot not in self._populated_slots():
                    continue
                expected = ((tx_slot - 1) << 4) | tx_lane
                # sids is physical-lane indexed: the stream feeding post-remap
                # lane j was captured on physical lane lane_map[j].
                sid = int(sids[int(lane_map[j])])
                if sid != expected:
                    failures.append(
                        f"[slot {mb.slot}] CROSSBAR2 input lane {j}: stream id {sid:#04x} "
                        f"decodes to slot {(sid >> 4) + 1} lane {sid & 0xF}, expected "
                        f"slot {tx_slot} lane {tx_lane} ({expected:#04x}) - "
                        f"miswired or mis-mapped backplane link")
            frames = np.asarray(await mb.CT.CROSSBAR2.capture_frame_number(), np.int64)
            # 8-bit counters: measure the spread modulo 256
            centered = ((frames - frames[0]) % 256 + 128) % 256 - 128
            spread = int(centered.max() - centered.min())
            logger.info(f"[slot {mb.slot}] CROSSBAR2 frame numbers={frames.tolist()} "
                        f"spread={spread}")
            if spread > spread_limit:
                # frames is physical-lane indexed too; invert the lane map to
                # name the slot whose stream sits on the worst physical lane.
                worst = int(np.argmax(np.abs(centered)))
                worst_slot = next((j + 1 for j in range(16)
                                   if int(lane_map[j]) == worst), None)
                failures.append(f"[slot {mb.slot}] CROSSBAR2 frame-number spread {spread} > "
                                f"{spread_limit} across input lanes (worst: physical lane "
                                f"{worst} = slot {worst_slot}) - boards are not "
                                f"frame-aligned; check the crate sync")
        assert not failures, f"{len(failures)} alignment problem(s):\n" + '\n'.join(failures)

    @pytest.mark.ct_shuffle256
    async def test_packet_geometry(self, s256_slot, s256_lane, crate_setup, xr):
        '''
        Verify the shuffle256 packet geometry, identity and integrity on the wire.

        Captures raw packets and checks the CB3-derived geometry readback,
        the exact packet length, the stream-id identity (crate/slot/lane),
        the header flag bits, the inner-timestamp progression and the
        all-zero packet-flags trailer (whose decoded bits localize any
        upstream lane error).

        Parameters:
            s256_slot (int): backplane slot of the board to capture (1-16)

            s256_lane (int): corner-turn output lane to capture (0-7)
        '''
        logger = self.get_logger()
        mb = self._slot_board(s256_slot)
        ts, raw, counts, geometry = await self._capture(mb, s256_lane, ncap=2, format='32')
        logger.info(f"[slot {s256_slot} lane {s256_lane}] geometry={geometry}")
        assert geometry['geometry_source'] == 'CB3', \
            "geometry not derived from CROSSBAR3: the corner turn is not in an active shuffle mode"
        fpp = geometry['frames_per_packet']
        send_flags = geometry['send_flags']
        expected_words = 4 + fpp * 512 + (fpp * 80 if send_flags else 0) + 1
        assert geometry['bins_per_frame'] == self.S256_BINS_PER_FRAME
        assert geometry['channels_per_bin'] == self.S256_CHANNELS_PER_BIN
        assert geometry['words_per_packet'] == expected_words
        assert raw.shape[2] == expected_words, \
            f"captured packet is {raw.shape[2]} words, geometry says {expected_words}"
        words = raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF
        w0, w1 = int(words[0, 0]), int(words[0, 1])
        logger.info(f"[slot {s256_slot} lane {s256_lane}] w0={w0:#010x} w1={w1:#010x}")
        assert w0 & 0xFFFF == 0x14CF, "inner CT header cookie/version/length"
        assert (w0 >> 16) == self._stream_id16(mb, s256_lane), \
            (f"stream id {(w0 >> 16):#06x} does not identify crate/slot/lane "
             f"{self._stream_id16(mb, s256_lane):#06x}")
        assert (w1 >> 31) & 1 == 0, "bypass header bit"
        assert (w1 >> 30) & 1 == int(send_flags), "send_flags header bit"
        # CB2/CB3 never program the header bins/channels/four_bits fields;
        # they read zero by design and are not asserted here.
        # The trailer is a real error register on sep24+ firmware (earlier
        # builds polluted it with stale sublane-FIFO data) and must be zero
        # on a clean datapath.
        for row in range(2):
            trailer = int(words[row, -1])
            assert trailer == 0, \
                (f"[slot {s256_slot} lane {s256_lane}] packet-flags trailer "
                 f"{trailer:#010x}: {self._decode_packet_flags(trailer)}")
        ts_delta = int(ts[1]) - int(ts[0])
        w3_delta = (int(words[1, 3]) - int(words[0, 3])) & 0xFFFFFFFF
        assert ts_delta > 0 and w3_delta == ts_delta * fpp, \
            (f"inner timestamp advanced by {w3_delta} over {ts_delta} captured packets "
             f"(expected {ts_delta * fpp}) - frame numbering is inconsistent")
        assert int(mb.CT_PROBER.OVERRUN) == 0
        assert int(mb.CT_PROBER._DATA_FIFO_OVERFLOW_STICKY) == 0

    @pytest.mark.ct_shuffle256
    async def test_output_lane_framing(self, crate_setup, xr):
        '''
        Every corner-turn output lane emits exactly one well-formed header.

        Regression for the pre-sep24 shared-strobe defect: all 8 GPU lanes
        (and the prober tap) were sampled with lane 0's TVALID/TLAST, so
        lane k's packets carried 8k+1 copies of header word 0 up front and
        lost their last 8k words (flags region + trailer). The parametrized
        geometry stage covers lanes 0 and 7 on every slot; this test sweeps
        all 8 lanes on representative slots, capturing lanes 0 and 7
        back-to-back first (lanes legitimately emit up to ~230 ns apart on
        per-lane strobes - packets associate by header timestamp, never by
        simultaneous arrival).
        '''
        logger = self.get_logger()
        failures = []
        for slot in (1, 8, 12):
            mb = self._slot_board(slot)
            for lane in (0, 7, 1, 2, 3, 4, 5, 6):
                ts, raw, counts, geometry = await self._capture(mb, lane, ncap=2, format='32')
                words = raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF
                for p in range(words.shape[0]):
                    row = words[p]
                    w0 = int(row[0])
                    dups = 1
                    while dups < row.shape[0] and int(row[dups]) == w0:
                        dups += 1
                    if not np.any(row):
                        # An all-zero buffer is an empty capture, not a framing
                        # defect: the probe sent nothing, or the read picked the
                        # wrong receiver channel (the channel axis carries one
                        # column per armed probe - see
                        # ct_common.select_board_channel). Keep it distinct from
                        # the strobe check below, whose signature is a *real*
                        # header word repeated 8k+1 times.
                        failures.append(
                            f"[slot {slot} lane {lane}] packet {p}: capture buffer "
                            f"is entirely zero ({row.shape[0]} words) - no packet "
                            f"reached this row (check the probe is armed and that "
                            f"the capture read this board's receiver channel); "
                            f"this is NOT the shared-strobe defect")
                        continue
                    if w0 & 0xFFFF != 0x14CF or dups != 1:
                        failures.append(
                            f"[slot {slot} lane {lane}] packet {p}: header word "
                            f"{w0:#010x} repeated {dups}x - per-lane output "
                            f"strobes broken (pre-sep24 shared-strobe defect)")
                        continue
                    if int(row[1]) != 0x42000000:
                        failures.append(f"[slot {slot} lane {lane}] packet {p}: "
                                        f"w1={int(row[1]):#010x}, expected 0x42000000")
                    if int(row[-1]) != 0:
                        failures.append(f"[slot {slot} lane {lane}] packet {p}: "
                                        f"trailer {int(row[-1]):#010x}: "
                                        f"{self._decode_packet_flags(int(row[-1]))}")
        assert not failures, \
            f"{len(failures)} lane-framing problem(s):\n" + '\n'.join(failures)

    @pytest.mark.ct_shuffle256
    async def test_trailer_error_propagation(self, crate_setup, xr):
        '''
        Force aligner drops and verify the error plumbing reports them.

        Shrinks the CROSSBAR2 SOF window to 50 (measured to drop frames on
        most lanes: the crate's lane arrival skews are 44-57 ct_clk cycles),
        then requires: CB2's PACKET_ERROR_CTR counts, CB3's tracks it (its
        SHUFFLE_FLAGS_MASK includes CB2's summary bits), and the GPU-visible
        packet trailer carries bit 28 (CB2 missing/short summary) plus
        per-CB2-input-lane bits (12-27) naming the dropped lanes. The window
        is restored in a finally and both counters must then stay zero -
        proving the deliberate errors leave no residue.
        '''
        logger = self.get_logger()
        boards = sorted(self.array.ib, key=lambda m: m.slot)
        mb = self._slot_board(1)
        w_ok = int(mb.CT.CROSSBAR2.SOF_WINDOW_STOP)

        def perr_sum(cb):
            s = 0
            for sel in range(8):
                cb.LANE_MONITOR_SEL = sel
                s += int(cb.PACKET_ERROR_CTR)
            cb.LANE_MONITOR_SEL = 0
            return s

        def clear_all():
            for b in boards:
                for cb in (b.CT.CROSSBAR2, b.CT.CROSSBAR3):
                    cb.LANE_MONITOR_RESET = 1
                    cb.LANE_MONITOR_RESET = 0

        try:
            for b in boards:
                b.CT.CROSSBAR2.SOF_WINDOW_STOP = 50
            clear_all()
            await asyncio.sleep(2)
            cb2, cb3 = perr_sum(mb.CT.CROSSBAR2), perr_sum(mb.CT.CROSSBAR3)
            logger.info(f"[slot {mb.slot}] deliberate-drop counters: CB2={cb2} CB3={cb3}")
            assert cb2 > 0, \
                "shrinking the SOF window to 50 produced no CB2 packet errors - " \
                "the deliberate-drop stimulus did not work (did the skew shrink?)"
            assert cb3 > 0, \
                f"CB3 PACKET_ERROR_CTR did not track CB2's ({cb3} vs {cb2}): " \
                f"CB2 summary bits are not propagating through the trailer"
            ts, raw, counts, geometry = await self._capture(mb, 0, ncap=2, format='32')
            words = raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF
            trailers = [int(words[p, -1]) for p in range(words.shape[0])]
            logger.info(f"[slot {mb.slot}] trailers under deliberate drops: "
                        f"{[hex(t) for t in trailers]}")
            flagged = [t for t in trailers if t]
            assert flagged, \
                f"counters count but no captured trailer carries error bits: " \
                f"{[hex(t) for t in trailers]}"
            for t in flagged:
                assert t & (1 << 28), \
                    (f"trailer {t:#010x} lacks bit 28 (CB2 missing/short): "
                     f"{self._decode_packet_flags(t)}")
                assert t & 0x0FFFF000, \
                    (f"trailer {t:#010x} names no CB2 input lane (bits 12-27): "
                     f"{self._decode_packet_flags(t)}")
        finally:
            for b in boards:
                b.CT.CROSSBAR2.SOF_WINDOW_STOP = w_ok
            await asyncio.sleep(0.5)
            clear_all()
        await asyncio.sleep(5)
        resid2 = sum(perr_sum(b.CT.CROSSBAR2) for b in boards)
        resid3 = sum(perr_sum(b.CT.CROSSBAR3) for b in boards)
        assert resid2 == 0 and resid3 == 0, \
            (f"packet errors persist after restoring SOF_WINDOW_STOP={w_ok} "
             f"(CB2={resid2}, CB3={resid3})")

    @pytest.mark.ct_shuffle256
    async def test_prober_arming(self, crate_setup, xr):
        '''
        Every captured packet must begin at a packet boundary.

        Regression for the pre-sep24 ct_prober arming race: the capture
        window could open on the TLAST cycle of an in-flight packet, so
        ~1 capture in 30 came back shifted by one word (word 0 held the
        previous packet's trailer instead of the header). sep24 arms bursts
        only on the first word after a TLAST. 200 packets across lanes 0
        and 7 put the old failure rate at ~7 expected hits.
        '''
        logger = self.get_logger()
        mb = self._slot_board(1)
        bad, total = [], 0
        for i in range(20):
            lane = (0, 7)[i % 2]
            ts, raw, counts, geometry = await self._capture(mb, lane, ncap=10, format='32')
            words = raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF
            for p in range(words.shape[0]):
                total += 1
                w0 = int(words[p, 0])
                if w0 & 0xFFFF != 0x14CF:
                    bad.append(f"iter {i} lane {lane} packet {p}: w0={w0:#010x}")
        logger.info(f"[slot {mb.slot}] prober arming: {total} packets, "
                    f"{len(bad)} not on a packet boundary")
        assert total >= 200, f"only {total} packets captured; need >= 200"
        assert not bad, (f"{len(bad)}/{total} captured packets do not start at a "
                         f"packet boundary (arming race):\n" + '\n'.join(bad[:10]))

    @pytest.mark.ct_shuffle256
    async def test_corner_turn_shuffle256(self, s256_slot, s256_lane, waveform, crate_setup, xr):
        '''
        Verify the three-level shuffle256 corner turn byte-exactly against a host reference.

        Every one of the 256 channelizer streams in the crate is fed a
        distinct deterministic waveform; the captured output of the selected
        (board, lane) is compared sample for sample with the prediction of
        the driver's own crossbar models (get_shuffle_output), which are
        thereby cross-validated against silicon. A mismatch is reported in
        corner-turn coordinates (frame, bin, source slot/channel) with a
        histogram of offending source slots.

        Parameters:
            s256_slot (int): backplane slot of the board to capture (1-16)

            s256_lane (int): corner-turn output lane to capture (0-7)

            waveform (str): 'slot_channel_id' or 'ramp' (see _gen_s256_waveform)
        '''
        logger = self.get_logger()
        mb = self._slot_board(s256_slot)
        await self._program_waveform(waveform, xr)
        ts, data, counts, geometry = await self._capture(mb, s256_lane)
        np.testing.assert_array_equal(counts, [[1]])
        fpp = geometry['frames_per_packet']
        assert data.shape == (1, 1, fpp * 2048)

        ref = self._shuffle256_reference(waveform, xr)
        crate, slot0 = mb.get_id()
        ref_lane = np.asarray(ref[(crate, slot0, s256_lane)]['data'], np.complex64)
        assert ref_lane.size == 2048, \
            f"reference model produced {ref_lane.size} samples per frame, expected 2048"
        if geometry['use_offset_binary']:
            ref_lane = self._apply_offset_binary(ref_lane)
        ref_tiled = np.tile(ref_lane, fpp)

        received = data[0, 0]
        mismatch_idx = np.nonzero(received != ref_tiled)[0]
        logger.info(f"[slot {s256_slot} lane {s256_lane}] waveform={waveform} "
                    f"samples={received.size} mismatches={len(mismatch_idx)}")
        if len(mismatch_idx):
            logger.error(self._divergence_report(s256_slot, s256_lane, mismatch_idx,
                                                 received, ref_tiled))
        self.plot_and_test(xr, received, ref_tiled,
                           f'Corner turn shuffle256 (slot={s256_slot}, lane={s256_lane}, '
                           f'waveform={waveform})')

    @pytest.mark.ct_shuffle256
    async def test_frame_flags_shuffle256(self, s256_slot, s256_lane, crate_setup, xr):
        '''
        Verify the per-source-board frame-flags words survive the shuffle bit-exactly.

        Each board injects a distinct per-channel ADC-overflow mask
        (1 << (slot-1)) through its funcgen overflow buffers; the output
        packet's 16 frame-flags words (word w = source slot w+1) must then
        carry exactly those masks, after a negative phase proves they are
        all-zero without injection. The expectation is composed twice: once
        directly and once through the driver's flag-layout models against
        the live registers, which are asserted to agree first.

        The per-bin data-flags words are logged but not asserted: the
        channelizer TUSER truncation defect documented in
        ct_prober_test.test_data_flags_as_built means they carry no scaler
        flags on current firmware. An FFT OVERFLOW_FORCE phase is omitted
        deliberately: the FFT bit rides the identical concatenation path,
        and per-channel force loops across 16 boards multiply remote
        runtime and failure modes.

        Parameters:
            s256_slot (int): backplane slot of the board to capture (1-16)

            s256_lane (int): corner-turn output lane to capture (0-7)
        '''
        logger = self.get_logger()
        mb = self._slot_board(s256_slot)
        geometry = mb.CT_PROBER.get_packet_geometry(s256_lane)
        if not geometry['send_flags']:
            pytest.skip("the crate was initialized flags-off; the frame-flags test needs "
                        "the (default) flags-on corner-turn configuration")
        zeros = np.zeros(int(self.data.NUM_SAMPLES), np.uint8)
        inject = zeros.copy()
        inject[0] = 1 # one injected ADC-overflow event per frame

        # Phase 1 (negative): no injections anywhere -> all-zero frame flags.
        for mbx in self.array.ib:
            mbx.set_channelizer(data_source='frame8', fft_bypass=1, scaler_bypass=1,
                                scaler_bypass_data_type=0, offset_binary_encoding=False,
                                funcgen_left_shift=0, funcgen_right_shift=0,
                                overflow_buffer=zeros, local_sync=False)
        await self._ca_sync()
        xr.data.pop('s256_stimulus', None) # the algorithm stimulus is gone now
        ts, raw, counts, geometry = await self._capture(mb, s256_lane, format='32')
        cap = self._split_s256_packet(raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF, geometry)
        np.testing.assert_array_equal(cap.frame_flags, 0)

        # Phase 2: every board injects its own distinctive channel mask.
        masks = {mbx.slot: 1 << (mbx.slot - 1) for mbx in self.array.ib}
        for mbx in self.array.ib:
            mask = masks[mbx.slot]
            for ch in range(16):
                buf = inject if (mask >> ch) & 1 else zeros
                mbx.set_funcgen(function='frame8', channels=[ch],
                                left_shift=0, right_shift=0, overflow_buffer=buf)
        await self._ca_sync()
        ts, raw, counts, geometry = await self._capture(mb, s256_lane, format='32')
        cap = self._split_s256_packet(raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF, geometry)

        expected_direct = [masks.get(w + 1, 0) for w in range(16)]
        expected_model = [int(v) for v in self._expected_frame_flags(mb, s256_lane, masks)]
        assert expected_model == expected_direct, \
            (f"flag-layout model disagrees with the direct expectation: "
             f"model={[hex(v) for v in expected_model]} "
             f"direct={[hex(v) for v in expected_direct]}")
        nonzero_df = int(np.count_nonzero(cap.data_flags))
        logger.info(f"[slot {s256_slot} lane {s256_lane}] frame_flags="
                    f"{[hex(int(v)) for v in cap.frame_flags[0, 0]]} "
                    f"nonzero_data_flags_words={nonzero_df} (not asserted, TUSER defect)")
        for packet in range(cap.frame_flags.shape[0]):
            for frame in range(cap.frame_flags.shape[1]):
                observed = [int(v) for v in cap.frame_flags[packet, frame]]
                assert observed == expected_direct, '\n'.join(
                    f"frame-flags word {w} (source slot {w + 1}): observed {obs:#010x}, "
                    f"expected {exp:#010x}"
                    for w, (obs, exp) in enumerate(zip(observed, expected_direct))
                    if obs != exp)

    @pytest.mark.ct_shuffle256
    async def test_gpu_link_feed_crate(self, s256_slot, crate_setup, xr):
        '''
        Verify the 10G GPU links of each board absorb the shuffle256 output without overflow.

        Same checks as the bench GPU-link tests: clean-start the TX FIFOs
        (retrying the core reset, which released mid-stream can wedge a
        lane - see ct_common.gpu_clean_start), then require an advancing
        FRAME_CTR and zero data/frame FIFO overflow on all 8 lanes, twice
        with a settling interval.

        Parameters:
            s256_slot (int): backplane slot of the board to check (1-16)
        '''
        logger = self.get_logger()
        mb = self._slot_board(s256_slot)
        if getattr(mb, 'GPU', None) is None:
            pytest.skip("This firmware implements no GPU data links")
        await self._require_dataflow(mb)
        gpu = mb.GPU
        await ct_common.gpu_clean_start(gpu, logger)
        a = int(gpu.FRAME_CTR)
        await asyncio.sleep(0.5)
        b = int(gpu.FRAME_CTR)
        assert a != b, (f"[slot {s256_slot}] GPU FRAME_CTR is static: the corner-turn "
                        f"output is not reaching the 10G links")
        for check in range(2):
            data_overflow = int(gpu.DATA_FIFO_OVERFLOW)
            frame_overflow = int(gpu.FRAME_FIFO_OVERFLOW)
            logger.debug(f"[slot {s256_slot}] gpu check={check} "
                         f"data_overflow={data_overflow:#04x} frame_overflow={frame_overflow:#04x}")
            assert data_overflow == 0, \
                (f"[slot {s256_slot}] 10G TX data FIFO overflow mask {data_overflow:#04x} "
                 f"(frame_overflow={frame_overflow:#04x}) - wedged-FIFO reset weakness or "
                 f"rate problem; see ct_common.gpu_clean_start")
            assert frame_overflow == 0, \
                f"[slot {s256_slot}] 10G TX frame FIFO overflow mask {frame_overflow:#04x}"
            if check == 0:
                await asyncio.sleep(0.5)
