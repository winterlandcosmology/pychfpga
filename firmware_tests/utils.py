import matplotlib.pyplot as plt
from cycler import cycler
import numpy as np
from pathlib import Path
from textwrap import wrap

def plot(datasets, split_complex=False, title="test"):
    styles = cycler(color=['tab:blue', 'orange', 'forestgreen'], marker=['.', ' ', ' '])
    plt.rc('axes', prop_cycle=styles)

    labels = ["Returned", "Reference"]

    l = len(labels)
    for i in range(len(datasets) - l):
        labels.append(f"Dataset {i}")

    # Check all datasets are the same length
    lengths = [d.size for d in datasets]
    if len(set(lengths)) != 1:
        raise ValueError("All datasets must be the same length if data_range is not specified")

    fig, axs = plt.subplots(2 if split_complex else 1)
    for i in range(2*len(datasets)):
        im = i >= len(datasets)
        ax = axs[int(im)] if split_complex else axs
        if im and not split_complex:
            break
        if split_complex and not im:
            ax.plot(datasets[i][::2], label=labels[i] + "(Re)")
        elif split_complex and im:
            ax.plot(datasets[i % len(datasets)][1::2], label=labels[i % len(datasets)] + "(Im)")
        else:
            ax.plot(datasets[i], label=labels[i])
        ax.legend()

    fig.supxlabel("Bin")
    fig.supylabel("Output")
    fig.suptitle("\n".join(wrap(title, 60)))
