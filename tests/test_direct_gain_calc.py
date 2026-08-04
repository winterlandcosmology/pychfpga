"""Tests for direct RMS gain calculation."""

import asyncio

import numpy as np
import pytest

from pychfpga.calculate_gains import (
    DEFAULT_GAIN_TARGET,
    DirectGainCalc,
    derive_target_shift,
)
from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor
from pychfpga.common.state import State
from pychfpga.fpga_master import FPGAMaster
from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA
from pychfpga.fpga_firmware.chfpga.f_engine.scaler import SCALER
from pychfpga.raw_acq.fft_data_processor import FFTDataProcessor
from pychfpga.raw_acq.raw_data_processor import _merge_fft_decoder_into_raw


NBINS = 16


class _DummyLog:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class _DummyMetrics:
    def __init__(self):
        self.rows = []

    def add(self, *args, **kwargs):
        self.rows.append((args, kwargs))


class _FakeFFT:
    def __init__(self, bin_map=None):
        self.bin_map = np.asarray(bin_map if bin_map is not None else np.arange(4))

    def get_bin_map(self, flatten=True, reverse=False):
        assert flatten is True
        assert reverse is False
        return self.bin_map


class _FakeScaler:
    CAPTURE_DATA_TYPES = {
        'fft_2x': 4,
        'fft_full': 5,
    }

    def __init__(self, four_bits=1):
        self.FOUR_BITS = four_bits


class _FakeProber:
    def __init__(self, reset=0):
        self.reset_writes = []
        self._reset = int(reset)

    @property
    def RESET(self):
        return self._reset

    @RESET.setter
    def RESET(self, value):
        self._reset = int(value)
        self.reset_writes.append(self._reset)


class _FakeChan:
    def __init__(self, four_bits=1, bin_map=None, prober_reset=0):
        self.SCALER = _FakeScaler(four_bits=four_bits)
        self.FFT = _FakeFFT(bin_map=bin_map)
        self.PROBER = _FakeProber(reset=prober_reset)


class _FakeGPIO:
    def __init__(self, adc_or=0, adc_ov=0, adc_sequence=None):
        object.__setattr__(self, 'clear_writes', [])
        object.__setattr__(self, '_adc_or', adc_or)
        object.__setattr__(self, '_adc_ov', adc_ov)
        object.__setattr__(self, '_adc_sequence', list(adc_sequence or []))
        object.__setattr__(self, '_clear_count', -1)

    def __setattr__(self, name, value):
        if name in ('ADC_OR_CLEAR', 'ADC_OV_CLEAR'):
            self.clear_writes.append((name, value))
            if name == 'ADC_OV_CLEAR' and value == 0 and self._adc_sequence:
                object.__setattr__(self, '_clear_count', self._clear_count + 1)
                ix = min(self._clear_count, len(self._adc_sequence) - 1)
                object.__setattr__(self, '_adc_or', self._adc_sequence[ix])
                object.__setattr__(self, '_adc_ov', 0)
        object.__setattr__(self, name, value)

    @property
    def ADC_OR(self):
        return self._adc_or

    @ADC_OR.setter
    def ADC_OR(self, value):
        object.__setattr__(self, '_adc_or', value)

    @property
    def ADC_OV(self):
        return self._adc_ov

    @ADC_OV.setter
    def ADC_OV(self, value):
        object.__setattr__(self, '_adc_ov', value)


class _FakeUCAP:
    def __init__(self):
        self.outputs = []

    def select_output(self, output):
        self.outputs.append(output)


class _FakeFPGA:
    _PLATFORM_ID_MGK7MB_REV0 = 2
    _PLATFORM_ID_MGK7MB_REV2 = 3
    _PLATFORM_ID_CRS = 5

    def __init__(
            self, bits_per_bin=48, platform_id=2, four_bits=1, bin_map=None,
            gpio=None, required_capture_frames=None, prober_resets=None):
        self.FFT_TYPE = 0
        self.FFT_INFO = {0: {'bits_per_bin': bits_per_bin, 'bins_per_word': 2}}
        self.PLATFORM_ID = platform_id
        self.CAPTURE_TYPE = (
            'UCAP' if platform_id == self._PLATFORM_ID_CRS else 'PROBER')
        self.GPIO = gpio or _FakeGPIO()
        self.UCAP = _FakeUCAP()
        prober_resets = prober_resets or {}
        self.chan = {
            0: _FakeChan(
                four_bits=four_bits, bin_map=bin_map,
                prober_reset=prober_resets.get(0, 0)),
            1: _FakeChan(
                four_bits=four_bits, bin_map=bin_map,
                prober_reset=prober_resets.get(1, 0)),
        }
        self.default_channels = [0, 1]
        self._capture_pkt_frames_per_burst = 1
        self.required_capture_frames = required_capture_frames
        self.config = State(
            raw_capture=State(
                channels=(0, 1),
                source='adc',
                frames_per_burst=1,
            ),
            chan={
                ch: State(
                    data_source='adc',
                    prober_sub_period=23,
                    scaler=State(cap_data_type=0),
                )
                for ch in self.default_channels
            },
        )

    def get_receiver_config(self, channels=None):
        cap_type = int(self.config.chan[int((channels or [0])[0])].scaler.cap_data_type)
        required = self.required_capture_frames
        if required is None:
            required = 4 if cap_type == 5 else 2 if cap_type == 4 else 1
        return State(raw=State(
            packets_per_logical_frame=[1, required],
            capture_format=[
                State(fmt_interleave_factor=1),
                State(fmt_interleave_factor=required),
            ],
        ))


