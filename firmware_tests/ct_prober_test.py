import logging
import numpy as np
from pychfpga.fpga_array import FPGAArray
from .utils import plot
import pytest
import asyncio
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
import matplotlib.pyplot as plt
from .module_test import TestModule
from . import ct_common

'''
Corner-turn capture probe (ct_prober) tests
===========================================
These tests exercise the corner-turn output capture probe: an independent
capture engine that snapshots complete corner-turn packets from one selectable
output lane and streams them over the slow capture link with cookie 0xA2
(BSB port 6). They verify the register plane (parked power-up state, lane
selection, FIFO-ceiling enforcement) and the full capture path (packet
geometry, the CT header carried in the payload, burst reassembly, live lane
switching, and the (4+4)-bit complex decode).

The probe exists only in ICE corner-turn bitstreams: on firmware without it
(CRS/UCAP platforms, corr16 builds) the setup_ct_prober fixture skips every
test except test_ct_source_rejected_without_probe, which then verifies the
explicit error path instead.

The probe's capture FIFO holds 2048 words, so a whole burst must fit in it.
Every shuffle16 packet geometry fits (517/582 words per packet at one frame
per packet with flags off/on, 1029/1159 at two frames), but the algorithm
tests want minimal deterministic packets, so the setup_ct_prober fixture
configures the corner turn into the canonical probe operating point
(frames_per_packet=1, send_flags=False, default bin selection). The flags
tests use their own setup_ct_flags fixture to switch to the flags-on
geometries instead; the applied operating point is cached so the (slow)
reconfiguration only happens when a test needs a different one. None of this
affects the other firmware test suites, which capture upstream of the corner
turn.

The flags tests fabricate deterministic per-channel flags and verify the
packet's flags block bit-exactly. Note the as-built firmware limitation
documented at test_data_flags_as_built: the per-bin data-flags words do not
carry scaler overflow flags on current bitstreams; the per-frame frame-flags
word (ADC and FFT overflow per channel) is fully functional.
'''


