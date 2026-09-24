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
        raise ValueError(f"All datasets must be the same length. Instead, they are the following sizes: {lengths}")

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

def gen_data(func, samples, **kwargs):
    if func == 'a': #constant input
        return np.ones(samples) * kwargs.get('a', 1)
    elif func == 'ab': #alternating input
        period = kwargs.get('period', 1)
        res = np.tile(np.concatenate((np.repeat(kwargs.get('a', 0), period), np.repeat(kwargs.get('b', 1), period))), samples//(2*period))
        return np.append(res, np.repeat(kwargs.get('a', 0), samples - res.size))
    elif func  == 'tile':
        pattern = kwargs.get('pattern', [0, 1])
        res = np.tile(pattern, samples // len(pattern))
        return np.append(res, pattern[:samples - res.size])
    elif func == 'ramp': #single ramp from min to max inclusive
        min = kwargs.get('min', 0)
        max = kwargs.get('max', samples)
        res = np.repeat(np.arange(min, max + 1), samples // (max - min + 1))
        return np.append(res, max * np.ones(samples - res.size))
    elif func == 'periodic_ramp': #periodic ramp from min to max inclusive
        #TODO: fix to ensure bounds are always exactly respected
        min = kwargs.get('min', 0)
        max = kwargs.get('max', samples)
        res = np.tile(np.arange(min, max + 1), samples // (max - min + 1))
        return np.append(res, np.arange(min, min + (samples - res.size)))
    elif func == 'complex_ramp': #complex ramp from min+min*j to max+max*j
        min = kwargs.get('min', 0)
        max = kwargs.get('max', np.sqrt(samples))
        half_samples = samples // 2
        reals = np.repeat(np.arange(min, max + 1), half_samples // (max - min + 1))
        reals = np.append(reals, max * np.ones(half_samples - reals.size))
        cmplx = np.tile(np.arange(min, max + 1), half_samples // (max - min + 1))
        cmplx = np.append(cmplx, np.arange(min, min + half_samples - cmplx.size))
        return np.stack((reals, cmplx), axis=1).reshape(-1) #interleave the real and complex arrays
    elif func == 'arb': #user-provided data buffer
        return kwargs.get('data', np.zeros(samples))
    raise ValueError(f"Unknown data generation function '{func}'")