class _FakeBoard:
    def __init__(self, fpga=None):
        self.fpga = fpga or _FakeFPGA()
        self.start_calls = []
        self.stop_count = 0
        self.capture_calls = []
        self.events = None

    def get_id(self, ch=None, default_crate=None, default_slot=None):
        if ch is None:
            return (0, 1)
        return (0, 1, ch)

    def get_channels(self):
        return list(self.fpga.default_channels)

    def get_channel_ids(self, channels):
        return [self.get_id(ch) for ch in channels]

    def get_stream_ids(self, channels):
        return [10 + int(ch) for ch in channels]

    def set_data_capture(self, **kwargs):
        self.capture_calls.append(kwargs)
        channels = kwargs.get('channels', self.fpga.default_channels)
        for ch in channels:
            self.fpga.config.chan[int(ch)].data_source = kwargs['source']
            self.fpga.config.chan[int(ch)].prober_sub_period = int(kwargs['sub_period'])
        self.fpga.config.raw_capture.channels = tuple(channels)
        self.fpga.config.raw_capture.source = kwargs['source']

    def start_data_capture(self, **kwargs):
        self.start_calls.append(kwargs)
        if self.events is not None:
            self.events.append('start')
        channels = list(kwargs['channels'])
        self.fpga.config.raw_capture.channels = tuple(channels)
        self.fpga.config.raw_capture.source = kwargs['source']
        for ch in channels:
            self.fpga.config.chan[int(ch)].data_source = kwargs['source']
        if kwargs['source'] == 'scaler':
            cap_type = kwargs['data_type']
            if isinstance(cap_type, str):
                cap_type = _FakeScaler.CAPTURE_DATA_TYPES[cap_type]
            for ch in channels:
                self.fpga.config.chan[int(ch)].scaler.cap_data_type = int(cap_type)
        if self.fpga.CAPTURE_TYPE == 'PROBER':
            frames = int(kwargs['frames_per_burst'])
            self.fpga._capture_pkt_frames_per_burst = frames
            self.fpga.config.raw_capture.frames_per_burst = frames

    def stop_data_capture(self):
        self.stop_count += 1
        if self.events is not None:
            self.events.append('stop')

class _FakeFpgas:
    def __init__(self, ib):
        self.ib = [ib]
        self.ib_by_id = {ib.get_id(): ib}
        self.set_calls = []
        self.sync_count = 0
        self.events = None

    def get_iceboards(self, targets, lane_type='chan'):
        assert lane_type == 'chan'
        if targets is None:
            return {0: self.ib[0]}
        return {self.ib[0]: [0]}

    def sync(self, **kwargs):
        self.sync_count += 1
        if self.events is not None:
            self.events.append('sync')

    def get_iceboard_from_id(self, board_id):
        return self.ib_by_id[tuple(board_id)]

    async def set_gains_async(self, gains, bank=0, when='now'):
        self.set_calls.append((gains, bank, when))


class _FakeGainServer:
    def __init__(self, rms):
        self.rms = np.asarray(rms, dtype=np.float64)
        self.starts = []

    async def start_fft_rms(self, **kwargs):
        self.starts.append(kwargs)

    async def get_fft_rms(self):
        return [10], self.rms.tolist()


def _fake_master(ib, server=None):
    master = object.__new__(FPGAMaster)
    master.log = _DummyLog()
    master.gain_calc_metrics = _DummyMetrics()
    master.gain_hdf5 = None
    master.fpgas = _FakeFpgas(ib)
    master.raw_acq = {'raw0': server} if server is not None else {}
    master.raw_acq_ibs = {'raw0': [ib]} if server is not None else {}
    master.config = State(
        fpga=State(
            compute_gains=State(
                data_capture=State(capture_rate=0),
            ),
            sync_params=State(enable=False),
        ),
    )
    master.SECONDS_PER_FRAME = 0
    master._gain_operation_lock = asyncio.Lock()
    master.capture_reset_calls = []
    master.raw_acq_sync_count = 0
    master.events = []
    ib.events = master.events
    master.fpgas.events = master.events

    async def sync_raw_acq_config():
        master.raw_acq_sync_count += 1
        master.events.append('decoder')

    async def set_fpga_data_capture(**kwargs):
        master.capture_reset_calls.append(kwargs)

    master._sync_raw_acq_firmware_config = sync_raw_acq_config
    master.set_fpga_data_capture = set_fpga_data_capture
    return master


def _ice_capture_profile(*, gain):
    return State(
        capture_source='scaler' if gain else 'adc',
        baseline_capture_rate=10,
        capture_rate=12 if gain else 23,
        tmux_factor=3,
        scaler_cap_data_type='fft_full' if gain else 0,
        frames_per_burst=4 if gain else 1,
    )


def _crs_capture_profile(*, gain):
    return State(
        capture_source='scaler' if gain else 'adc',
        baseline_capture_rate=10,
        capture_rate=12 if gain else 23,
        tmux_factor=3,
        scaler_cap_data_type='fft_2x' if gain else 0,
        ucap_mode=2,
        ucap_output='cap' if gain else 'corr',
    )


