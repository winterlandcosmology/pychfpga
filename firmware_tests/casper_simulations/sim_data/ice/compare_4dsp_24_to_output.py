from pathlib import Path
import os
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
FOUR_DSP_DIR = DATA_DIR / "4dsp_24"
PLOT_DIR = DATA_DIR / "4dsp_24_comparisons"

INPUT_STEMS = ("ramp", "two_tone", "noise")
ADC_BITS = 8
ADC_INPUTS = 4
INPUT_FRAME_SAMPLES = 2048
INSTRUMENT = "hirax"
SIM_DTYPE = np.int32
FREQ_MAX_MHZ = 400.0
PLOT_LINEWIDTH = 2
AVERAGE_LINEWIDTH = 3
LABEL_FONTSIZE = 16
LEGEND_FONTSIZE = 15
TICK_FONTSIZE = 14


def log2_abs(values):
    values = np.asarray(values)
    abs_values = np.abs(values).astype(np.float64)
    out = np.full(abs_values.shape, np.nan, dtype=np.float64)
    nonzero = abs_values > 0
    out[nonzero] = np.log2(abs_values[nonzero])
    return out


def nanmean(values):
    finite = np.isfinite(values)
    if not np.any(finite):
        return float("nan")
    return float(np.mean(values[finite]))


def load_input_frame(stem):
    path = DATA_DIR / f"{stem}.bin"
    if not path.exists():
        raise FileNotFoundError(path)
    signal = InputSignal.from_binary(path, adc_bits=ADC_BITS, n_inputs=ADC_INPUTS)
    if signal.data.size < INPUT_FRAME_SAMPLES:
        raise RuntimeError(f"{path} has {signal.data.size} samples, expected at least {INPUT_FRAME_SAMPLES}")
    return path, signal.data.T.ravel()[:INPUT_FRAME_SAMPLES].astype(np.int64, copy=False)


def load_output_frame(path):
    if not path.exists():
        raise FileNotFoundError(path)
    signal = OutputSignal.from_file(path, instrument=INSTRUMENT, dtype=SIM_DTYPE)
    sync_idx = np.flatnonzero(signal.sync_out == 1)
    if sync_idx.size != 1:
        raise RuntimeError(f"Expected exactly one sync_out pulse in {path}, got {sync_idx.size}")
    frame = signal.get_sync_frame()
    real, imag = frame.get_real_imag(descramble=True)
    real[real == 0] = 1
    imag[imag == 0] = 1
    return int(sync_idx[0]), real.astype(np.int64, copy=False), imag.astype(np.int64, copy=False)


