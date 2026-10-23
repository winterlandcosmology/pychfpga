import time
import logging
import sys
import pickle

from pychfpga.fpga_array import FPGAArray

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.colors import LogNorm
from matplotlib.ticker import LogLocator, LogFormatterSciNotation

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QLabel, QSpinBox, QGridLayout
)
from PyQt6.QtCore import QThread, pyqtSignal

logging.getLogger().setLevel(logging.INFO)

def emit_log(logger, msg):
    if logger:
        logger(msg)

#---
PRBS = 5         
BIT_RATE = 25e9   # 25 Gb/s per lane
TEST_DWELL = 0.5
DWELL = 5      # seconds; can increase if marginal links
TX_POW = 0      # your TXDIFFCTRL
TX_PRE = 10       # your TXPRECURSOR
TX_POST = 0       # your TXPOSTCURSOR
#---

#---
sweep_PRE = range(0, 21, 1)
sweep_POST = range(0, 21, 1)
sweep_DWELL = 0.5

CRATE = 'rev1_008'
BACKPLANES = [1, 2, 3, 4]
ONLY_QSFPs = True
#---


#---
# hwm = 'crs 69:1 70:2 98:3 65:4 67:5 106:6 84:7 99:8'#  87:9 113:10 114:11 116:12 112:13 115:14 111:15 103:16' # all backplanes
# ca = FPGAArray(hwm, mode='corr64', sync_method='local', sync_source='bp_trig', sync_master=1, sync_master_source='irigb_gen', sync_master_output=1, stdout_log_level='warn')
#---

#-- FUNCTIONS --#

def get_temps(ca):
    return [i.SYSMON.temperature() for i in ca.ib]

def reset_and_setup_links(ca, PRBS, TX_POW, TX_PRE, TX_POST):
    # program PRBS + TX/RX defaults, inhibit all TX, no loopback
    for ib in ca.ib:
        for ch in ib.CT.BPLINKS.gty:
            ch.TXPRBSSEL   = PRBS
            ch.RXPRBSSEL   = PRBS
            ch.TXDIFFCTRL  = TX_POW
            ch.TXPRECURSOR = TX_PRE
            ch.TXPOSTCURSOR= TX_POST
            ch.TXPRBSFORCEERR = 0
            ch.RXLPMEN = 0
            ch.TXINHIBIT = 1
            ch.LOOPBACK = 0

def retrain_and_clear(ca, retrain_delay=0.2, settle_time=0.5):
    for ib in ca.ib:
        for ch in ib.CT.BPLINKS.gty:
            ch.reset_rx_equalizer()
    time.sleep(retrain_delay)
    for ib in ca.ib:
        for ch in ib.CT.BPLINKS.gty:
            ch.RXPRBSCNTRESET = 1
            ch.RXPRBSCNTRESET = 0
    time.sleep(settle_time)