def test_start_transmission_uses_ice_full_resolution_gain_geometry():
    ib = _FakeBoard(_FakeFPGA(
        platform_id=_FakeFPGA._PLATFORM_ID_MGK7MB_REV2))
    master = _fake_master(ib)

    asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
        master,
        capture_config=_ice_capture_profile(gain=True),
        gain_capture=True,
        sync=True))

    assert ib.start_calls[0]['frames_per_burst'] == 4
    assert ib.start_calls[0]['source'] == 'scaler'
    assert ib.start_calls[0]['data_type'] == 'fft_full'
    assert ib.capture_calls == [{
        'channels': [0, 1],
        'sub_period': 12,
        'source': 'scaler',
    }]
    assert master.raw_acq_sync_count == 1
    assert master.fpgas.sync_count == 1


def test_set_capture_without_targets_uses_all_board_channels():
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib)

    asyncio.run(FPGAMaster.set_fpga_data_capture(
        master, capture_rate=17, source='adc'))

    assert ib.capture_calls == [{
        'channels': [0, 1],
        'sub_period': 17,
        'source': 'adc',
    }]


def test_start_transmission_rejects_wrong_ice_gain_burst_before_writes():
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib)
    profile = _ice_capture_profile(gain=True)
    profile.frames_per_burst = 3

    with pytest.raises(RuntimeError, match='requires frames_per_burst=4'):
        asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
            master, capture_config=profile, gain_capture=True))

    assert ib.start_calls == []


def test_start_transmission_selects_crs_gain_and_normal_outputs():
    ib = _FakeBoard(_FakeFPGA(
        bits_per_bin=64, platform_id=_FakeFPGA._PLATFORM_ID_CRS))
    master = _fake_master(ib)

    asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
        master,
        capture_config=_crs_capture_profile(gain=True),
        gain_capture=True,
        sync=True))
    asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
        master,
        capture_config=_crs_capture_profile(gain=False),
        gain_capture=False,
        sync=True))

    assert ib.fpga.UCAP.outputs == ['cap', 'corr']
    assert [call['source'] for call in ib.start_calls] == [
        'scaler', 'funcgen']


def test_initial_profile_starts_without_stopping_capture():
    ib = _FakeBoard(_FakeFPGA(
        platform_id=_FakeFPGA._PLATFORM_ID_MGK7MB_REV2))
    master = _fake_master(ib, _FakeGainServer(np.ones((1, 4))))

    asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
        master,
        capture_config=_ice_capture_profile(gain=True),
        gain_capture=True,
        push_decoder=True,
        sync=True))

    assert ib.stop_count == 0
    assert len(ib.start_calls) == 1
    assert master.raw_acq_sync_count == 1
    assert master.fpgas.sync_count == 1
    assert master.events == ['start', 'decoder', 'sync']


def test_decoder_update_failure_prevents_sync():
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib, _FakeGainServer(np.ones((1, 4))))

    async def fail_decoder_update():
        raise RuntimeError("decoder update failed")

    master._sync_raw_acq_firmware_config = fail_decoder_update

    with pytest.raises(RuntimeError, match="decoder update failed"):
        asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
            master,
            capture_config=_ice_capture_profile(gain=True),
            gain_capture=True,
            push_decoder=True,
            sync=True))

    assert ib.stop_count == 0
    assert len(ib.start_calls) == 1
    assert master.fpgas.sync_count == 0


def test_invalid_live_capture_profile_is_rejected_before_hardware_writes():
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib, _FakeGainServer(np.ones((1, 4))))
    profile = _ice_capture_profile(gain=False)
    profile.baseline_capture_rate = "invalid"

    with pytest.raises(ValueError, match="Invalid raw data capture profile"):
        asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
            master,
            capture_config=profile,
            push_decoder=True,
            sync=True))

    assert ib.stop_count == 0
    assert ib.start_calls == []
    assert ib.capture_calls == []
    assert master.raw_acq_sync_count == 0
    assert master.fpgas.sync_count == 0


def test_invalid_capture_sub_period_is_rejected_before_hardware_writes():
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib, _FakeGainServer(np.ones((1, 4))))
    profile = _ice_capture_profile(gain=False)
    profile.capture_rate = 32

    with pytest.raises(ValueError, match="capture_rate"):
        asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
            master,
            capture_config=profile,
            push_decoder=True,
            sync=True))

    assert ib.stop_count == 0
    assert ib.start_calls == []
    assert master.events == []


def test_zero_baseline_rate_configures_but_does_not_activate():
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib)
    profile = _ice_capture_profile(gain=False)
    profile.baseline_capture_rate = 0

    asyncio.run(FPGAMaster.start_fpga_raw_data_transmission(
        master,
        capture_config=profile,
        push_decoder=False,
        sync=True))

    assert ib.start_calls == []
    assert ib.stop_count == 1
    assert master.fpgas.sync_count == 0


class _NaturalOrderFFT:
    def __init__(self, bin_map):
        self.bin_map = np.asarray(bin_map)

    def get_bin_map(self, flatten=True, reverse=True):
        assert flatten is True
        assert reverse is True
        return self.bin_map


def test_float_gain_word_decode_returns_quantized_natural_order_values():
    scaler = object.__new__(SCALER)
    object.__setattr__(scaler, 'fpga', State(
        chan={0: State(FFT=_NaturalOrderFFT([2, 0, 3, 1]))},
    ))
    words = np.array([
        (0 << 11) | 100,
        (1 << 11) | 200,
        (2 << 11) | 300,
        (3 << 11) | 400,
    ], dtype=np.uint16)

    decoded = SCALER._decode_float_gain_words(
        scaler, words, shift_left=4)

    firmware_order = np.array([
        100 * 2**4,
        200 * 2**5,
        300 * 2**6,
        400 * 2**7,
    ], dtype=float)
    np.testing.assert_array_equal(
        decoded, firmware_order[[2, 0, 3, 1]])