def plot_comparison(stem, input_frame, four_real, four_imag, usual_real, usual_imag):
    if four_real.size != usual_real.size or four_imag.size != usual_imag.size:
        raise RuntimeError(
            f"{stem} bin count mismatch: "
            f"4dsp real/imag={four_real.size}/{four_imag.size}, "
            f"usual real/imag={usual_real.size}/{usual_imag.size}"
        )
    if four_real.size != four_imag.size:
        raise RuntimeError(f"4-DSP real/imag bin count mismatch: {four_real.size}/{four_imag.size}")

    real_residual = usual_real - four_real
    imag_residual = usual_imag - four_imag
    real_residual[real_residual == 0] = 1
    imag_residual[imag_residual == 0] = 1
    real_residual_log2 = log2_abs(real_residual)
    imag_residual_log2 = log2_abs(imag_residual)
    real_residual_avg = nanmean(real_residual_log2)
    imag_residual_avg = nanmean(imag_residual_log2)
    real_max_abs = int(np.max(np.abs(real_residual)))
    imag_max_abs = int(np.max(np.abs(imag_residual)))

    freq_mhz = np.arange(four_real.size, dtype=np.float64) * FREQ_MAX_MHZ / four_real.size

    fig, axes = plt.subplots(
        4,
        1,
        figsize=(21, 10.5),
        sharex=False,
        gridspec_kw={"height_ratios": [0.5, 1, 1, 1]},
    )
    input_samples = np.arange(input_frame.size)
    axes[0].plot(input_samples, input_frame, lw=1, color="black")
    axes[0].set_xlabel("Sample", fontsize=LABEL_FONTSIZE)
    axes[0].set_ylabel("ADC counts", fontsize=LABEL_FONTSIZE)
    axes[0].set_xlim(input_samples[0], input_samples[-1])
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(freq_mhz, log2_abs(four_real), lw=PLOT_LINEWIDTH, color="black", label="4DSP Re")
    axes[1].plot(freq_mhz, log2_abs(usual_real), lw=PLOT_LINEWIDTH, color="tab:green", label="3DSP Re")
    axes[1].set_ylabel("log2(abs(FFT int))", fontsize=LABEL_FONTSIZE)
    axes[1].set_xlim(0, FREQ_MAX_MHZ)
    axes[1].legend(fontsize=LEGEND_FONTSIZE)
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(freq_mhz, log2_abs(four_imag), lw=PLOT_LINEWIDTH, color="black", label="4DSP Im")
    axes[2].plot(freq_mhz, log2_abs(usual_imag), lw=PLOT_LINEWIDTH, color="tab:green", label="3DSP Im")
    axes[2].set_ylabel("log2(abs(FFT int))", fontsize=LABEL_FONTSIZE)
    axes[2].set_xlim(0, FREQ_MAX_MHZ)
    axes[2].legend(fontsize=LEGEND_FONTSIZE)
    axes[2].grid(True, alpha=0.3)

    axes[3].step(
        freq_mhz,
        real_residual_log2,
        where="mid",
        lw=PLOT_LINEWIDTH,
        color="tab:green",
        alpha=0.5,
        label="Re res",
    )
    axes[3].step(
        freq_mhz,
        imag_residual_log2,
        where="mid",
        lw=PLOT_LINEWIDTH,
        color="tab:green",
        alpha=0.5,
        ls="-",
        label="Im res",
    )
    axes[3].axhline(
        real_residual_avg,
        lw=AVERAGE_LINEWIDTH,
        color="black",
        label=f"Re avg={real_residual_avg:.2f}",
    )
    axes[3].axhline(
        imag_residual_avg,
        lw=AVERAGE_LINEWIDTH,
        color="black",
        ls="--",
        label=f"Im avg={imag_residual_avg:.2f}",
    )
    axes[3].set_xlabel("Frequency [MHz]", fontsize=LABEL_FONTSIZE)
    axes[3].set_ylabel("log2(abs(out - 4DSP))", fontsize=LABEL_FONTSIZE)
    axes[3].set_xlim(0, FREQ_MAX_MHZ)
    axes[3].legend(ncol=2, fontsize=LEGEND_FONTSIZE)
    axes[3].grid(True, alpha=0.3)
    for ax in axes:
        ax.tick_params(axis="both", labelsize=TICK_FONTSIZE)

    fig.tight_layout()

    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PLOT_DIR / f"{stem}__4dsp_24_vs_out.png"
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path, real_residual_avg, imag_residual_avg, real_max_abs, imag_max_abs


def main():
    print("\n4-DSP 24-bit simulation vs default simulation")
    print("=============================================")
    print(f"data dir:  {DATA_DIR}")
    print(f"4dsp dir:  {FOUR_DSP_DIR}")
    print(f"plot dir:  {PLOT_DIR}")

    written = []
    for stem in INPUT_STEMS:
        input_path, input_frame = load_input_frame(stem)
        four_path = FOUR_DSP_DIR / f"{stem}_24_out.bin"
        usual_path = DATA_DIR / f"{stem}_out.bin"
        four_sync_idx, four_real, four_imag = load_output_frame(four_path)
        usual_sync_idx, usual_real, usual_imag = load_output_frame(usual_path)

        print(f"\n{stem}")
        print("-" * len(stem))
        print(f"input:  {input_path}")
        print(f"4dsp:   {four_path} (sync index {four_sync_idx}, bins {four_real.size})")
        print(f"usual:  {usual_path} (sync index {usual_sync_idx}, bins {usual_real.size})")

        output_path, real_avg, imag_avg, real_max_abs, imag_max_abs = plot_comparison(
            stem,
            input_frame,
            four_real,
            four_imag,
            usual_real,
            usual_imag,
        )
        written.append(output_path)
        print(
            f"real_avg_log2_residual={real_avg:.6g}, imag_avg_log2_residual={imag_avg:.6g}, "
            f"real_max_abs={real_max_abs}, imag_max_abs={imag_max_abs}, plot={output_path.name}"
        )

    print(f"\nSaved {len(written)} plots:")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()