class TestCTProber(TestModule):

    CAPTURE_PERIOD = 0.01  # seconds between capture bursts
    NCAP = 1               # bursts per capture in single-burst tests
    HEADER_NCAP = 2        # bursts needed for timestamp-spacing checks
    PATTERN_BYTE = 0x2F    # asymmetric nibbles: Re=2, Im=-1
    INNER_HEADER_COOKIE = 0xCF
    INNER_HEADER_WORDS = 4
    CT_NOISE_SEED = 0x5EED # seed for the deterministic noise waveform

    # Flags-on shuffle16 packet geometry (4-word header + fpp*512 data words
    # + fpp*(64 combined data-flags + 1 frame-flags) words + 1 trailer) and
    # the resulting 2048-word capture FIFO burst ceilings.
    FLAGS_WORDS_PER_PACKET = {1: 582, 2: 1159}
    FLAGS_MAX_BURST = {1: 3, 2: 1}
    ADC_FLAG_MASK = 0xA5C3 # per-channel ADC-overflow injection pattern
    FFT_FLAG_MASK = 0x3C69 # per-channel FFT OVERFLOW_FORCE pattern
    # As-built expectation for the only data-flags word that can be nonzero
    # (word 63 on lanes 6/7, see test_data_flags_as_built), pinned from
    # hardware (mb 509, aug11 bitstream) under maximal flag forcing:
    # lane 6 samples the gain-bank bit (0 for the default bank 0) and
    # lane 7 the FFT-overflow bit, asserted on the frame's TLAST word for
    # all 16 channels. Set to None to fall back to a stability-only check
    # that logs the observed value.
    DATA_FLAGS_W63 = {6: 0x0000, 7: 0xFFFF}

    def get_logger(self):
        return self.logger

    def get_widths(_, test_config):
        widths = NameSpace({})
        widths.PLATFORM = test_config.platform[:3] # Either ICE or CRS
        widths.MAX_LANE = 7 #corner-turn output lanes are numbered 0-7 (3-bit LANE_SEL)
        return widths

    @pytest.fixture(scope='function', autouse=True)
    async def setup(self, xr, request):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = await self.board_conn(xr, request)
        self.data.NUM_SAMPLES = self.board.ADC_SAMPLES_PER_FRAME
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.logleveltest)
        # Park all capture engines, then run the canonical reset sequence
        # (channelizers into reset, corner turn reset pulsed, corner turn
        # released BEFORE the channelizers): the chan-to-CT stream only starts
        # cleanly in that order, and a fresh connection leaves CORR_RESET
        # asserted, which silences the corner turn entirely.
        ct_common.stop_captures(self.board)
        self.board.reset()
        # check that the probe error flags don't go high either before or after test
        probe = getattr(self.board, 'CT_PROBER', None)
        if probe is not None:
            pre_flags = (int(probe.OVERRUN), int(probe._DATA_FIFO_OVERFLOW_STICKY))
            self.logger.debug(f"setup pre-check: overrun={pre_flags[0]} overflow_sticky={pre_flags[1]}")
            assert all(v == 0 for v in pre_flags)
        yield # test occurs here
        ct_common.stop_captures(self.board)
        if probe is not None:
            post_flags = (int(probe.OVERRUN), int(probe._DATA_FIFO_OVERFLOW_STICKY))
            self.logger.debug(f"setup post-check after cleanup: overrun={post_flags[0]} overflow_sticky={post_flags[1]}")
            assert all(v == 0 for v in post_flags)

    def _configure_ct(self, xr, key, frames_per_packet, send_flags):
        '''Put the corner turn into a known probe test operating point.

        The applied operating point is cached in the session data and only
        re-applied when a test needs a different one, since reconfiguration
        is slow and stops the corner-turn clock. init_crossbars wraps all
        bin-selector programming in the chan/corr reset sequence, which also
        satisfies the firmware requirement that SEND_FLAGS may only change
        across a module reset.
        '''
        if xr.data.get('ct_config') == key:
            return
        self.logger.info(
            f"Configuring the corner turn into the '{key}' probe test operating point "
            f"(frames_per_packet={frames_per_packet}, send_flags={send_flags}, "
            f"default bin selection)")
        self.board.CT.init_crossbars(
            mode=self.board.mode, frames_per_packet=frames_per_packet, send_flags=send_flags)
        # init_crossbars never writes USE_OFFSET_BINARY and its power-on
        # default is 1; clear it so the packet-header flag (which drives
        # the capture decoder's offset-binary correction) is truthful for
        # the two's-complement data these tests generate.
        for bin_sel in self.board.CT.CROSSBAR.BIN_SEL:
            bin_sel.USE_OFFSET_BINARY = 0
        self.board.sync()
        # Reset the GPU link MACs after reconfiguring the corner turn, as
        # the production init_corner_turn flow does: a corner-turn stream
        # interrupted mid-packet (clock stop during reconfiguration or
        # board bring-up) leaves orphaned words in a link's TX FIFO, which
        # then overflows on every subsequent packet. OVERFLOW_RESET clears
        # only the flag; this clears the FIFOs.
        if getattr(self.board, 'GPU', None) is not None:
            self.board.GPU.reset()
        xr.data.ct_config = key
        # Reconfiguration stops the corner-turn clock, so any cached dataflow
        # verdict is stale: make _require_ct_dataflow prove it again.
        xr.data.pop('ct_prober_dataflow', None)

    @pytest.fixture(scope='function')
    async def setup_ct_prober(self, setup, xr):
        self.logger.debug(f"Attempting to setup ct_prober on instance {self}")
        if getattr(self.board, 'CT_PROBER', None) is None:
            pytest.skip("Corner-turn capture probe is not implemented in this firmware "
                        "(CRS/UCAP platform or corr16 build)")
        # Canonical probe operating point: default bin selection, one frame
        # per packet, flags off. This guarantees a known minimal geometry for
        # the algorithm tests.
        self._configure_ct(xr, 'canonical', frames_per_packet=1, send_flags=False)
        self.logger.debug("Setup corner turn for testing ct_prober")

    @pytest.fixture(scope='function')
    async def setup_ct_flags(self, fpp, setup, xr):
        '''Configure the corner turn for the flags tests: send_flags=True at
        the parametrized frames-per-packet, default bin selection otherwise.
        '''
        self.logger.debug(f"Attempting to setup ct_prober flags-on (fpp={fpp}) on instance {self}")
        if getattr(self.board, 'CT_PROBER', None) is None:
            pytest.skip("Corner-turn capture probe is not implemented in this firmware "
                        "(CRS/UCAP platform or corr16 build)")
        if self.board.mode != 'shuffle16':
            pytest.skip("Corner-turn flags tests are currently defined only for shuffle16")
        self._configure_ct(xr, f'flags_fpp{fpp}', frames_per_packet=fpp, send_flags=True)

    def _decode_ct_byte(self, byte, use_offset_binary=False):
        '''Reference (4+4)-bit complex decode of one corner-turn payload byte.'''
        return ct_common.decode_ct_byte(byte, use_offset_binary)

    def _gen_ct_waveforms(self, waveform):
        '''Generate one deterministic frame-periodic funcgen pattern per channelizer.

        Returns a (16, NUM_SAMPLES) uint8 array with a distinct pattern per
        channel, so any channel-routing error in the corner turn shows up as
        a data mismatch. All frames repeat the same pattern, so the reference
        holds no matter which frame the probe happens to capture.
        '''
        num_samples = int(self.data.NUM_SAMPLES)
        channels = np.arange(16)[:, None]
        samples = np.arange(num_samples)[None, :]
        if waveform == 'ramp':
            data = (samples + 16 * channels) % 256 # per-channel phase-offset ramps
        elif waveform == 'channel_id':
            # Constant complex identity: channel ch reads (ch, 15-ch) in the
            # decoded (4+4)-bit nibbles everywhere, pinpointing routing errors.
            data = np.where(samples % 2 == 0, channels << 4, (15 - channels) << 4)
        elif waveform == 'noise':
            rng = np.random.default_rng(self.CT_NOISE_SEED)
            data = rng.integers(0, 256, size=(16, num_samples))
        else:
            raise ValueError(f"Unknown corner-turn test waveform '{waveform}'")
        return data.astype(np.uint8)

    def _corner_turn_reference(self, lane, chan_bytes):
        '''Compute the expected decoded capture for one corner-turn output lane.

        With the FFT and scaler bypassed, funcgen byte pair (2b, 2b+1) of each
        channel becomes frequency bin b, and the corner turn keeps the top
        nibble of each byte as the signed (4+4)-bit complex value. The
        bin/channel permutation itself is delegated to the driver's own
        ChanBinSel.map() model (which uses the bin selection programmed this
        session), so this test cross-validates that model against silicon.
        CROSSBAR2/CROSSBAR3 are exact pass-throughs in shuffle16 and need no
        modelling.
        '''
        re4 = (chan_bytes[:, 0::2].view(np.int8) >> 4).astype(np.float32)
        im4 = (chan_bytes[:, 1::2].view(np.int8) >> 4).astype(np.float32)
        cvals = re4 + 1j * im4 # (16, NUM_SAMPLES/2) complex, one value per bin
        input_data = {ch: cvals[ch] for ch in range(16)}
        ref_data = self.board.CT.CROSSBAR.BIN_SEL[lane].map(input_data)['data']
        return np.asarray(ref_data, dtype=np.complex64)

    async def _require_ct_dataflow(self):
        '''Skip capture tests when the corner turn emits no output packets.

        The probe can only capture what the corner turn produces, and
        read_raw_frames() has no timeout: without this check a silent
        corner-turn output (e.g. a board whose BCT bring-up is incomplete)
        would hang the capture tests forever. CROSSBAR3.OUTPUT_FRAME_CTR
        counts frames leaving the last corner-turn stage - the stream the
        probe taps - so a static counter proves the corner turn itself is
        silent, independently of the probe. (GPU.FRAME_CTR is unsuitable:
        that status register is never driven by the firmware.)
        '''
        await ct_common.require_ct_dataflow(
            self.board, self.data, self.get_logger(),
            sync=lambda: (self.board.set_trig(1), self.board.sync()))

    async def _set_capture_ct(self, ncap=1, lane=0, frames_per_burst=1, format=None, period=None,
                        split=None):
        '''Configures a corner-turn capture on the given lane and reads it back.

        Returns (ts, data, counts, geometry), where geometry is the packet
        geometry read back from the first-crossbar bin-selector registers.
        Raw captures (format='32') of multi-packet bursts are only split
        into per-packet rows when split=True (decoded complex captures are
        always split).
        '''
        return await ct_common.set_capture_ct(
            self.board, self.ca, ncap=ncap, lane=lane, frames_per_burst=frames_per_burst,
            format=format, period=self.CAPTURE_PERIOD if period is None else period,
            split=split, recv_verbose=self.test_config.loglevelrecv,
            logger=self.get_logger())

    async def _gpu_clean_start(self, gpu, retries=8):
        '''Reset the GPU link MACs until their TX FIFOs come up clean.

        See ct_common.gpu_clean_start for the reset-release wedge mechanism
        this works around.
        '''
        await ct_common.gpu_clean_start(gpu, self.get_logger(), retries=retries)

    async def _capture_flags_words(self, lane, fpp, frames_per_burst=1, ncap=1):
        '''Capture flags-on packets on `lane` and split each into its sections.

        Packet layout (shuffle16 with combined data flags): 4 header words,
        then ALL frames' data words, then ALL frames' data-flags words, then
        one frame-flags word per frame (frame 0 first), then the zero
        packet-flags trailer word.

        Returns a NameSpace with ts, counts, geometry and the packet sections
        as int64 word arrays: header (n, 4), data (n, fpp*512), data_flags
        (n, fpp, 64), frame_flags (n, fpp) and trailer (n,), where
        n = ncap * frames_per_burst captured packets.
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        ts, raw, counts, geometry = await self._set_capture_ct(
            ncap=ncap, lane=lane, frames_per_burst=frames_per_burst, format='32',
            split=True) # split multi-packet bursts into per-packet rows
        assert geometry['send_flags'], "flags tests need a flags-on corner-turn configuration"
        assert geometry['frames_per_packet'] == fpp
        assert raw.shape[2] == geometry['words_per_packet']
        words = (raw[:, 0, :].astype(np.int64) & 0xFFFFFFFF)
        data_flag_words = geometry['flags_words'] - fpp # minus the per-frame frame-flags words
        pos = self.INNER_HEADER_WORDS
        header = words[:, :pos]
        data = words[:, pos:pos + geometry['data_words']]
        pos += geometry['data_words']
        data_flags = words[:, pos:pos + data_flag_words].reshape(words.shape[0], fpp, -1)
        pos += data_flag_words
        frame_flags = words[:, pos:pos + fpp]
        pos += fpp
        trailer = words[:, pos]
        assert pos + 1 == geometry['words_per_packet']
        logger.debug(f"_capture_flags_words: lane={lane} fpp={fpp} packets={words.shape[0]} "
                     f"data_flags_shape={data_flags.shape} "
                     f"frame_flags={[hex(int(v)) for v in frame_flags.ravel()[:4]]} "
                     f"trailer={trailer.tolist()}")
        return NameSpace(dict(ts=ts, counts=counts, geometry=geometry, header=header,
                              data=data, data_flags=data_flags, frame_flags=frame_flags,
                              trailer=trailer))

    # plot_and_test is inherited from TestModule

    @pytest.mark.ct_prober
    async def test_present_and_parked(self, setup_ct_prober):
        '''
        Verify that the probe responds on its BSB port and powers up parked and clean.

        A stall or garbage here usually means the loaded bitstream has no
        ct_prober (wrong bitstream) even though the driver was instantiated.
        '''
        logger = self.get_logger()
        probe = self.board.CT_PROBER
        capture_en = int(probe.CAPTURE_EN)
        overrun = int(probe.OVERRUN)
        overflow_sticky = int(probe._DATA_FIFO_OVERFLOW_STICKY)
        logger.debug(f"test_present_and_parked: capture_en={capture_en} overrun={overrun} "
                     f"overflow_sticky={overflow_sticky}")
        assert capture_en == 0
        assert overrun == 0
        assert overflow_sticky == 0

    @pytest.mark.ct_prober
    async def test_ct_source_rejected_without_probe(self, setup):
        '''
        Verify that start_ct_capture fails with an explicit error on firmware without the probe.
        '''
        logger = self.get_logger()
        if getattr(self.board, 'CT_PROBER', None) is not None:
            pytest.skip("This firmware implements the corner-turn capture probe; "
                        "the rejection path is exercised on corr16/CRS runs")
        logger.debug("test_ct_source_rejected_without_probe: expecting RuntimeError from start_ct_capture")
        with pytest.raises(RuntimeError, match='corner-turn capture engine'):
            ct_common.start_ct_capture(self.board, period=self.CAPTURE_PERIOD)

    @pytest.mark.ct_prober
    async def test_lane_readback(self, lanes, setup_ct_prober):
        '''
        Verify that LANE_SEL is latched into the capture mux and read back in STATUS.

        Parameters:
            lanes (list of int): corner-turn output lanes to sweep
        '''
        logger = self.get_logger()
        probe = self.board.CT_PROBER
        probe.RESET = 1 # in reset, the capture mux follows LANE_SEL directly
        for lane in lanes:
            probe.LANE_SEL = lane
            current_lane = int(probe.CURRENT_LANE)
            logger.debug(f"test_lane_readback: lane_sel={lane} current_lane={current_lane}")
            assert current_lane == lane
        probe.LANE_SEL = 0
        probe.RESET = 0

    @pytest.mark.ct_prober
    async def test_single_burst(self, lane, setup_ct_prober, xr):
        '''
        Verify that one burst decodes to the advertised geometry with complete counts.

        Parameters:
            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        ts, data, counts, geometry = await self._set_capture_ct(ncap=self.NCAP, lane=lane)
        samples = (geometry['frames_per_packet'] * geometry['bins_per_frame']
                   * geometry['channels_per_bin'])
        logger.debug(f"test_single_burst: lane={lane} samples={samples} data_shape={data.shape} "
                     f"data_dtype={data.dtype} counts={counts.tolist()}")
        logger.debug(f"test_single_burst: received_first16={data[0, 0, :16]}")
        assert data.shape == (1, 1, samples)
        assert data.dtype == np.complex64
        np.testing.assert_array_equal(counts, [[1]])
        assert int(self.board.CT_PROBER.OVERRUN) == 0

    @pytest.mark.ct_prober
    async def test_inner_header(self, lane, setup_ct_prober, xr):
        '''
        Verify that the CT header carried in the payload matches the crossbar configuration.

        Parameters:
            lane (int): corner-turn output lane to capture

        Notes:
            Captures with format='32' so the raw packet words (including the
            4-word CT header and the packet-flags trailer) are visible.
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        ts, data, counts, geometry = await self._set_capture_ct(
            ncap=self.HEADER_NCAP, lane=lane, format='32')
        logger.debug(f"test_inner_header: lane={lane} words_per_packet={geometry['words_per_packet']} "
                     f"data_shape={data.shape}")
        assert data.shape[2] == geometry['words_per_packet']
        for row in range(self.HEADER_NCAP):
            words = data[row, 0].astype(np.int64) & 0xFFFFFFFF
            w0, w1 = int(words[0]), int(words[1])
            logger.debug(f"test_inner_header: row={row} w0={w0:#010x} w1={w1:#010x} trailer={int(words[-1])}")
            assert w0 & 0xFF == self.INNER_HEADER_COOKIE, "inner CT header cookie"
            assert (w0 >> 8) & 0xF == self.INNER_HEADER_WORDS, "header length"
            assert (w0 >> 12) & 0xF == 1, "protocol version"
            assert (w0 >> 16) & 0xF == lane, "source lane in inner stream id"
            assert (w1 >> 28) & 1 == int(geometry['four_bits'])
            assert (w1 >> 29) & 1 == int(geometry['use_offset_binary'])
            assert (w1 >> 30) & 1 == int(geometry['send_flags'])
            assert (w1 >> 24) & 0xF == geometry['frames_per_packet']
            assert (w1 >> 12) & 0xFFF == geometry['bins_per_frame']
            # Bitstreams built before the chan_bin_sel header fix under-report
            # NUMBER_OF_CHANNELS by 4 (the firmware computed
            # (last_fifo - first_fifo) * 4 without the inclusive +1). Accept
            # both encodings until all deployed ct bitstreams carry the fix.
            assert w1 & 0xFFF in (geometry['channels_per_bin'],
                                  geometry['channels_per_bin'] - 4)
            assert int(words[-1]) == 0, "packet-flags trailer word"
        # Wrapper timestamps count CT packets; bursts are spaced by the
        # programmed primary period. The host may occasionally miss a burst
        # on the best-effort UDP capture path, so accept any whole number of
        # periods between the two captured bursts.
        period = int(self.board.CT_PROBER.PERIOD0) + 1
        ts_diff = int(ts[1]) - int(ts[0])
        logger.debug(f"test_inner_header: wrapper_ts={ts.tolist()} expected_period={period} "
                     f"periods_elapsed={ts_diff / period}")
        assert ts_diff > 0 and ts_diff % period == 0
        # The inner frame timestamp advances by frames_per_packet per CT packet.
        w3_diff = (int(data[1, 0, 3]) - int(data[0, 0, 3])) & 0xFFFFFFFF
        assert w3_diff == ts_diff * geometry['frames_per_packet']

    @pytest.mark.ct_prober
    async def test_burst_indices_and_timestamps(self, lane, setup_ct_prober, xr):
        '''
        Verify that a multi-packet burst is reassembled from its burst-index nibbles.

        Parameters:
            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        probe = self.board.CT_PROBER
        geometry = probe.get_packet_geometry(lane)
        frames_per_burst = min(3, probe.DATA_FIFO_DEPTH // geometry['words_per_packet'])
        logger.debug(f"test_burst_indices_and_timestamps: lane={lane} "
                     f"words_per_packet={geometry['words_per_packet']} frames_per_burst={frames_per_burst}")
        if frames_per_burst < 2:
            pytest.skip("Current corner-turn packet size only allows single-packet bursts")
        ts, data, counts, geometry = await self._set_capture_ct(
            ncap=self.HEADER_NCAP, lane=lane, frames_per_burst=frames_per_burst)
        assert data.shape[0] == self.HEADER_NCAP * frames_per_burst
        np.testing.assert_array_equal(counts, np.ones_like(counts))
        ts = ts.astype(np.int64)
        for burst in range(self.HEADER_NCAP):
            rows = slice(burst * frames_per_burst, (burst + 1) * frames_per_burst)
            logger.debug(f"test_burst_indices_and_timestamps: burst={burst} ts={ts[rows].tolist()}")
            np.testing.assert_array_equal(np.diff(ts[rows]), 1)

    @pytest.mark.ct_prober
    async def test_lane_switch(self, target_lane, setup_ct_prober, xr):
        '''
        Verify that writing CT_PROBER.LANE_SEL retargets the next burst.

        Parameters:
            target_lane (int): lane to switch to on the fly (should differ from lane 0,
                where the capture starts, for the switch to be observable)
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        ts, data, counts, _ = await self._set_capture_ct(ncap=self.NCAP, lane=0, format='32')
        initial_lane = (int(data[0, 0, 0]) >> 16) & 0xF
        logger.debug(f"test_lane_switch: initial_lane={initial_lane} target_lane={target_lane}")
        assert initial_lane == 0
        self.board.CT_PROBER.LANE_SEL = target_lane
        receiver = await self.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, counts = receiver.read_raw_frames(ncap=self.NCAP, source=2, format='32',
                                                    chan_first=False)
        switched_lane = (int(data[0, 0, 0]) >> 16) & 0xF
        logger.debug(f"test_lane_switch: switched_lane={switched_lane}")
        assert switched_lane == target_lane, "inner header lane number"

    @pytest.mark.ct_prober
    async def test_burst_ceiling_rejected(self, setup_ct_prober):
        '''
        Verify that bursts that cannot fit the 2048-word capture FIFO are refused.
        '''
        logger = self.get_logger()
        probe = self.board.CT_PROBER
        geometry = probe.get_packet_geometry(0)
        max_frames = probe.DATA_FIFO_DEPTH // geometry['words_per_packet']
        logger.debug(f"test_burst_ceiling_rejected: words_per_packet={geometry['words_per_packet']} "
                     f"max_frames={max_frames}")
        if max_frames >= 4:
            pytest.skip("Current corner-turn packet size fits 4 packets; "
                        "the 2-bit BURST_LENGTH range check triggers first")
        with pytest.raises(ValueError, match='does not fit'):
            ct_common.start_ct_capture(self.board, period=self.CAPTURE_PERIOD,
                                       frames_per_burst=max_frames + 1)

    @pytest.mark.ct_prober
    async def test_constant_pattern_nibble_decode(self, lane, setup_ct_prober, xr):
        '''
        Verify that decoded samples match the reference nibble decode of the raw bytes.

        A constant funcgen pattern makes every payload byte identical, so the
        decoded output must be that byte's (4+4)-bit complex value everywhere.
        This validates the Re/Im nibble order and offset-binary handling on
        real hardware (byte-to-channel ordering needs a structured pattern and
        is not covered here).

        Parameters:
            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        logger.debug(f"test_constant_pattern_nibble_decode: pattern_byte={self.PATTERN_BYTE:#04x}")
        self.board.set_channelizer(
            data_source='arb', fft_bypass=1, scaler_bypass=1,
            offset_binary_encoding=False,
            data=np.full(self.data.NUM_SAMPLES, self.PATTERN_BYTE, dtype=np.uint8))
        self.board.sync()
        ts, raw, counts, geometry = await self._set_capture_ct(ncap=self.NCAP, lane=lane, format='32')
        payload_words = raw[0, 0, self.INNER_HEADER_WORDS:self.INNER_HEADER_WORDS + geometry['data_words']]
        payload_bytes = (payload_words.astype(np.int64) & 0xFFFFFFFF).astype('>u4').view(np.uint8)
        logger.debug(f"test_constant_pattern_nibble_decode: payload_first16={payload_bytes[:16]}")
        assert np.unique(payload_bytes).size == 1, "constant input must give a constant payload"
        expected = self._decode_ct_byte(int(payload_bytes[0]), geometry['use_offset_binary'])

        receiver = await self.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, counts = receiver.read_raw_frames(ncap=self.NCAP, source=2, chan_first=False)
        ref_data = np.full(data.shape[2], expected, dtype=np.complex64)
        logger.debug(f"test_constant_pattern_nibble_decode: received_first16={data[0, 0, :16]}")
        logger.debug(f"test_constant_pattern_nibble_decode: reference_first16={ref_data[:16]}")

        self.plot_and_test(xr, data[0, 0], ref_data,
                           f'Constant pattern nibble decode (lane={lane}, byte={self.PATTERN_BYTE:#04x})')

    @pytest.mark.ct_prober
    async def test_chan_source_roundtrip(self, setup_ct_prober, xr):
        '''
        Verify that a channelizer capture still decodes after the corner-turn probe is parked.
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        await self._set_capture_ct(ncap=self.NCAP, lane=0)
        logger.debug("test_chan_source_roundtrip: parking the corner-turn probe, then capturing FUNCGEN")
        ct_common.start_data_capture(self.board, period=self.CAPTURE_PERIOD, source='funcgen')
        capture_en = int(self.board.CT_PROBER.CAPTURE_EN)
        logger.debug(f"test_chan_source_roundtrip: capture_en={capture_en}")
        assert capture_en == 0, "ct probe must stay parked"
        self.board.set_channelizer(data_source='ramp', fft_bypass=1, scaler_bypass=1)
        receiver = await self.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        self.board.sync()
        ts, data, counts = receiver.read_raw_frames(ncap=self.NCAP, source=0)
        logger.debug(f"test_chan_source_roundtrip: counts={counts.tolist()}")
        assert np.all(counts > 0)

    @pytest.mark.ct_prober
    async def test_corner_turn_shuffle16(self, lane, waveform, setup_ct_prober, xr):
        '''
        Verify the corner-turn shuffling algorithm end to end against a host reference.

        Each channelizer is fed a distinct deterministic waveform through the
        bypassed FFT and scaler, so the corner turn sees known (4+4)-bit
        complex values in every frequency bin of every channel. The captured
        output of the selected lane is then compared sample for sample with a
        host-computed reference built from the same waveforms and the bin
        selection programmed into the crossbar.

        Parameters:
            lane (int): corner-turn output lane to capture and verify

            waveform (str): per-channel funcgen pattern ('ramp', 'channel_id'
                or 'noise'); all are deterministic and frame-periodic
        '''
        logger = self.get_logger()
        if self.board.mode != 'shuffle16':
            pytest.skip("Corner-turn algorithm test is currently defined only for shuffle16")
        await self._require_ct_dataflow()
        chan_bytes = self._gen_ct_waveforms(waveform)
        logger.debug(f"test_corner_turn_shuffle16: lane={lane} waveform={waveform} "
                     f"chan_bytes_shape={chan_bytes.shape}")
        self.board.set_channelizer(
            data_source=None, fft_bypass=1, scaler_bypass=1,
            scaler_bypass_data_type=0, offset_binary_encoding=False)
        for ch in range(16):
            self.board.set_funcgen(function='arb', channels=[ch], left_shift=0, right_shift=0,
                                   data=chan_bytes[ch])
        self.board.sync()
        ts, data, counts, geometry = await self._set_capture_ct(ncap=self.NCAP, lane=lane)
        np.testing.assert_array_equal(counts, [[1]])
        assert geometry['bins_per_frame'] == 128
        assert geometry['channels_per_bin'] == 16
        assert geometry['four_bits'] and not geometry['use_offset_binary']

        ref_data = self._corner_turn_reference(lane, chan_bytes)
        received = data[0, 0]
        mismatches = int(np.count_nonzero(received != ref_data))
        logger.debug(f"test_corner_turn_shuffle16: samples={received.size} mismatches={mismatches}")
        logger.debug(f"test_corner_turn_shuffle16: received_first16={received[:16]}")
        logger.debug(f"test_corner_turn_shuffle16: reference_first16={ref_data[:16]}")

        self.plot_and_test(xr, received, ref_data,
                           f'Corner turn shuffle16 (lane={lane}, waveform={waveform})')

    @pytest.mark.ct_prober
    async def test_gpu_link_feed(self, setup_ct_prober, xr):
        '''
        Verify that the corner-turn output stream reaches the 10G GPU link inputs.

        The GPU link block counts incoming frames on lane 0 (FRAME_CTR, an
        8-bit counter incrementing once per CT packet, so it rolls over
        continuously while data flows) and latches sticky per-lane FIFO
        overflow flags. A static FRAME_CTR means the corner-turn output is
        not wired to the links (firmware built without the reconnection);
        overflow flags indicate the links cannot absorb the data rate.
        '''
        logger = self.get_logger()
        if getattr(self.board, 'GPU', None) is None:
            pytest.skip("This firmware implements no GPU data links")
        await self._require_ct_dataflow()
        gpu = self.board.GPU
        # Clean-start the TX FIFOs after the stream interruptions of this
        # test's own setup (see _gpu_clean_start for why one reset is not
        # always enough).
        await self._gpu_clean_start(gpu)
        a = int(gpu.FRAME_CTR)
        await asyncio.sleep(0.2)
        b = int(gpu.FRAME_CTR)
        data_overflow = int(gpu.DATA_FIFO_OVERFLOW)
        frame_overflow = int(gpu.FRAME_FIFO_OVERFLOW)
        logger.debug(f"test_gpu_link_feed: frame_ctr {a}->{b} advancing={a != b} "
                     f"data_overflow={data_overflow:#04x} frame_overflow={frame_overflow:#04x}")
        assert a != b, "GPU FRAME_CTR is static: corner-turn output is not reaching the 10G links"
        assert data_overflow == 0
        assert frame_overflow == 0

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_flags_packet_size(self, fpp, lane, setup_ct_flags, xr):
        '''
        Verify that the larger flags-on packets are advertised, transported and captured exactly.

        With send_flags on, every shuffle16 packet grows to 582 words (one
        frame per packet) or 1159 words (two frames). This test proves the
        probe capture path handles the larger packets safely: the geometry
        readback, the captured packet length, the CT header fields, the zero
        trailer and every overflow/overrun indicator along the path,
        including the per-bin-selector flags FIFOs that are only exercised
        when flags are enabled.

        Parameters:
            fpp (int): frames per packet programmed into the corner turn

            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        probe = self.board.CT_PROBER
        geometry = probe.get_packet_geometry(lane)
        logger.debug(f"test_flags_packet_size: lane={lane} fpp={fpp} geometry={geometry}")
        assert geometry['send_flags']
        assert geometry['words_per_packet'] == self.FLAGS_WORDS_PER_PACKET[fpp]
        assert geometry['data_words'] == fpp * 512
        assert geometry['flags_words'] == fpp * 65
        cap = await self._capture_flags_words(lane, fpp)
        np.testing.assert_array_equal(cap.counts, [[1]])
        w0, w1 = int(cap.header[0, 0]), int(cap.header[0, 1])
        logger.debug(f"test_flags_packet_size: w0={w0:#010x} w1={w1:#010x}")
        assert w0 & 0xFF == self.INNER_HEADER_COOKIE, "inner CT header cookie"
        assert (w0 >> 16) & 0xF == lane, "source lane in inner stream id"
        assert (w1 >> 31) & 1 == 0, "bypass header bit"
        assert (w1 >> 30) & 1 == 1, "send_flags header bit"
        assert (w1 >> 24) & 0xF == fpp
        assert (w1 >> 12) & 0xFFF == geometry['bins_per_frame']
        assert int(cap.trailer[0]) == 0, "packet-flags trailer word"
        assert int(probe.OVERRUN) == 0
        assert int(probe._DATA_FIFO_OVERFLOW_STICKY) == 0
        # The bin selectors' data/frame flags FIFOs only fill with
        # send_flags on; none of them may overflow at the nominal rate.
        for bin_sel in self.board.CT.CROSSBAR.BIN_SEL:
            assert int(bin_sel.DATA_FLAGS_OVERFLOW) == 0
            assert int(bin_sel.FRAME_FIFO_OVERFLOW) == 0

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_flags_burst_ceiling(self, fpp, setup_ct_flags, xr):
        '''
        Verify the capture FIFO burst ceilings of the flags-on packet sizes.

        582-word packets must allow 3-packet bursts and 1159-word packets
        single-packet bursts in the 2048-word capture FIFO; one word more
        must be refused by the driver. A maximal burst is then captured
        intact to show the ceiling is actually safe, not just enforced.

        Parameters:
            fpp (int): frames per packet programmed into the corner turn
        '''
        logger = self.get_logger()
        probe = self.board.CT_PROBER
        geometry = probe.get_packet_geometry(0)
        max_frames = probe.DATA_FIFO_DEPTH // geometry['words_per_packet']
        logger.debug(f"test_flags_burst_ceiling: fpp={fpp} words_per_packet={geometry['words_per_packet']} "
                     f"max_frames={max_frames}")
        assert max_frames == self.FLAGS_MAX_BURST[fpp]
        cap = await self._capture_flags_words(0, fpp, frames_per_burst=max_frames)
        assert cap.data.shape[0] == max_frames
        np.testing.assert_array_equal(cap.counts, np.ones_like(cap.counts))
        if max_frames > 1:
            # Packets within a burst are consecutive CT packets: the inner
            # header timestamp (word 3) must advance by exactly fpp between
            # them, proving real burst reassembly (the wrapper timestamps of
            # split raw captures are synthesized and prove nothing).
            w3 = cap.header[:, 3]
            np.testing.assert_array_equal(np.diff(w3) & 0xFFFFFFFF, fpp)
        assert int(probe.OVERRUN) == 0
        assert int(probe._DATA_FIFO_OVERFLOW_STICKY) == 0
        with pytest.raises(ValueError, match='does not fit'):
            ct_common.start_ct_capture(self.board, period=self.CAPTURE_PERIOD,
                                       frames_per_burst=max_frames + 1)

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_flags_gpu_link_feed(self, fpp, setup_ct_flags, xr):
        '''
        Verify that the 10G GPU links absorb the larger flags-on packets without overflow.

        Same checks as test_gpu_link_feed, but under the flags-on corner-turn
        configuration (fpp=2 with flags is the production-like operating
        point and the largest sustained packet rate). The overflow flags are
        read twice with a settling interval to catch slow-onset overflow of
        a nearly-saturated link.

        Parameters:
            fpp (int): frames per packet programmed into the corner turn
        '''
        logger = self.get_logger()
        if getattr(self.board, 'GPU', None) is None:
            pytest.skip("This firmware implements no GPU data links")
        await self._require_ct_dataflow()
        gpu = self.board.GPU
        # This test measures sustained-rate absorption, so it needs a clean
        # start after the stream interruptions of its own setup (the
        # per-test board.reset(), the sync() in the dataflow gate above).
        # Production's init_corner_turn likewise pulses the GPU core reset
        # as its last step; _gpu_clean_start additionally retries the reset,
        # which released mid-stream can itself wedge a lane.
        await self._gpu_clean_start(gpu)
        a = int(gpu.FRAME_CTR)
        await asyncio.sleep(0.5)
        b = int(gpu.FRAME_CTR)
        assert a != b, "GPU FRAME_CTR is static: corner-turn output is not reaching the 10G links"
        for check in range(2):
            data_overflow = int(gpu.DATA_FIFO_OVERFLOW)
            frame_overflow = int(gpu.FRAME_FIFO_OVERFLOW)
            logger.debug(f"test_flags_gpu_link_feed: fpp={fpp} check={check} "
                         f"data_overflow={data_overflow:#04x} frame_overflow={frame_overflow:#04x}")
            assert data_overflow == 0, (
                f"10G TX data FIFO overflowed under flags-on packets "
                f"(fpp={fpp} check={check} data_overflow={data_overflow:#04x} "
                f"frame_overflow={frame_overflow:#04x})")
            assert frame_overflow == 0, (
                f"10G TX frame FIFO overflowed under flags-on packets "
                f"(fpp={fpp} check={check} frame_overflow={frame_overflow:#04x})")
            if check == 0:
                await asyncio.sleep(0.5)

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_flags_data_decode(self, fpp, lane, waveform, setup_ct_flags, xr):
        '''
        Verify that the decoded capture path is byte-exact with flags appended to the packets.

        Runs one corner-turn algorithm instance under the flags-on
        configuration through the normal decoded capture path (no raw
        format), proving that the receiver's data slice is unaffected by the
        flags block that now follows it in every packet.

        Parameters:
            fpp (int): frames per packet programmed into the corner turn

            lane (int): corner-turn output lane to capture and verify

            waveform (str): per-channel funcgen pattern (see _gen_ct_waveforms)
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        chan_bytes = self._gen_ct_waveforms(waveform)
        self.board.set_channelizer(
            data_source=None, fft_bypass=1, scaler_bypass=1,
            scaler_bypass_data_type=0, offset_binary_encoding=False)
        for ch in range(16):
            self.board.set_funcgen(function='arb', channels=[ch], left_shift=0, right_shift=0,
                                   data=chan_bytes[ch])
        self.board.sync()
        ts, data, counts, geometry = await self._set_capture_ct(ncap=self.NCAP, lane=lane)
        np.testing.assert_array_equal(counts, [[1]])
        samples = (geometry['frames_per_packet'] * geometry['bins_per_frame']
                   * geometry['channels_per_bin'])
        logger.debug(f"test_flags_data_decode: lane={lane} fpp={fpp} samples={samples} "
                     f"data_shape={data.shape}")
        assert data.shape == (1, 1, samples)
        assert samples == fpp * 2048
        # every frame of the packet carries the same frame-periodic waveforms
        ref_data = np.tile(self._corner_turn_reference(lane, chan_bytes), fpp)
        self.plot_and_test(xr, data[0, 0], ref_data,
                           f'Flags-on data decode (lane={lane}, fpp={fpp}, waveform={waveform})')

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_frame_flags_adc_mask(self, fpp, lane, setup_ct_flags, xr):
        '''
        Verify the per-channel ADC-overflow bits of the frame-flags word bit-exactly.

        The funcgen 'frame8' source takes an overflow buffer whose sample
        bit 0 injects an ADC-overflow event, which the channelizer
        accumulates into a per-frame sticky TUSER bit; the corner turn
        latches those bits from all 16 channels into the low half of each
        packet's frame-flags word. Injecting one overflow per frame on a
        known subset of channels must therefore reproduce exactly that
        channel mask in every captured frame-flags word, after a negative
        phase proves the word is all-zero without injection.

        Parameters:
            fpp (int): frames per packet programmed into the corner turn

            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        zeros = np.zeros(int(self.data.NUM_SAMPLES), np.uint8)
        inject = zeros.copy()
        inject[0] = 1 # one injected ADC-overflow event per frame
        # Phase 1 (negative): no injected overflows -> all-zero frame flags.
        self.board.set_channelizer(
            data_source='frame8', fft_bypass=1, scaler_bypass=1,
            scaler_bypass_data_type=0, offset_binary_encoding=False,
            funcgen_left_shift=0, funcgen_right_shift=0, overflow_buffer=zeros)
        self.board.sync()
        cap = await self._capture_flags_words(lane, fpp)
        np.testing.assert_array_equal(cap.frame_flags, 0)
        # Phase 2: inject on the channels selected by ADC_FLAG_MASK.
        for ch in range(16):
            buf = inject if (self.ADC_FLAG_MASK >> ch) & 1 else zeros
            self.board.set_funcgen(function='frame8', channels=[ch],
                                   left_shift=0, right_shift=0, overflow_buffer=buf)
        self.board.sync()
        cap = await self._capture_flags_words(lane, fpp)
        expected = self.board.CT.CROSSBAR.BIN_SEL[lane].map_flags(
            frame_flags={ch: ((self.ADC_FLAG_MASK >> ch) & 1, 0)
                         for ch in range(16)})['frame_flags_word']
        logger.debug(f"test_frame_flags_adc_mask: lane={lane} fpp={fpp} expected={expected:#010x} "
                     f"observed={[hex(int(v)) for v in cap.frame_flags.ravel()]}")
        assert expected == self.ADC_FLAG_MASK # layout model: channel c lands on bit c
        np.testing.assert_array_equal(cap.frame_flags, expected)

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_frame_flags_fft_mask(self, fpp, lane, setup_ct_flags, xr):
        '''
        Verify the per-channel FFT-overflow bits of the frame-flags word bit-exactly.

        With the FFT in the datapath, each channel's OVERFLOW_FORCE debug bit
        deterministically asserts the per-frame FFT-overflow TUSER bit, which
        the corner turn latches into the high half of the frame-flags word.
        The FFT shift schedule is left at its shift-every-stage default so
        genuine data-driven overflow cannot occur: only the forced channels
        may flag. Phases: all-zero negative check, FFT mask alone, then FFT
        and ADC masks combined.

        Parameters:
            fpp (int): frames per packet programmed into the corner turn

            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        zeros = np.zeros(int(self.data.NUM_SAMPLES), np.uint8)
        inject = zeros.copy()
        inject[0] = 1
        self.board.set_channelizer(
            data_source='frame8', fft_bypass=0, scaler_bypass=1,
            scaler_bypass_data_type=0, offset_binary_encoding=False,
            funcgen_left_shift=0, funcgen_right_shift=0, overflow_buffer=zeros)
        self.board.set_fft_shift() # shift every stage: no genuine overflow possible
        self.board.sync()
        bin_sel = self.board.CT.CROSSBAR.BIN_SEL[lane]
        try:
            # Phase 1 (negative): nothing forced -> all-zero frame flags.
            cap = await self._capture_flags_words(lane, fpp)
            np.testing.assert_array_equal(cap.frame_flags, 0)
            # Phase 2: force the FFT-overflow flag on the FFT_FLAG_MASK channels.
            for ch in range(16):
                self.board.chan[ch].FFT.OVERFLOW_FORCE = (self.FFT_FLAG_MASK >> ch) & 1
            cap = await self._capture_flags_words(lane, fpp)
            expected = bin_sel.map_flags(
                frame_flags={ch: (0, (self.FFT_FLAG_MASK >> ch) & 1)
                             for ch in range(16)})['frame_flags_word']
            logger.debug(f"test_frame_flags_fft_mask: lane={lane} fpp={fpp} expected={expected:#010x} "
                         f"observed={[hex(int(v)) for v in cap.frame_flags.ravel()]}")
            assert expected == self.FFT_FLAG_MASK << 16
            np.testing.assert_array_equal(cap.frame_flags, expected)
            # Phase 3: combine with the ADC injection mask.
            for ch in range(16):
                buf = inject if (self.ADC_FLAG_MASK >> ch) & 1 else zeros
                self.board.set_funcgen(function='frame8', channels=[ch],
                                       left_shift=0, right_shift=0, overflow_buffer=buf)
            self.board.sync()
            cap = await self._capture_flags_words(lane, fpp)
            expected = bin_sel.map_flags(
                frame_flags={ch: ((self.ADC_FLAG_MASK >> ch) & 1,
                                  (self.FFT_FLAG_MASK >> ch) & 1)
                             for ch in range(16)})['frame_flags_word']
            np.testing.assert_array_equal(cap.frame_flags, expected)
        finally:
            # OVERFLOW_FORCE is a live control bit that survives board.reset();
            # never leak it into later tests.
            for ch in range(16):
                self.board.chan[ch].FFT.OVERFLOW_FORCE = 0

    @pytest.mark.ct_prober
    @pytest.mark.ct_flags
    async def test_data_flags_as_built(self, fpp, lane, setup_ct_flags, xr):
        '''
        Characterize the as-built (defective) content of the per-bin data-flags words.

        As designed, each data-flags word should carry the per-bin scaler
        overflow flags of all 16 channels for two selected bins. As built,
        it cannot: the channelizer truncates the scaler's 5-bit TUSER to
        bits 3:0 (chan.vhd:695), dropping the high-bin scaler overflow, and
        chan_bin_sel samples stale constants (chfpga_package.vhd:534-540,
        where SCALER_LOW collides with FFT_OVERFLOW=1 and SCALER_HIGH with
        GAINBANK=2), so it reads the gain-bank bit as the "even scaler flag"
        and the FFT-overflow bit as the "odd scaler flag". Both of those are
        asserted only on the last word(s) of a frame, and the surviving true
        scaler flag (TUSER bit 3) is never read.

        Consequence, verified here: the data-flags block is all-zero
        everywhere except possibly the final flags word (word 63) on the
        lanes whose default bin selection includes the frame's last data
        word - lane 6 (bin 1022, samples the gain-bank bit) and lane 7
        (bin 1023, samples the FFT-overflow bit), both in the high half of
        word 63. This defect is characterized deliberately and NOT fixed:
        a fix needs a TUSER repack in the firmware and a recompile.

        Hardware-pinned expectation (DATA_FLAGS_W63): lane 6 word 63 reads
        0x0000 (the gain-bank bit is 0 for the default bank 0) and lane 7
        reads 0xFFFF (the FFT-overflow flag is asserted on the frame's
        TLAST data word, which only lane 7's final selected bin coincides
        with, for all 16 channels when forced).

        Parameters:
            fpp (int): frames per packet programmed into the corner turn

            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        await self._require_ct_dataflow()
        zeros = np.zeros(int(self.data.NUM_SAMPLES), np.uint8)
        inject = zeros.copy()
        inject[0] = 1
        # Phase 1 (quiescent): FFT bypassed, nothing injected -> the whole
        # data-flags block must be zero on every lane.
        self.board.set_channelizer(
            data_source='frame8', fft_bypass=1, scaler_bypass=1,
            scaler_bypass_data_type=0, offset_binary_encoding=False,
            funcgen_left_shift=0, funcgen_right_shift=0, overflow_buffer=zeros)
        self.board.sync()
        cap = await self._capture_flags_words(lane, fpp)
        np.testing.assert_array_equal(cap.data_flags, 0)
        # Phase 2 (maximal forcing): every flag source that CAN reach the
        # corner turn is asserted on all 16 channels - FFT overflow forced,
        # one ADC overflow injected per frame. If scaler flags reached the
        # data-flags words as designed, or the sampled bits were asserted
        # frame-wide, this phase would light them up.
        self.board.set_channelizer(
            data_source='frame8', fft_bypass=0, scaler_bypass=1,
            scaler_bypass_data_type=0, offset_binary_encoding=False,
            funcgen_left_shift=0, funcgen_right_shift=0, overflow_buffer=inject)
        self.board.set_fft_shift() # no genuine data-driven FFT overflow on top
        self.board.sync()
        try:
            for ch in range(16):
                self.board.chan[ch].FFT.OVERFLOW_FORCE = 1
            cap = await self._capture_flags_words(lane, fpp, ncap=3)
            # stimulus sanity: with everything forced, the (functional)
            # frame-flags word must be all-ones in every frame
            np.testing.assert_array_equal(cap.frame_flags, 0xFFFFFFFF)
            flags = cap.data_flags # (3, fpp, 64)
            if lane <= 5:
                np.testing.assert_array_equal(flags, 0)
            else:
                np.testing.assert_array_equal(flags[:, :, :63], 0)
                w63 = flags[:, :, 63]
                assert np.all(w63 & 0xFFFF == 0), "low half of data-flags word 63 must stay zero"
                observed = np.unique(w63 >> 16)
                logger.info(f"test_data_flags_as_built: lane={lane} fpp={fpp} "
                            f"word63_high={[hex(int(v)) for v in observed]}")
                assert observed.size == 1, \
                    "data-flags word 63 must be stable across frames and captures"
                if self.DATA_FLAGS_W63 is not None:
                    assert int(observed[0]) == self.DATA_FLAGS_W63[lane]
        finally:
            for ch in range(16):
                self.board.chan[ch].FFT.OVERFLOW_FORCE = 0