@pytest.mark.parametrize('target_shift', [36, 44])
def test_set_float_gain_table_encodes_ice_and_crs_one_lsb_gains(target_shift):
    class FakeScaler:
        USE_COMPLEX_GAINS = False

        def __init__(self):
            self.fpga = State(
                NUMBER_OF_FREQUENCY_BINS=256,
                chan={0: State(FFT=_NaturalOrderFFT(np.arange(256)))},
            )
            self.cached_gain_table = {}
            self.cached_gain_log = {}
            self.cached_gain_timestamp = {}
            self.writes = []

        def _decode_float_gain_words(self, words, shift_left):
            return SCALER._decode_float_gain_words(self, words, shift_left)

        def set_page(self, page):
            self.page = page

        def write_ram(self, addr, data):
            self.writes.append((self.page, addr, bytes(data)))

    scaler = FakeScaler()
    one_lsb_gain = DEFAULT_GAIN_TARGET * 2**target_shift
    requested = np.linspace(one_lsb_gain / 4, one_lsb_gain, 256)

    SCALER.set_gain_table(
        scaler, requested, bank=0, gain_timestamp=123.0, log_gain=None)

    assert len(scaler.writes) == 1
    assert scaler.cached_gain_log[0] == -1
    assert scaler.cached_gain_timestamp[0] == 123.0
    assert scaler.cached_gain_table[0].shape == (256,)
    assert np.all(np.isfinite(scaler.cached_gain_table[0]))
    np.testing.assert_allclose(
        scaler.cached_gain_table[0], requested, rtol=2 / 2**11)
    assert not np.array_equal(
        scaler.cached_gain_table[0],
        np.frombuffer(scaler.writes[0][2], dtype='<u2').astype(float),
    )


def test_uncached_real_gain_read_uses_firmware_bin_count():
    class FakeScaler:
        cached_gain_table = {}
        USE_COMPLEX_GAINS = False
        USE_FLOAT_GAINS = False
        fpga = State(NUMBER_OF_FREQUENCY_BINS=1024)

        def __init__(self):
            self.reads = []

        def read_ram(self, addr, length, type):
            self.reads.append((addr, length, type))
            return np.arange(length, dtype=np.uint16)

    scaler = FakeScaler()

    gains = SCALER.get_gain_table(scaler, bank=0, use_cache=False)

    assert len(scaler.reads) == 4
    assert gains.shape == (1024,)


def test_board_cached_gain_getter_does_not_read_gain_mode_registers():
    class ReadFreeScaler:
        cached_gain_log = {0: -1}

        @property
        def USE_FLOAT_GAINS(self):
            raise AssertionError('USE_FLOAT_GAINS register was read')

        @property
        def SHIFT_LEFT(self):
            raise AssertionError('SHIFT_LEFT register was read')

        def get_gain_table(self, bank, use_cache):
            assert bank == 0
            assert use_cache is True
            return np.array([10.0, 20.0])

    board = State(
        slot=0,
        logger=_DummyLog(),
        get_channelizers=lambda: [State(
            chan_number=3,
            SCALER=ReadFreeScaler(),
        )],
    )

    gains = chFPGA.get_gains(board, bank=0, use_cache=True)

    assert gains[0][0] == 3
    np.testing.assert_array_equal(gains[0][1][0], [10.0, 20.0])
    assert gains[0][1][1] == -1


def test_save_gains_uses_decoded_quantized_scaler_cache():
    class FakeFpgas:
        def __init__(self):
            self.use_cache = None

        async def get_gains_async(self, bank, use_cache):
            self.use_cache = use_cache
            return {(0, 1, 0): (np.array([10.0, 20.0]), -1)}

        async def get_gain_timestamps_async(self, bank):
            return {(0, 1, 0): 123.0}

    class FakeArchive:
        archive_files = ['/tmp/gains.h5']

        def set_gain(self, gains, compute_time):
            self.gains = gains
            self.compute_time = compute_time

        def write(self, smp, run_name):
            self.write_args = (smp, run_name)

    master = object.__new__(FPGAMaster)
    master.log = _DummyLog()
    master.fpgas = FakeFpgas()
    master.gain_hdf5 = FakeArchive()
    master.run_name = 'test-run'
    master._chan_id_to_serial_number = lambda cid: f'input-{cid[-1]}'

    asyncio.run(FPGAMaster.save_gains(master, bank=0))

    assert master.fpgas.use_cache is True
    np.testing.assert_array_equal(
        master.gain_hdf5.gains['input-0'][0], [10.0, 20.0])


