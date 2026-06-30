from pathlib import Path
import os
import re
import sys

os.environ.setdefault("MPLCONFIGDIR", "/tmp/pychfpga_mpl")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
FIRMWARE_TESTS_DIR = HERE.parents[2]
REPO_ROOT = FIRMWARE_TESTS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from firmware_tests.casper_simulations.caspersimtools import InputSignal, OutputSignal


DATA_DIR = HERE
CAPTURE_DIR = DATA_DIR / "captures"
FULL_BITGROWTH_DIR = DATA_DIR / "full_bitgrowth"
PLOT_DIR = CAPTURE_DIR / "full_bitgrowth_comparisons"

INPUT_STEMS = ("ramp", "two_tone", "noise")
SHIFT_TO_CAPTURE_LEFT_SHIFT = {
    1984: 5,
    # Old 18-bit captures need one fixed binary-point bit in addition to the
    # programmed FFT stage shifts to match the 29-bit full-bitgrowth reference.
    2047: 12,
    1367: 8,
}
SHIFT_TO_CAPTURE_COLOR = {
    1984: "tab:green",
    2047: "tab:red",
    1367: "tab:blue",
}
ADC_BITS = 8
ADC_INPUTS = 4
INPUT_FRAME_SAMPLES = 2048
INSTRUMENT = "hirax"
SIM_DTYPE = np.int32
FREQ_MAX_MHZ = 400.0

CAPTURE_NAME_RE = re.compile(
    r"^(?P<stem>.+?)__(?P<bitstream>.+)__fftshift_(?P<fft_shift>\d+)__"
    r"(?P<fft_bits>\d+)bit__ch(?P<channel>\d+)\.npz$"
)


def log2_abs(values):
    values = np.asarray(values)
    abs_values = np.abs(values).astype(np.float64)
    out = np.full(abs_values.shape, np.nan, dtype=np.float64)
    nonzero = abs_values > 0
    out[nonzero] = np.log2(abs_values[nonzero])
    return out


def log2_scalar(value):
    value = abs(float(value))
    if value == 0:
        return float("-inf")
    return float(np.log2(value))


def format_log2(value):
    if np.isneginf(value):
        return "-inf"
    return f"{value:.6g}"


def nanmean(values):
    finite = np.isfinite(values)
    if not np.any(finite):
        return float("nan")
    return float(np.mean(values[finite]))


def parse_capture_path(path):
    match = CAPTURE_NAME_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(f"Capture filename does not match expected convention: {path.name}")
    info = match.groupdict()
    return {
        "path": path,
        "stem": info["stem"],
        "bitstream": info["bitstream"],
        "fft_shift": int(info["fft_shift"]),
        "fft_bits": int(info["fft_bits"]),
        "channel": int(info["channel"]),
    }


def find_capture(stem, fft_shift):
    matches = []
    for path in sorted(CAPTURE_DIR.glob("*.npz")):
        info = parse_capture_path(path)
        if info["stem"] == stem and info["fft_shift"] == fft_shift:
            matches.append(info)
    if len(matches) != 1:
        names = [str(item["path"]) for item in matches]
        raise RuntimeError(f"Expected one capture for stem={stem!r}, fft_shift={fft_shift}; found {len(matches)}: {names}")
    return matches[0]


def load_input_frame(stem):
    path = DATA_DIR / f"{stem}.bin"
    if not path.exists():
        raise FileNotFoundError(path)
    signal = InputSignal.from_binary(path, adc_bits=ADC_BITS, n_inputs=ADC_INPUTS)
    if signal.data.size < INPUT_FRAME_SAMPLES:
        raise RuntimeError(f"{path} has {signal.data.size} samples, expected at least {INPUT_FRAME_SAMPLES}")
    return path, signal.data.T.ravel()[:INPUT_FRAME_SAMPLES].astype(np.int64, copy=False)


def load_full_bitgrowth_frame(stem):
    path = FULL_BITGROWTH_DIR / f"{stem}_out_29.bin"
    if not path.exists():
        raise FileNotFoundError(path)
    signal = OutputSignal.from_file(path, instrument=INSTRUMENT, dtype=SIM_DTYPE)
    sync_idx = np.flatnonzero(signal.sync_out == 1)
    if sync_idx.size != 1:
        raise RuntimeError(f"Expected exactly one sync_out pulse in {path}, got {sync_idx.size}")
    frame = signal.get_sync_frame()
    real, imag = frame.get_real_imag(descramble=True)
    return path, int(sync_idx[0]), real.astype(np.int64, copy=False), imag.astype(np.int64, copy=False)


def load_capture(info, left_shift):
    with np.load(info["path"]) as data:
        keys = set(data.files)
        if keys != {"real", "imag"}:
            raise RuntimeError(f"{info['path']} must contain only real and imag arrays; got {sorted(keys)}")
        real = data["real"].astype(np.int64, copy=False)
        imag = data["imag"].astype(np.int64, copy=False)
    real[real == 0] = 1
    imag[imag == 0] = 1
    return real << left_shift, imag << left_shift


