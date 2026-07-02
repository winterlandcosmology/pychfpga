import matplotlib.pyplot as plt
from cycler import cycler
import asyncio
import numpy as np
from pathlib import Path
import socket
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
            d = datasets[i]
            y = np.asarray(d.real, dtype=float) if np.iscomplexobj(d) else d[::2]
            ax.plot(y, label=labels[i] + "(Re)")
        elif split_complex and im:
            d = datasets[i % len(datasets)]
            y = np.asarray(d.imag, dtype=float) if np.iscomplexobj(d) else d[1::2]
            ax.plot(y, label=labels[i % len(datasets)] + "(Im)")
        else:
            ax.plot(datasets[i], label=labels[i])
        ax.legend()

    fig.supxlabel("Bin")
    fig.supylabel("Output")
    fig.suptitle("\n".join(wrap(title, 60)))


async def ping_async(addr, timeout=0.3):
    """
    Establish a TCP connection with `addr` and return the interface and local port used for the connection.

    Parameters:
        addr ((str, int) tuple): Address and port to which a TCP connection is made
        timeout (float): Time to wait before giving up on the connection

    Return:
        An (interface_address, local_port) if the connection is successful, None otherwise.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)

    loop = asyncio.get_event_loop()
    try:
        await loop.sock_connect(s, addr)
        if_addr = s.getsockname()
        s.close()
    except (socket.timeout, Exception):
        if_addr = None

    return if_addr


async def ping_sources_async(port_map):
    src_addrs = [tuple(src) for port_info in port_map
                 for src in port_info['sources']]
    src_if_addrs = await asyncio.gather(*[ping_async(src) for src in src_addrs])
    return dict(zip(src_addrs, src_if_addrs))