def test_load_gains_scrambles_natural_order_archive_for_firmware():
    channel_id = (0, 1, 0)
    board = _FakeBoard(_FakeFPGA(bin_map=[2, 0, 3, 1]))

    class FakeFpgas:
        ib = [board]

        def __init__(self):
            self.set_call = None

        def get_channel_ids(self):
            return [channel_id]

        def get_iceboard_from_id(self, board_id):
            assert tuple(board_id) == channel_id[:2]
            return board

        async def set_gains_async(self, gains, **kwargs):
            self.set_call = (gains, kwargs)

    class FakeArchive:
        last_update = 'latest'
        reader = State(last_value=State(filename='/tmp/gains.h5'))

        def read(self, update_id, field):
            assert (update_id, field) == ('latest', 'update_id')
            return 'gain-uid'

        def read_gain(self, update_id):
            assert update_id == 'gain-uid'
            return (
                {'input-0': (np.array([10., 20., 30., 40.]), -1)},
                {'input-0': 123.},
            )

    master = object.__new__(FPGAMaster)
    master.log = _DummyLog()
    master.fpgas = FakeFpgas()
    master.gain_hdf5 = FakeArchive()
    master._chan_id_to_serial_number = lambda cid: f'input-{cid[-1]}'

    uid = asyncio.run(FPGAMaster.load_gains(master))

    gains, kwargs = master.fpgas.set_call
    np.testing.assert_array_equal(
        gains[channel_id][0], [30., 10., 40., 20.])
    assert gains[channel_id][1] == -1
    assert kwargs == {
        'bank': 0,
        'when': 'now',
        'gain_timestamps': {channel_id: 123.},
    }
    assert uid == 'gain-uid'


def _runtime_master(*, fail_gain=False, block_gain=None):
    events = []
    master = object.__new__(FPGAMaster)
    master.log = _DummyLog()
    master._gain_operation_lock = asyncio.Lock()
    master.fpgas = State(
        sync_timestamp='science-sync',
        ib=[State(fpga=State(CAPTURE_TYPE='PROBER'))])
    master.frame0_irigb_time = None
    master.config = State(
        raw_acq=State(
            enable=True,
            common_config=State(hdf5_capture_duration=0),
        ),
        fpga=State(
            compute_gains=State(
                enable=True,
                targets=[('*',)],
                number_of_fft_averages=1000,
                target=DEFAULT_GAIN_TARGET,
                save_gains=True,
                data_capture=_ice_capture_profile(gain=True),
            ),
            raw_data_capture=_ice_capture_profile(gain=False),
            firmware_correlator=State(
                enable=True,
                hdf5_capture_duration=0,
            ),
        ),
    )
    master.raw_acq = {'raw0': object()}

    async def set_capture(**kwargs):
        events.append(('set-capture', kwargs))

    async def compute(**kwargs):
        events.append(('compute', kwargs))
        if block_gain is not None:
            await block_gain.wait()
        if fail_gain:
            raise ValueError('gain input RMS is invalid')

    master.set_fpga_data_capture = set_capture
    master.compute_gains = compute
    return master, events


def test_runtime_gain_phase_switches_source_without_stopping_writers():
    master, events = _runtime_master()

    asyncio.run(FPGAMaster.runtime_compute_gains(
        master,
        target=3.0,
        data_capture={'capture_rate': 14},
    ))

    assert [event[0] for event in events] == [
        'set-capture',
        'compute',
        'set-capture',
    ]
    assert events[0][1] == {'capture_rate': 14, 'source': 'scaler'}
    assert events[1][1]['target'] == 3.0
    assert events[2][1] == {'capture_rate': 23, 'source': 'adc'}
    assert master.config.fpga.raw_data_capture.capture_rate == 23
    assert master.frame0_irigb_time is None


def test_runtime_gain_failure_restores_normal_source():
    master, events = _runtime_master(fail_gain=True)

    with pytest.raises(ValueError, match='gain input RMS is invalid'):
        asyncio.run(FPGAMaster.runtime_compute_gains(master))

    assert [event[0] for event in events] == [
        'set-capture',
        'compute',
        'set-capture',
    ]
    assert events[-1][1] == {'capture_rate': 23, 'source': 'adc'}


def test_runtime_gain_rejects_a_second_active_request():
    async def exercise():
        release = asyncio.Event()
        master, events = _runtime_master(block_gain=release)
        first = asyncio.create_task(FPGAMaster.runtime_compute_gains(master))
        while not master._gain_operation_lock.locked():
            await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match='already in progress'):
            await FPGAMaster.runtime_compute_gains(master)
        release.set()
        await first
        return events

    events = asyncio.run(exercise())
    assert sum(
        event[0] == 'set-capture' and event[1]['source'] == 'scaler'
        for event in events) == 1


def test_runtime_gain_requires_running_raw_acq():
    master, events = _runtime_master()
    master.raw_acq = {}

    with pytest.raises(RuntimeError, match='requires enabled and running raw_acq'):
        asyncio.run(FPGAMaster.runtime_compute_gains(master))

    assert events == []


def test_runtime_gain_rejects_active_ucap_transition():
    master, events = _runtime_master()
    master.fpgas.ib[0].fpga.CAPTURE_TYPE = 'UCAP'

    with pytest.raises(RuntimeError, match='supported only for PROBER'):
        asyncio.run(FPGAMaster.runtime_compute_gains(master))

    assert events == []


def test_runtime_serial_gain_uses_one_capture_phase_for_all_groups():
    master, events = _runtime_master()
    master.config.fpga.compute_gains.targets = [
        (0, '*', '*'),
        (1, '*', '*'),
    ]

    asyncio.run(FPGAMaster.serial_compute_gains(master))

    compute_events = [event for event in events if isinstance(event, tuple)
                      and event[0] == 'compute']
    assert [event[1]['targets'] for event in compute_events] == [
        [[0, '*', '*']],
        [[1, '*', '*']],
    ]
    assert sum(event[0] == 'set-capture' for event in events) == 2


def test_runtime_gain_rejects_static_capture_override():
    master, events = _runtime_master()

    with pytest.raises(ValueError, match='static field'):
        asyncio.run(FPGAMaster.runtime_compute_gains(
            master,
            data_capture={'frames_per_burst': 2}))

    assert events == []


