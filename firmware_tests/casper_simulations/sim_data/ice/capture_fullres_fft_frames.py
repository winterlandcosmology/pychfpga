from pathlib import Path
import asyncio
import os
import re
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
FIRMWARE_TESTS_DIR = HERE.parents[2]
REPO_ROOT = FIRMWARE_TESTS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from firmware_tests.casper_simulations.caspersimtools import InputSignal
from pychfpga.fpga_array import FPGAArray
from wtl.config import load_yaml_config


CONFIG = Path(os.environ.get("PYCHFPGA_CONFIG", FIRMWARE_TESTS_DIR / "config_ice.yaml"))
INPUT_DIR = HERE

# ============================================================================
# EDIT THIS BLOCK BEFORE EACH RUN
#
# Partial-bitgrowth bitstream: FFT_SHIFT = 1984, FFT_OUTPUT_BITS = 24
# Old bitstream:               FFT_SHIFT = 2047, FFT_OUTPUT_BITS = 18
# Old bitstream:               FFT_SHIFT = 1367, FFT_OUTPUT_BITS = 18
#
# The actual bitstream comes from firmware_tests/config_ice.yaml. Change that
# file between runs, not this script.
# ============================================================================
FFT_SHIFT = 1367
FFT_OUTPUT_BITS = 18
INPUT_STEMS = ("ramp", "two_tone", "noise")
CHANNEL = 0
NCAP = 4
CAPTURE_PERIOD = 0.01
CAP_DATA_TYPE = "fft_full"
OUTPUT_DIR = INPUT_DIR / "captures"
# ============================================================================


def config_value(config, key, default=None):
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def safe_label(label):
    label = str(label)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "unknown"


def bitstream_label(connection_config):
    bitfile = config_value(connection_config, "bitfile")
    if bitfile:
        return safe_label(Path(str(bitfile)).stem)
    mode = config_value(connection_config, "mode", "unknown_mode")
    return safe_label(f"{mode}_default")


def integer_parts(values, label):
    rounded = np.rint(values)
    fraction = values - rounded
    if np.any(fraction != 0):
        raise RuntimeError(
            f"Captured {label} output has non-integer values; "
            f"max discarded fraction={np.max(np.abs(fraction))}"
        )
    return rounded.astype(np.int64)


def get_capture_bits_per_word(board, receiver):
    receiver_config = getattr(receiver, "config", None)
    if receiver_config is not None:
        if isinstance(receiver_config, dict):
            value = receiver_config.get("capture_bits_per_word")
        else:
            value = getattr(receiver_config, "capture_bits_per_word", None)
        if value is not None:
            return int(value)

    raw_capture = getattr(getattr(board, "config", None), "raw_capture", None)
    value = getattr(raw_capture, "capture_bits_per_word", None) if raw_capture is not None else None
    return int(value) if value is not None else 32


def load_funcgen_input(board, stem):
    input_path = INPUT_DIR / f"{stem}.bin"
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    adc_bits = int(board.ADC_BITS_PER_SAMPLE)
    n_inputs = int(board.SAMPLES_PER_WORD)
    signal = InputSignal.from_binary(input_path, adc_bits=adc_bits, n_inputs=n_inputs)
    nsamp = int(board.ADC_SAMPLES_PER_FRAME)
    if signal.data.size < nsamp:
        raise RuntimeError(f"{input_path} has {signal.data.size} samples, expected at least {nsamp}")
    return input_path, signal.data.T.ravel()[:nsamp].astype(np.int64, copy=False)


def setup_capture(board, channels):
    board.stop_data_capture()
    board.reset_scaler_overflow_flags(channels=channels)
    board.set_channelizer(
        fft_bypass=False,
        scaler_bypass=False,
        data_source=None,
        scaler_bypass_data_type=0,
        scaler_cap_data_type=CAP_DATA_TYPE,
        offset_binary_encoding=False,
        scaler_eight_bit=True,
        prober_user_flags=False,
        scaler_rounding_mode=0,
        symmetric_saturation=False,
        channels=channels,
    )
    board.set_scaler_output_modes(cap_data_type=CAP_DATA_TYPE, channels=channels)
    for channel in channels:
        board.chan[channel].FFT.FFT_SHIFT = FFT_SHIFT
        board.chan[channel].FFT.reset_fft_overflow_count()


async def capture_one_frame(ca, board, funcgen_input, channels):
    board.stop_data_capture()
    board.reset_scaler_overflow_flags(channels=channels)
    for channel in channels:
        board.chan[channel].FFT.reset_fft_overflow_count()

    board.sync()
    board.set_funcgen(
        function="arb",
        channels=channels,
        left_shift=0,
        right_shift=0,
        reset_chan=True,
        data=funcgen_input,
    )

    fft_info = board.FFT_INFO[int(board.FFT_TYPE)]
    fft_bins_per_word = int(fft_info.get("bins_per_word") or 1)
    frames_per_burst = 2 * fft_bins_per_word

    board.start_data_capture(
        period=CAPTURE_PERIOD,
        frames_per_burst=frames_per_burst,
        channels=channels,
        source="scaler",
        data_type=CAP_DATA_TYPE,
    )
    board.sync()

    receiver = await ca.get_data_receiver_async(verbose=1)
    receiver.flush()
    ts, data, counts = receiver.read_raw_frames(ncap=NCAP, source=1, chan_first=False)
    return receiver, data[-1, 0, :], int(ts[-1]), counts[-1].tolist(), frames_per_burst