def probe_tx_lane(ca, tx_to_rx_lane_map:dict, tx_slot:int, tx_lane:int, dwell:float=DWELL, retrain_delay:float=0.2, settle_time:float=0.5,
                PRBS=PRBS, TX_POW=TX_POW, TX_PRE=TX_PRE, TX_POST=TX_POST,
                verbose=True, log=None, attempts=3): 
    """
    Enable TX on exactly one (slot, lane) and scan all receivers.
    Returns list of matches as (rx_slot, rx_lane, err_count, ber).
    """
    reset_and_setup_links(ca, PRBS, TX_POW, TX_PRE, TX_POST)

    # enable only the chosen TX
    for ib in ca.ib:
        for ch in ib.CT.BPLINKS.gty:
            ch.TXINHIBIT = 1
    tx_ib = [ib for ib in ca.ib if ib.slot == tx_slot][0]
    tx_ch = tx_ib.CT.BPLINKS.gty[tx_lane]
    tx_ch.TXINHIBIT = 0

    # retrain + clear
    retrain_and_clear(ca, retrain_delay=retrain_delay, settle_time=settle_time)
    time.sleep(dwell)

    matches = []
    if verbose:
        print(f"\nProbing TX -> Slot {tx_slot} GTY[{tx_lane}] ...")

    for attempt in range(attempts):
        for ib in ca.ib:
            # log.emit(f"ib: {ib.slot} ...")
            # looping if ERR>0
            cherrs = {}
            for ch in ib.CT.BPLINKS.gty:
                # Read error counter once; “0” means PRBS is being recovered cleanly
                err = ch.ERR_CTR or 0
                ber = (err or 1) / (BIT_RATE * dwell) if dwell > 0 else float('inf')
                cherrs[ch.instance_number] = err
                if ch.ERR_CTR == 0: 
                    matches.append((ib.slot, ch.instance_number, err, ber))
                    if verbose:
                        print(f"RX located at: Slot {ib.slot} GTY[{ch.instance_number}]  (ERR={err}, BER≤{ber:.2e})")
                    tx_to_rx_lane_map[(tx_slot, tx_lane)] = (ib.slot, ch.instance_number)
            # if matches:
            #     if log:
            #         log.emit(f"Found zero-error link: TX {tx_slot}:{tx_lane} → RX {matches[0][0]}:{matches[0][1]} (ERR={matches[0][2]}, BER≤{matches[0][3]:.2e})")
            #     break
            else:
                if log and verbose:
                    log.emit(f"Attempt {attempt+1}/{attempts} for TX Slot {tx_slot} Lane {tx_lane}: No zero-error RX found. Retrying...")
                time.sleep(0.1)  # wait before retrying
                                
        mean, std = np.mean(list(cherrs.values())), np.std(list(cherrs.values()))
        neg3std = mean - 3*std
        lowerrs = {ch: err for ch, err in cherrs.items() if err <= neg3std}
        if len(lowerrs) > 1 and log and verbose:
            log.emit(f"  Note: Multiple RX with low errors (≤ {neg3std:.0f}). Check cables or increase DWELL for better discrimination.")
            match = min(lowerrs.items(), key=lambda x: x[1])  # pick lowest error among them
            matches.append((ib.slot, match[0], match[1], match[1] / (BIT_RATE * dwell))) # adding minimum error match as well, but with a note of caution
        elif len(lowerrs) == 1:
            matches.append((ib.slot, list(lowerrs.keys())[0], lowerrs[list(lowerrs.keys())[0]], lowerrs[list(lowerrs.keys())[0]] / (BIT_RATE * dwell)))
            if log and verbose:
                log.emit(f"  Note: Single RX with significantly lower errors ({lowerrs[list(lowerrs.keys())[0]]} ≤ {neg3std:.0f}). ")
        elif log and verbose:
            log.emit(f"  No RX with significantly low errors (≤ {neg3std:.0f}). Check cables or increase DWELL.")

    if not matches and verbose and log:
        log.emit(" No zero-error RX found. Check cable or increase DWELL.")
    return matches