def plot_comparison(stem, input_frame, sim_real, sim_imag, capture_info, cap_real, cap_imag, left_shift):
    if sim_real.size != cap_real.size or sim_imag.size != cap_imag.size:
        raise RuntimeError(
            f"{capture_info['path']} bin count mismatch: "
            f"sim real/imag={sim_real.size}/{sim_imag.size}, "
            f"capture real/imag={cap_real.size}/{cap_imag.size}"
        )
    if sim_real.size != sim_imag.size:
        raise RuntimeError(f"Simulation real/imag bin count mismatch: {sim_real.size}/{sim_imag.size}")

    real_residual = cap_real - sim_real
    imag_residual = cap_imag - sim_imag
    real_residual_log2 = log2_abs(real_residual)
    imag_residual_log2 = log2_abs(imag_residual)
    real_residual_avg = nanmean(real_residual_log2)
    imag_residual_avg = nanmean(imag_residual_log2)
    real_max_abs = int(np.max(np.abs(real_residual)))
    imag_max_abs = int(np.max(np.abs(imag_residual)))
    capture_color = SHIFT_TO_CAPTURE_COLOR[capture_info["fft_shift"]]

    freq_mhz = np.arange(sim_real.size, dtype=np.float64) * FREQ_MAX_MHZ / sim_real.size

    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=False)
    axes[0].plot(np.arange(input_frame.size), input_frame, lw=1, color="black")
    axes[0].set_title(f"{stem} input ADC frame")
    axes[0].set_xlabel("Sample")
    axes[0].set_ylabel("ADC counts")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(freq_mhz, log2_abs(sim_real), lw=1, color="black", label="simulation real")
    axes[1].plot(
        freq_mhz,
        log2_abs(cap_real),
        lw=1,
        color=capture_color,
        label=f"captured real << {left_shift}",
    )
    axes[1].set_title("Full-bitgrowth simulation vs captured real FFT MSBs")
    axes[1].set_ylabel("log2(abs(FFT integer))")
    axes[1].set_xlim(0, FREQ_MAX_MHZ)
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(freq_mhz, log2_abs(sim_imag), lw=1, color="black", label="simulation imag")
    axes[2].plot(
        freq_mhz,
        log2_abs(cap_imag),
        lw=1,
        color=capture_color,
        label=f"captured imag << {left_shift}",
    )
    axes[2].set_title("Full-bitgrowth simulation vs captured imaginary FFT MSBs")
    axes[2].set_ylabel("log2(abs(FFT integer))")
    axes[2].set_xlim(0, FREQ_MAX_MHZ)
    axes[2].legend()
    axes[2].grid(True, alpha=0.3)

    axes[3].plot(
        freq_mhz,
        real_residual_log2,
        lw=1,
        color=capture_color,
        alpha=0.5,
        label="real residual",
    )
    axes[3].plot(
        freq_mhz,
        imag_residual_log2,
        lw=1,
        color=capture_color,
        alpha=0.5,
        ls="-",
        label="imag residual",
    )
    axes[3].axhline(
        real_residual_avg,
        lw=3,
        color="black",
        label=f"real avg={real_residual_avg:.6g}",
    )
    axes[3].axhline(
        imag_residual_avg,
        lw=3,
        color="black",
        ls="--",
        label=f"imag avg={imag_residual_avg:.6g}",
    )
    axes[3].set_title(
        "Residual MSB: "
        f"real max_abs={real_max_abs} (log2={format_log2(log2_scalar(real_max_abs))}), "
        f"imag max_abs={imag_max_abs} (log2={format_log2(log2_scalar(imag_max_abs))})"
    )
    axes[3].set_xlabel("Frequency [MHz]")
    axes[3].set_ylabel("log2(abs(captured - simulation))")
    axes[3].set_xlim(0, FREQ_MAX_MHZ)
    axes[3].legend(ncol=2)
    axes[3].grid(True, alpha=0.3)

    fig.suptitle(
        f"{stem}: {capture_info['bitstream']}, FFT_SHIFT={capture_info['fft_shift']}, "
        f"{capture_info['fft_bits']}bit, ch={capture_info['channel']}"
    )
    fig.tight_layout()

    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PLOT_DIR / f"{capture_info['path'].stem}__vs_full_bitgrowth.png"
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path, real_residual_avg, imag_residual_avg, real_max_abs, imag_max_abs


def main():
    print("\nFull-resolution capture vs full-bitgrowth simulation")
    print("====================================================")
    print(f"data dir:     {DATA_DIR}")
    print(f"capture dir:  {CAPTURE_DIR}")
    print(f"plot dir:     {PLOT_DIR}")

    written = []
    for stem in INPUT_STEMS:
        input_path, input_frame = load_input_frame(stem)
        sim_path, sync_idx, sim_real, sim_imag = load_full_bitgrowth_frame(stem)
        print(f"\n{stem}")
        print("-" * len(stem))
        print(f"input:      {input_path}")
        print(f"simulation: {sim_path} (sync index {sync_idx}, bins {sim_real.size})")

        for fft_shift, left_shift in SHIFT_TO_CAPTURE_LEFT_SHIFT.items():
            capture_info = find_capture(stem, fft_shift)
            cap_real, cap_imag = load_capture(capture_info, left_shift)
            output_path, real_avg, imag_avg, real_max_abs, imag_max_abs = plot_comparison(
                stem,
                input_frame,
                sim_real,
                sim_imag,
                capture_info,
                cap_real,
                cap_imag,
                left_shift,
            )
            written.append(output_path)
            print(
                f"FFT_SHIFT={fft_shift:4d}, left_shift={left_shift:2d}, "
                f"capture={capture_info['path'].name}, "
                f"real_avg_log2_residual={real_avg:.6g}, imag_avg_log2_residual={imag_avg:.6g}, "
                f"real_max_abs={real_max_abs}, imag_max_abs={imag_max_abs}, "
                f"plot={output_path.name}"
            )

    print(f"\nSaved {len(written)} plots:")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()