@pytest.mark.parametrize('removed_argument', ['capture_rate', 'scaler_cap_data_type'])
def test_compute_gains_rejects_removed_flat_capture_arguments(removed_argument):
    ib = _FakeBoard(_FakeFPGA())
    master = _fake_master(ib)

    with pytest.raises(TypeError, match=removed_argument):
        FPGAMaster.compute_gains(master, **{removed_argument: 12})


def test_derive_target_shift_uses_fft_metadata():
    ice = _FakeFPGA(bits_per_bin=48)
    crs = _FakeFPGA(bits_per_bin=64, platform_id=_FakeFPGA._PLATFORM_ID_CRS)

    assert derive_target_shift(ice) == 36
    assert derive_target_shift(crs) == 44


def test_fpga_master_target_shift_rejects_non_four_bit_corr_output():
    ib = _FakeBoard(_FakeFPGA(four_bits=0))
    server = _FakeGainServer(np.ones((1, 4)))
    master = _fake_master(ib, server)

    with pytest.raises(RuntimeError, match='4-bit corr output'):
        asyncio.run(FPGAMaster.compute_gains(
            master,
            targets=[('*',)],
            number_of_fft_averages=1,
        ))


def test_direct_gain_formula_uses_rms_and_target_shift():
    gc = DirectGainCalc(
        channel_ids=[(0, 1, 0)],
        stream_ids=[10],
        target=2.0,
        target_shift=36,
        n_bins=4,
    )
    rms = np.array([[100.0, 200.0, 400.0, 800.0]])
    gains = gc.update_gains(np.array([10]), rms)

    gain_vec, glog = gains[(0, 1, 0)]
    assert glog == -1
    np.testing.assert_allclose(gain_vec, 2.0 * 2**36 / rms[0])
    filtered, mask = gc.get_filtered_gains()
    np.testing.assert_allclose(filtered[(0, 1, 0)][0], gain_vec)
    assert not mask.any()


def test_direct_gain_supports_per_channel_target_shift():
    gc = DirectGainCalc(
        channel_ids=[(0, 1, 0), (0, 1, 1)],
        stream_ids=[10, 11],
        target=1.0,
        target_shift=[36, 44],
        n_bins=4,
    )
    rms = np.array([
        [0.0, 0.1, 0.999, 1.0],
        [0.0, 0.1, 0.999, 2.0],
    ])
    gc.update_gains(np.array([10, 11]), rms)

    expected_rms = np.maximum(rms, 1.0)
    np.testing.assert_allclose(
        gc.gains,
        np.array([2**36, 2**44])[:, np.newaxis] / expected_rms,
    )
    _, mask = gc.get_filtered_gains()
    np.testing.assert_array_equal(mask, rms < 1.0)


def test_direct_gain_mask_rows_follow_completed_channel_order():
    gc = DirectGainCalc(
        channel_ids=[(0, 1, 0), (0, 1, 1)],
        stream_ids=[10, 11],
        target=1.0,
        target_shift=[36, 44],
        n_bins=4,
    )
    gc.update_gains(np.array([11]), np.array([[0.0, 1.0, 0.5, 2.0]]))

    gains, mask = gc.get_filtered_gains()

    assert list(gains) == [(0, 1, 1)]
    np.testing.assert_array_equal(mask, [[True, False, True, False]])


@pytest.mark.parametrize('invalid_rms', [-1.0, np.nan, np.inf, -np.inf])
def test_direct_gain_rejects_negative_or_nonfinite_rms_without_mutation(
        invalid_rms):
    gc = DirectGainCalc(channel_ids=[(0, 1, 0)], stream_ids=[10])
    rms = np.array([[1.0, 0.0, invalid_rms, 4.0]])

    with pytest.raises(ValueError, match='negative or non-finite'):
        gc.update_gains(np.array([10]), rms)

    assert gc.gains is None
    assert gc.n_bins is None
    assert not hasattr(gc, 'resolution_limited')
    assert not gc.done.any()


class _MockRawProcessor:
    NCHAN = 1
    stream_ids = (10,)
    NUMBER_OF_SUBFRAMES = 1
    config = State(
        packets_per_logical_frame=1,
        capture_format=[
            None,
            State(fmt_bus_width=32, fmt_data_width=32, fmt_is_complex=True, fmt_interleave_factor=2),
        ],
    )


