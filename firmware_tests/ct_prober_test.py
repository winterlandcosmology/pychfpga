import logging
import numpy as np
from pychfpga.fpga_array import FPGAArray
from .utils import plot
import pytest
from time import sleep
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
import matplotlib.pyplot as plt
from .module_test import TestModule

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
If the operational corner-turn configuration produces packets that cannot fit
(flags enabled with 2 frames per packet), the setup_ct_prober fixture
reconfigures the corner turn once per session into the probe's design
operating point (frames_per_packet=1, send_flags=False, default bin
selection) and leaves it there. This does not affect the other firmware test
suites, which capture upstream of the corner turn.
'''


class TestCTProber(TestModule):

    CAPTURE_PERIOD = 0.01  # seconds between capture bursts
    NCAP = 1               # bursts per capture in single-burst tests
    HEADER_NCAP = 2        # bursts needed for timestamp-spacing checks
    PATTERN_BYTE = 0x2F    # asymmetric nibbles: Re=2, Im=-1
    INNER_HEADER_COOKIE = 0xCF
    INNER_HEADER_WORDS = 4
    CT_NOISE_SEED = 0x5EED # seed for the deterministic noise waveform

    def get_logger(self):
        return self.logger

    def get_widths(_, test_config):
        widths = NameSpace({})
        widths.PLATFORM = test_config.platform[:3] # Either ICE or CRS
        widths.MAX_LANE = 7 #corner-turn output lanes are numbered 0-7 (3-bit LANE_SEL)
        return widths

    @pytest.fixture(scope='function', autouse=True)
    def setup(self, xr):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = self.board_conn(xr)
        self.data.NUM_SAMPLES = self.board.ADC_SAMPLES_PER_FRAME
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.logleveltest)
        # Park all capture engines, then run the canonical reset sequence
        # (channelizers into reset, corner turn reset pulsed, corner turn
        # released BEFORE the channelizers): the chan-to-CT stream only starts
        # cleanly in that order, and a fresh connection leaves CORR_RESET
        # asserted, which silences the corner turn entirely.
        self.board.stop_data_capture()
        self.board.reset()
        # check that the probe error flags don't go high either before or after test
        probe = getattr(self.board, 'CT_PROBER', None)
        if probe is not None:
            pre_flags = (int(probe.OVERRUN), int(probe._DATA_FIFO_OVERFLOW_STICKY))
            self.logger.debug(f"setup pre-check: overrun={pre_flags[0]} overflow_sticky={pre_flags[1]}")
            assert all(v == 0 for v in pre_flags)
        yield # test occurs here
        self.board.stop_data_capture()
        if probe is not None:
            post_flags = (int(probe.OVERRUN), int(probe._DATA_FIFO_OVERFLOW_STICKY))
            self.logger.debug(f"setup post-check after cleanup: overrun={post_flags[0]} overflow_sticky={post_flags[1]}")
            assert all(v == 0 for v in post_flags)

    @pytest.fixture(scope='function')
    def setup_ct_prober(self, setup, xr):
        self.logger.debug(f"Attempting to setup ct_prober on instance {self}")
        if getattr(self.board, 'CT_PROBER', None) is None:
            pytest.skip("Corner-turn capture probe is not implemented in this firmware "
                        "(CRS/UCAP platform or corr16 build)")
        # Once per session: put the corner turn into the canonical probe test
        # operating point (default bin selection, one frame per packet, flags
        # off). This guarantees a known geometry for the algorithm tests and
        # keeps every burst within the probe's 2048-word capture FIFO.
        if 'ct_canonical_configured' not in xr.data:
            self.logger.info(
                "Configuring the corner turn into the probe test operating point "
                "(frames_per_packet=1, send_flags=False, default bin selection)")
            self.board.init_crossbars(
                mode=self.board.mode, frames_per_packet=1, send_flags=False)
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
            xr.data.ct_canonical_configured = True
        self.logger.debug("Setup corner turn for testing ct_prober")

    def _decode_ct_byte(self, byte, use_offset_binary=False):
        '''Reference (4+4)-bit complex decode of one corner-turn payload byte.'''
        byte = int(byte) & 0xFF
        if use_offset_binary:
            byte ^= 0x88
        re = ((byte >> 4) + 8) % 16 - 8 # sign-extend the 4-bit nibbles
        im = ((byte & 0xF) + 8) % 16 - 8
        return complex(re, im)

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

    def _require_ct_dataflow(self):
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
        logger = self.get_logger()
        if 'ct_prober_dataflow' not in self.data:
            cb3 = getattr(self.board.CT, 'CROSSBAR3', None)
            if cb3 is None:
                self.data.ct_prober_dataflow = True # no independent monitor; try anyway
            else:
                self.board.set_trig(1)
                self.board.sync()
                a = int(cb3.OUTPUT_FRAME_CTR)
                sleep(0.3)
                b = int(cb3.OUTPUT_FRAME_CTR)
                logger.debug(f"_require_ct_dataflow: output_frame_ctr {a}->{b} advancing={a != b}")
                self.data.ct_prober_dataflow = a != b
        if not self.data.ct_prober_dataflow:
            pytest.skip("The corner-turn output is not streaming on this board "
                        "(CROSSBAR3 OUTPUT_FRAME_CTR is static): complete the "
                        "corner-turn dataflow bring-up before running capture tests")

    def _set_capture_ct(self, ncap=1, lane=0, frames_per_burst=1, format=None, period=None):
        '''Configures a corner-turn capture on the given lane and reads it back.

        Returns (ts, data, counts, geometry), where geometry is the packet
        geometry read back from the first-crossbar bin-selector registers.
        '''
        logger = self.get_logger()
        if period is None:
            period = self.CAPTURE_PERIOD
        logger.debug(f"_set_capture_ct: lane={lane} ncap={ncap} frames_per_burst={frames_per_burst} "
                     f"period={period} format={format}")
        self.board.start_data_capture(
            period=period, source='ct', lane=lane, frames_per_burst=frames_per_burst)
        geometry = self.board.CT_PROBER.get_packet_geometry(lane)
        logger.debug(f"_set_capture_ct: geometry={geometry}")
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, counts = receiver.read_raw_frames(
            ncap=ncap, source=2, format=format, chan_first=False)
        logger.debug(f"_set_capture_ct: ts={ts.tolist()} data_shape={data.shape} counts={counts.tolist()}")
        return ts, data, counts, geometry

    def plot_and_test(self, xr, data, ref_data, title, **kwargs):
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
        test_name = title[:title.find('(')] #up to position of first bracket
        parameters  = title[title.find('('):]
        xr.header(test_name)
        print(f'Parameters: {parameters}')
        print(data.shape, data.dtype)
        residuals = data - ref_data
        print("Creating plots")
        plot((data, ref_data), title=test_name, **kwargs)
        xr.insert_plot()
        plt.close()
        plot((residuals,), title = f"Residuals of {test_name.lower()}", **kwargs)
        xr.insert_plot()
        plt.close()
        print(f"Sum of residuals: {np.sum(np.abs(residuals))}")
        if not self.test_config.only_plot:
            assert np.equal(data, ref_data).all()

    @pytest.mark.ct_prober
    def test_present_and_parked(self, setup_ct_prober):
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
    def test_ct_source_rejected_without_probe(self, setup):
        '''
        Verify that source='ct' fails with an explicit error on firmware without the probe.
        '''
        logger = self.get_logger()
        if getattr(self.board, 'CT_PROBER', None) is not None:
            pytest.skip("This firmware implements the corner-turn capture probe; "
                        "the rejection path is exercised on corr16/CRS runs")
        logger.debug("test_ct_source_rejected_without_probe: expecting RuntimeError from start_data_capture")
        with pytest.raises(RuntimeError, match='corner-turn capture probe'):
            self.board.start_data_capture(period=self.CAPTURE_PERIOD, source='ct')

    @pytest.mark.ct_prober
    def test_lane_readback(self, lanes, setup_ct_prober):
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
    def test_single_burst(self, lane, setup_ct_prober, xr):
        '''
        Verify that one burst decodes to the advertised geometry with complete counts.

        Parameters:
            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        self._require_ct_dataflow()
        ts, data, counts, geometry = self._set_capture_ct(ncap=self.NCAP, lane=lane)
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
    def test_inner_header(self, lane, setup_ct_prober, xr):
        '''
        Verify that the CT header carried in the payload matches the crossbar configuration.

        Parameters:
            lane (int): corner-turn output lane to capture

        Notes:
            Captures with format='32' so the raw packet words (including the
            4-word CT header and the packet-flags trailer) are visible.
        '''
        logger = self.get_logger()
        self._require_ct_dataflow()
        ts, data, counts, geometry = self._set_capture_ct(
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
    def test_burst_indices_and_timestamps(self, lane, setup_ct_prober, xr):
        '''
        Verify that a multi-packet burst is reassembled from its burst-index nibbles.

        Parameters:
            lane (int): corner-turn output lane to capture
        '''
        logger = self.get_logger()
        self._require_ct_dataflow()
        probe = self.board.CT_PROBER
        geometry = probe.get_packet_geometry(lane)
        frames_per_burst = min(3, probe.DATA_FIFO_DEPTH // geometry['words_per_packet'])
        logger.debug(f"test_burst_indices_and_timestamps: lane={lane} "
                     f"words_per_packet={geometry['words_per_packet']} frames_per_burst={frames_per_burst}")
        if frames_per_burst < 2:
            pytest.skip("Current corner-turn packet size only allows single-packet bursts")
        ts, data, counts, geometry = self._set_capture_ct(
            ncap=self.HEADER_NCAP, lane=lane, frames_per_burst=frames_per_burst)
        assert data.shape[0] == self.HEADER_NCAP * frames_per_burst
        np.testing.assert_array_equal(counts, np.ones_like(counts))
        ts = ts.astype(np.int64)
        for burst in range(self.HEADER_NCAP):
            rows = slice(burst * frames_per_burst, (burst + 1) * frames_per_burst)
            logger.debug(f"test_burst_indices_and_timestamps: burst={burst} ts={ts[rows].tolist()}")
            np.testing.assert_array_equal(np.diff(ts[rows]), 1)

    @pytest.mark.ct_prober
    def test_lane_switch(self, target_lane, setup_ct_prober, xr):
        '''
        Verify that set_data_capture(source='ct', lane=...) retargets the next burst.

        Parameters:
            target_lane (int): lane to switch to on the fly (should differ from lane 0,
                where the capture starts, for the switch to be observable)
        '''
        logger = self.get_logger()
        self._require_ct_dataflow()
        ts, data, counts, _ = self._set_capture_ct(ncap=self.NCAP, lane=0, format='32')
        initial_lane = (int(data[0, 0, 0]) >> 16) & 0xF
        logger.debug(f"test_lane_switch: initial_lane={initial_lane} target_lane={target_lane}")
        assert initial_lane == 0
        self.board.set_data_capture(source='ct', lane=target_lane)
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, counts = receiver.read_raw_frames(ncap=self.NCAP, source=2, format='32',
                                                    chan_first=False)
        switched_lane = (int(data[0, 0, 0]) >> 16) & 0xF
        logger.debug(f"test_lane_switch: switched_lane={switched_lane}")
        assert switched_lane == target_lane, "inner header lane number"

    @pytest.mark.ct_prober
    def test_burst_ceiling_rejected(self, setup_ct_prober):
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
            self.board.start_data_capture(period=self.CAPTURE_PERIOD, source='ct',
                                          frames_per_burst=max_frames + 1)

    @pytest.mark.ct_prober
    def test_constant_pattern_nibble_decode(self, lane, setup_ct_prober, xr):
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
        self._require_ct_dataflow()
        logger.debug(f"test_constant_pattern_nibble_decode: pattern_byte={self.PATTERN_BYTE:#04x}")
        self.board.set_channelizer(
            data_source='arb', fft_bypass=1, scaler_bypass=1,
            offset_binary_encoding=False,
            data=np.full(self.data.NUM_SAMPLES, self.PATTERN_BYTE, dtype=np.uint8))
        self.board.sync()
        ts, raw, counts, geometry = self._set_capture_ct(ncap=self.NCAP, lane=lane, format='32')
        payload_words = raw[0, 0, self.INNER_HEADER_WORDS:self.INNER_HEADER_WORDS + geometry['data_words']]
        payload_bytes = (payload_words.astype(np.int64) & 0xFFFFFFFF).astype('>u4').view(np.uint8)
        logger.debug(f"test_constant_pattern_nibble_decode: payload_first16={payload_bytes[:16]}")
        assert np.unique(payload_bytes).size == 1, "constant input must give a constant payload"
        expected = self._decode_ct_byte(int(payload_bytes[0]), geometry['use_offset_binary'])

        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, counts = receiver.read_raw_frames(ncap=self.NCAP, source=2, chan_first=False)
        ref_data = np.full(data.shape[2], expected, dtype=np.complex64)
        logger.debug(f"test_constant_pattern_nibble_decode: received_first16={data[0, 0, :16]}")
        logger.debug(f"test_constant_pattern_nibble_decode: reference_first16={ref_data[:16]}")

        self.plot_and_test(xr, data[0, 0], ref_data,
                           f'Constant pattern nibble decode (lane={lane}, byte={self.PATTERN_BYTE:#04x})')

    @pytest.mark.ct_prober
    def test_chan_source_roundtrip(self, setup_ct_prober, xr):
        '''
        Verify that switching back to a channelizer source parks the probe and decodes.
        '''
        logger = self.get_logger()
        self._require_ct_dataflow()
        self._set_capture_ct(ncap=self.NCAP, lane=0)
        logger.debug("test_chan_source_roundtrip: switching capture source back to FUNCGEN")
        self.board.start_data_capture(period=self.CAPTURE_PERIOD, source='funcgen')
        capture_en = int(self.board.CT_PROBER.CAPTURE_EN)
        logger.debug(f"test_chan_source_roundtrip: capture_en={capture_en}")
        assert capture_en == 0, "ct probe must be parked by the switch"
        self.board.set_channelizer(data_source='ramp', fft_bypass=1, scaler_bypass=1)
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        self.board.sync()
        ts, data, counts = receiver.read_raw_frames(ncap=self.NCAP, source=0)
        logger.debug(f"test_chan_source_roundtrip: counts={counts.tolist()}")
        assert np.all(counts > 0)

    @pytest.mark.ct_prober
    def test_corner_turn_shuffle16(self, lane, waveform, setup_ct_prober, xr):
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
        self._require_ct_dataflow()
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
        ts, data, counts, geometry = self._set_capture_ct(ncap=self.NCAP, lane=lane)
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
    def test_gpu_link_feed(self, setup_ct_prober, xr):
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
        self._require_ct_dataflow()
        gpu = self.board.GPU
        gpu.OVERFLOW_RESET = 1
        gpu.OVERFLOW_RESET = 0
        a = int(gpu.FRAME_CTR)
        sleep(0.2)
        b = int(gpu.FRAME_CTR)
        data_overflow = int(gpu.DATA_FIFO_OVERFLOW)
        frame_overflow = int(gpu.FRAME_FIFO_OVERFLOW)
        logger.debug(f"test_gpu_link_feed: frame_ctr {a}->{b} advancing={a != b} "
                     f"data_overflow={data_overflow:#04x} frame_overflow={frame_overflow:#04x}")
        assert a != b, "GPU FRAME_CTR is static: corner-turn output is not reaching the 10G links"
        assert data_overflow == 0
        assert frame_overflow == 0