def downshift_capture(board, receiver, captured):
    capture_bits_per_word = get_capture_bits_per_word(board, receiver)
    capture_right_shift = capture_bits_per_word - int(FFT_OUTPUT_BITS)
    if capture_right_shift < 0:
        raise RuntimeError(
            f"FFT_OUTPUT_BITS={FFT_OUTPUT_BITS} is wider than capture word width {capture_bits_per_word}"
        )

    raw_real = integer_parts(captured.real, "real")
    raw_imag = integer_parts(captured.imag, "imag")

    low_bit_mask = (1 << capture_right_shift) - 1 if capture_right_shift else 0
    real_low_nonzero = int(np.count_nonzero(raw_real & low_bit_mask)) if low_bit_mask else 0
    imag_low_nonzero = int(np.count_nonzero(raw_imag & low_bit_mask)) if low_bit_mask else 0

    real = raw_real >> capture_right_shift
    imag = raw_imag >> capture_right_shift

    min_value = -(1 << (int(FFT_OUTPUT_BITS) - 1))
    max_value = (1 << (int(FFT_OUTPUT_BITS) - 1)) - 1
    if np.any((real < min_value) | (real > max_value)):
        raise RuntimeError(f"Downshifted real data does not fit signed {FFT_OUTPUT_BITS}-bit range")
    if np.any((imag < min_value) | (imag > max_value)):
        raise RuntimeError(f"Downshifted imag data does not fit signed {FFT_OUTPUT_BITS}-bit range")

    return (
        real.astype(np.int32, copy=False),
        imag.astype(np.int32, copy=False),
        capture_bits_per_word,
        capture_right_shift,
        real_low_nonzero,
        imag_low_nonzero,
    )


async def main():
    if int(FFT_OUTPUT_BITS) <= 0:
        raise RuntimeError(f"FFT_OUTPUT_BITS must be positive, got {FFT_OUTPUT_BITS}")

    connection_config = load_yaml_config(f"{CONFIG}:connection_config")
    bs_label = bitstream_label(connection_config)
    ca = await FPGAArray(**connection_config)
    board = ca.ib[0]
    channels = [CHANNEL]

    n_inputs = int(board.SAMPLES_PER_WORD)
    adc_bits = int(board.ADC_BITS_PER_SAMPLE)
    nsamp = int(board.ADC_SAMPLES_PER_FRAME)
    if n_inputs != 4:
        raise RuntimeError(f"This script expects 4 parallel HIRAX inputs, got {n_inputs}")
    if adc_bits != 8:
        raise RuntimeError(f"This script expects 8-bit ADC input, got {adc_bits}")
    if nsamp != 2048:
        raise RuntimeError(f"This script expects a 2048-sample HIRAX frame, got {nsamp}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    setup_capture(board, channels)
    fft_info = board.FFT_INFO[int(board.FFT_TYPE)]

    print("\nFull-resolution FFT frame capture")
    print("=================================")
    print(f"config:        {CONFIG}")
    print(f"bitstream:     {bs_label}")
    print(f"input dir:     {INPUT_DIR}")
    print(f"output dir:    {OUTPUT_DIR}")
    print(f"channel:       {CHANNEL}")
    print(f"FFT_TYPE:      {int(board.FFT_TYPE)} ({fft_info.get('name', 'unknown')})")
    print(f"FFT info:      {fft_info}")
    print(f"FFT_SHIFT:     {FFT_SHIFT}")
    print(f"output bits:   {FFT_OUTPUT_BITS}")
    print(f"ADC geometry:  bits={adc_bits}, lanes={n_inputs}, frame_samples={nsamp}")

    for stem in INPUT_STEMS:
        input_path, funcgen_input = load_funcgen_input(board, stem)
        receiver, captured, timestamp, count, frames_per_burst = await capture_one_frame(ca, board, funcgen_input, channels)
        (
            real,
            imag,
            capture_bits_per_word,
            capture_right_shift,
            real_low_nonzero,
            imag_low_nonzero,
        ) = downshift_capture(board, receiver, captured)
        overflow_count = int(board.chan[CHANNEL].FFT.get_fft_overflow_count())

        if real.size != int(board.NUMBER_OF_FREQUENCY_BINS) or imag.size != int(board.NUMBER_OF_FREQUENCY_BINS):
            raise RuntimeError(
                f"Captured {stem} has real/imag sizes {real.size}/{imag.size}, "
                f"expected {int(board.NUMBER_OF_FREQUENCY_BINS)}"
            )

        output_path = OUTPUT_DIR / (
            f"{stem}__{bs_label}__fftshift_{int(FFT_SHIFT)}__{int(FFT_OUTPUT_BITS)}bit__ch{int(CHANNEL)}.npz"
        )
        np.savez_compressed(output_path, real=real, imag=imag)

        print(f"\n{stem}")
        print("-" * len(stem))
        print(f"input file:              {input_path}")
        print(f"funcgen min/max:         {int(funcgen_input.min())}/{int(funcgen_input.max())}")
        print(f"captured bins:           {captured.size}")
        print(f"capture word bits:       {capture_bits_per_word}")
        print(f"capture right shift:     {capture_right_shift}")
        print(f"frames_per_burst:        {frames_per_burst}")
        print(f"capture count/timestamp: {count}, {timestamp}")
        print(f"overflow_count:          {overflow_count}")
        print(f"real min/max:            {int(real.min())}/{int(real.max())}")
        print(f"imag min/max:            {int(imag.min())}/{int(imag.max())}")
        print(
            "discarded low bits:      "
            f"real={real_low_nonzero}/{real.size}, imag={imag_low_nonzero}/{imag.size}"
        )
        print(f"saved:                   {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