class _DummyPacketReceiver:
    def __init__(self, n_packets=4, max_packet_size=256):
        self.n_packets = n_packets
        self.max_packet_size = max_packet_size
        self.buf = np.zeros((n_packets, max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((n_packets,), dtype=np.uint32)
        self.n_stored = 0

    def flush(self, **kwargs):
        self.pkt_len.fill(0)
        self.n_stored = 0
        return 0


@pytest.fixture
def fft_processor():
    mock_raw = _MockRawProcessor()
    return FFTDataProcessor(_DummyPacketReceiver(), mock_raw, stream_ids=(10,))


def _start_fft_rms(proc, n_bins, n_frames):
    proc._alloc_fft_rms_buffers(proc.NCHAN, n_bins)
    asyncio.run(proc.start_fft_rms_async(
        list(proc.stream_ids),
        target_gain_bank=0,
        number_of_frames=n_frames))


def _emit_complex_frames(proc, spectra, counts_val=1):
    n_frames, nchan, _ = spectra.shape
    ts = np.arange(n_frames, dtype=np.uint64)
    counts = np.full((n_frames, nchan), counts_val, dtype=np.int64)
    indices = np.arange(n_frames)
    proc.consume((ts, spectra, counts, indices))


def test_fft_rms_averages_power_across_multiple_frames(fft_processor):
    proc = fft_processor
    n_bins = 4
    n_frames = 3

    _start_fft_rms(proc, n_bins, n_frames)

    frame0 = np.array([1 + 0j, 2 + 0j, 3 + 0j, 4 + 0j], dtype=np.complex64)
    frame1 = np.array([3 + 0j, 4 + 0j, 5 + 0j, 6 + 0j], dtype=np.complex64)
    frame2 = np.array([5 + 0j, 6 + 0j, 7 + 0j, 8 + 0j], dtype=np.complex64)
    spectra = np.stack([frame0, frame1, frame2])[:, np.newaxis, :]
    _emit_complex_frames(proc, spectra)

    expected = np.sqrt(
        (np.abs(frame0)**2 + np.abs(frame1)**2 + np.abs(frame2)**2) / n_frames)
    assert proc.fft_rms_done[0]
    np.testing.assert_allclose(proc.fft_rms[0], expected, rtol=1e-5)


def test_get_fft_rms_returns_completed_channels(fft_processor):
    proc = fft_processor
    _start_fft_rms(proc, n_bins=4, n_frames=1)
    _emit_complex_frames(proc, np.array([[[1 + 1j, 2 + 0j, 3 + 0j, 4 + 0j]]], dtype=np.complex64))

    sid, rms = asyncio.run(proc.get_fft_rms_async())

    assert sid.tolist() == [10]
    np.testing.assert_allclose(rms[0, 0], np.sqrt(2), rtol=1e-5)


def test_start_fft_rms_rejects_non_complex_decoder():
    mock_raw = _MockRawProcessor()
    mock_raw.config = State(
        packets_per_logical_frame=1,
        capture_format=[
            State(fmt_bus_width=16, fmt_data_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
            State(fmt_bus_width=16, fmt_data_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
        ],
    )
    proc = FFTDataProcessor(_DummyPacketReceiver(), mock_raw, stream_ids=(10,))
    with pytest.raises(RuntimeError, match='complex capture_format'):
        asyncio.run(proc.start_fft_rms_async([10], target_gain_bank=0, number_of_frames=2))


def test_fft_rms_accepts_ucap_split_frame_counts():
    mock_raw = _MockRawProcessor()
    mock_raw.NUMBER_OF_SUBFRAMES = 4
    mock_raw.config = State(
        packets_per_logical_frame=2,
        capture_format=[
            None,
            State(fmt_bus_width=32, fmt_data_width=32, fmt_is_complex=True, fmt_interleave_factor=1),
        ],
    )
    proc = FFTDataProcessor(_DummyPacketReceiver(), mock_raw, stream_ids=(10,))
    n_bins = 8
    n_frames = 2

    _start_fft_rms(proc, n_bins, n_frames)

    half0 = np.array([1 + 0j, 2 + 0j, 3 + 0j, 4 + 0j], dtype=np.complex64)
    half1 = np.array([5 + 0j, 6 + 0j, 7 + 0j, 8 + 0j], dtype=np.complex64)
    full = np.concatenate([half0, half1])
    for _ in range(n_frames):
        _emit_complex_frames(proc, half0.reshape(1, 1, 4), counts_val=4)
        _emit_complex_frames(proc, half1.reshape(1, 1, 4), counts_val=4)

    assert proc.fft_rms_done[0]
    np.testing.assert_allclose(proc.fft_rms[0], np.abs(full), rtol=1e-5)


def test_fft_rms_rejects_non_complex():
    with pytest.raises(TypeError, match='complex spectra'):
        FFTDataProcessor._bin_power(np.arange(8, dtype=np.float32))


def test_merge_fft_decoder_into_raw_complex_format():
    raw = State(
        source='funcgen',
        capture_format=[
            State(fmt_bus_width=16, fmt_data_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
            State(fmt_bus_width=16, fmt_data_width=16, fmt_is_complex=False, fmt_interleave_factor=1),
        ],
        packets_per_logical_frame=1,
    )
    fft = State(
        source='scaler',
        capture_format=[
            None,
            State(fmt_bus_width=32, fmt_data_width=32, fmt_is_complex=True, fmt_interleave_factor=2),
        ],
        packets_per_logical_frame=2,
        offset_encoding=True,
    )
    merged = _merge_fft_decoder_into_raw(raw, fft)
    assert merged.source == 'scaler'
    assert merged.packets_per_logical_frame == 2
    assert merged.capture_format[1].fmt_is_complex is True
    assert merged.capture_format[1].fmt_interleave_factor == 2


def test_formatter_bin_map_and_offset_encoding():
    proc = object.__new__(RawDataPacketProcessor)
    proc.config = State(
        offset_encoding=True,
        fft_bin_map=np.array([2, 0, 3, 1], dtype=np.intp),
    )
    bias = 1 << 15
    scrambled = np.array(
        [complex(bias + 100, bias), complex(bias + 200, bias),
         complex(bias + 300, bias), complex(bias + 400, bias)],
        dtype=np.complex64,
    )
    data = scrambled.reshape(1, 1, 4)
    out = proc._apply_fft_formatter_compensation(data, source=1, bit_width=16)
    expected = np.array([300 + 0j, 100 + 0j, 400 + 0j, 200 + 0j], dtype=np.complex64)
    np.testing.assert_allclose(out[0, 0], expected, rtol=0, atol=1e-3)


def test_full_resolution_complex_formatter_reconstructs_lanes():
    proc = object.__new__(RawDataPacketProcessor)
    proc.config = State(
        capture_format=[
            None,
            State(fmt_bus_width=32, fmt_data_width=29, fmt_is_complex=True, fmt_interleave_factor=4),
        ],
        fft_bin_map=np.array([0, 2, 1, 3], dtype=np.intp),
        offset_encoding=False,
    )
    proc.FRAMES_PER_BURST = 4
    proc.CAPTURE_BITS_PER_WORD = 32
    payload = np.array(
        [
            [
                [
                    10, 1,
                    20, 2,
                    30, 3,
                    40, 4,
                ],
            ],
        ],
        dtype=np.int32,
    )
    proc.raw_data = (payload << 3).astype(">i4").view(np.uint8)
    proc.ts_array = np.array([100], dtype=np.uint64)
    proc.raw_data_count = np.ones((1, 1, 4), dtype=np.int64)
    proc.raw_source = np.ones((1, 1, 4), dtype=np.uint8)
    proc.fmt_cdata1 = [
        None,
        np.empty((4, 1, 1), dtype=np.complex64),
    ]
    proc.fmt_cdata2 = [
        None,
        np.empty((1, 1, 4), dtype=np.complex64),
    ]

    ts, data, counts = proc.get_formatted_data(source=1)

    np.testing.assert_array_equal(ts, np.array([100], dtype=np.uint64))
    np.testing.assert_array_equal(counts, np.array([[1]], dtype=np.int64))
    expected = np.array([10 + 1j, 20 + 2j, 30 + 3j, 40 + 4j], dtype=np.complex64)
    np.testing.assert_allclose(data[0, 0], expected, rtol=0, atol=0)


def test_fpga_master_scrambles_gains_before_firmware_write():
    ib = _FakeBoard(_FakeFPGA(bin_map=[2, 0, 3, 1]))
    master = _fake_master(ib)
    gains = {
        (0, 1, 0): (np.array([10.0, 20.0, 30.0, 40.0]), -1),
    }

    mapped = FPGAMaster._scramble_gains_for_firmware_order(master, gains)

    np.testing.assert_array_equal(mapped[(0, 1, 0)][0], np.array([30.0, 10.0, 40.0, 20.0]))
    assert mapped[(0, 1, 0)][1] == -1


def test_compute_gains_retries_adc_flags_and_writes_firmware_order():
    gpio = _FakeGPIO(adc_sequence=[1, 0])
    ib = _FakeBoard(_FakeFPGA(bin_map=[1, 0, 3, 2], gpio=gpio))
    rms = np.array([[100.0, 200.0, 400.0, 800.0]])
    server = _FakeGainServer(rms)
    master = _fake_master(ib, server)

    asyncio.run(FPGAMaster.compute_gains(
        master,
        targets=[('*',)],
        number_of_fft_averages=1,
        target=DEFAULT_GAIN_TARGET,
    ))

    assert len(server.starts) == 2
    assert len(master.fpgas.set_calls) == 1
    gains, bank, when = master.fpgas.set_calls[0]
    assert bank == 0
    assert when == 'now'
    gain_vec, glog = gains[(0, 1, 0)]
    natural = DEFAULT_GAIN_TARGET * 2**36 / rms[0]
    np.testing.assert_allclose(gain_vec, natural[[1, 0, 3, 2]])
    assert glog == -1
    assert gpio.clear_writes[:4] == [
        ('ADC_OR_CLEAR', 0xff),
        ('ADC_OR_CLEAR', 0),
        ('ADC_OV_CLEAR', 0xff),
        ('ADC_OV_CLEAR', 0),
    ]
    assert ib.start_calls == []
    assert master.raw_acq_sync_count == 0


def test_compute_gains_fails_after_adc_guard_retries():
    gpio = _FakeGPIO(adc_sequence=[1] * 10)
    ib = _FakeBoard(_FakeFPGA(gpio=gpio))
    server = _FakeGainServer(np.array([[100.0, 200.0, 400.0, 800.0]]))
    master = _fake_master(ib, server)

    with pytest.raises(RuntimeError, match='ADC flags remained set after 10'):
        asyncio.run(FPGAMaster.compute_gains(
            master,
            targets=[('*',)],
            number_of_fft_averages=1,
        ))

    assert len(server.starts) == 10
    assert master.fpgas.set_calls == []
    assert ib.start_calls == []
    assert master.raw_acq_sync_count == 0


def test_compute_gains_applies_one_lsb_floor_to_sub_lsb_rms():
    ib = _FakeBoard(_FakeFPGA())
    rms = np.array([[100.0, 0.0, 0.5, 1.0]])
    server = _FakeGainServer(rms)
    master = _fake_master(ib, server)

    asyncio.run(FPGAMaster.compute_gains(
        master,
        targets=[('*',)],
        number_of_fft_averages=1,
    ))

    assert len(master.fpgas.set_calls) == 1
    gains, bank, when = master.fpgas.set_calls[0]
    gain_vec, glog = gains[(0, 1, 0)]
    target_full = DEFAULT_GAIN_TARGET * 2**36
    np.testing.assert_allclose(
        gain_vec,
        target_full / np.maximum(rms[0], 1.0),
    )
    assert glog == -1
    assert bank == 0
    assert when == 'now'
    assert ib.start_calls == []
    assert master.raw_acq_sync_count == 0