def sweep_ber_heatmap(ca, tx_slot, tx_lane, rx_slot, rx_lane,
                     pre_range=range(0, 13),
                     post_range=range(0, 13),
                     dwell=1.0,
                     prbs=5,
                     bit_rate=25e9,
                     tx_pow=10,
                     plot=False,
                     log=None,
                     progress=None):
   """
   Sweep TX pre/post cursor values and plot BER heatmap.
   Arguments:
     tx_slot, tx_lane  - transmitter slot/lane
     rx_slot, rx_lane  - receiver slot/lane
   """


   # --- Helper functions ---
   def setup_links(pre, post):
       for ib in ca.ib:
           for ch in ib.CT.BPLINKS.gty:
               ch.TXPRBSSEL   = prbs
               ch.RXPRBSSEL   = prbs
               ch.TXDIFFCTRL  = tx_pow
               ch.TXPRECURSOR = pre
               ch.TXPOSTCURSOR= post
               ch.TXPRBSFORCEERR = 0
               ch.RXLPMEN = 0
               ch.TXINHIBIT = 1
               ch.LOOPBACK = 0


   def retrain_and_clear():
       for ib in ca.ib:
           for ch in ib.CT.BPLINKS.gty:
               ch.reset_rx_equalizer()
       time.sleep(0.1)
       for ib in ca.ib:
           for ch in ib.CT.BPLINKS.gty:
               ch.RXPRBSCNTRESET = 1
               ch.RXPRBSCNTRESET = 0


   def measure(pre, post):
       setup_links(pre, post)
      
       # enable chosen TX
       for ib in ca.ib:
           for ch in ib.CT.BPLINKS.gty:
               ch.TXINHIBIT = 1
       tx_ib = [ib for ib in ca.ib if ib.slot == tx_slot][0]
       tx_ch = tx_ib.CT.BPLINKS.gty[tx_lane]
       tx_ch.TXINHIBIT = 0


       retrain_and_clear()
       time.sleep(dwell)


       # get RX BER
       rx_ib = [ib for ib in ca.ib if ib.slot == rx_slot][0]
       rx_ch = rx_ib.CT.BPLINKS.gty[rx_lane]
       err = rx_ch.ERR_CTR or 0
       ber = (err or 1) / (bit_rate * dwell)
       return ber, err


   # --- Sweep ---
   ber_matrix = np.zeros((len(pre_range), len(post_range)))
   zero_err_points = []
   total_points = len(pre_range) * len(post_range)
   done_points = 0
   emit_log(log, f"[START] TX({tx_slot},{tx_lane}) → RX({rx_slot},{rx_lane})")
   for i, pre in enumerate(pre_range):
       for j, post in enumerate(post_range):
            ber, err = measure(pre, post)
            ber_matrix[i, j] = ber
            if err == 0:
               zero_err_points.append((post, pre))  

            done_points += 1
            if progress:
                progress(done_points, total_points)
   emit_log(log, f"[DONE]  TX({tx_slot},{tx_lane}) → RX({rx_slot},{rx_lane})")
   emit_log(log, "-" * 40)

  # --- Plot (log scale, pinned floor) ---
   if plot:
       # ticks/extent
       n_pre  = len(pre_range)
       n_post = len(post_range)
       xmin = min(post_range) - 0.5
       xmax = max(post_range) + 0.5 if n_post > 1 else min(post_range) + 0.5
       ymin = min(pre_range)  - 0.5
       ymax = max(pre_range)  + 0.5 if n_pre  > 1 else min(pre_range)  + 0.5


       # BER floor = measurement limit when 0 errors observed
       ber_floor = 1.0 / (bit_rate * dwell)
       Z = np.clip(ber_matrix, ber_floor, None)   # avoid log(0)
       vmax = max(Z.max(), ber_floor * 10)        # keep vmax > vmin even if all zeros


       plt.figure(figsize=(8,6))
       img = plt.imshow(
           Z,
           origin="lower",
           extent=[xmin, xmax, ymin, ymax],
           aspect="auto",
           cmap="viridis",
           norm=LogNorm(vmin=ber_floor, vmax=vmax),
       )


       cbar = plt.colorbar(img)
       cbar.set_label("Bit Error Rate (BER)", rotation=270, labelpad=15)
       cbar.locator   = LogLocator(base=10)
       cbar.formatter = LogFormatterSciNotation(base=10)
       cbar.update_ticks()


       plt.xlabel("TX_POSTCURSOR")
       plt.ylabel("TX_PRECURSOR")
       plt.title(f"TX({tx_slot},{tx_lane}) → RX({rx_slot},{rx_lane})   dwell={dwell}s")


       # ticks at actual PRE/POST values
       plt.xticks(list(post_range))
       plt.yticks(list(pre_range))


       # mark zero-error points
       if zero_err_points:
           posts, pres = zip(*zero_err_points)
           plt.scatter(posts, pres, s=80, facecolors='none', edgecolors='purple',
                       linewidths=2)


       plt.tight_layout()
       plt.show()


   return ber_matrix

def run_link(ca, tx_slot, tx_lane, rx_slot, rx_lane, pre_range=sweep_PRE, post_range=sweep_POST, dwell=sweep_DWELL):
    ber = sweep_ber_heatmap(
        ca,
        tx_slot=tx_slot, tx_lane=tx_lane,
        rx_slot=rx_slot, rx_lane=rx_lane,
        pre_range=pre_range,
        post_range=post_range,
        dwell=dwell
    )
    np.save(f"ber_s{tx_slot}l{tx_lane}_to_s{rx_slot}l{rx_lane}.npy", ber)
    return ber