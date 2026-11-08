------------------
BER test dashboard
------------------

A PyQt6 application for bit-error-rate tests of a CRS crate: the backplane links (pages 1-3) and the QSFP
(100G) port of every board through a loopback plug (page 4).

.. code-block:: bash

    pip install -e ".[ber_dashboard]"          # PyQt6, matplotlib
    python -m pychfpga.apps.ber_dashboard

Plots, saved crate lists and results go to ``~/ber_dashboard`` (set ``BER_DASHBOARD_DIR`` to change it).

QSFP loopback tests
===================

Each board sends PRBS-31 on its 4 QSFP TX lanes and checks its own RX lanes through a loopback plug (passive copper,
or an optical module with a loopback fibre). All boards are measured at the same time, so a test takes the same
time for 1 or 16 boards.

1. **Boot**: press *Discover boards* (the boards broadcast their serial, crate and slot; the slot fields of the selected
   crate are filled, boards in U-Boot only with *Include boards in U-Boot*), or enter the serial numbers in their
   slot fields. Select *QSFP loopback (shuffle8)*, press *Boot Boards*. Any subset of slots can be used. The bitstream
   must have the CGE TX drive control bytes.
2. **4. QSFP loopback** page:

   - *Read plugs*: what is in each cage (vendor, part, copper or optical).
   - *Check loopback*: TX lane n must reach RX lane n. Lanes without lock at the test setting are "not judged".
   - *Measure*: errors and BER of every lane at the TX setting (swing / pre / post codes) for *Dwell* seconds.
   - *Run sweep*: every swing x pre x post combination; the *Lanes* tab then shows, per lane, how many settings
     have 0 errors and the best setting, and the *Sweep* tab shows the BER maps.
   - *Eye scans*: eye diagram of every lane at the TX setting.
   - *Restore defaults*: FPGA TX 950 mV / 0.01 dB / 3.99 dB (the firmware reset value), DFE.

*RX equalizer* "Auto" uses DFE behind a copper plug and LPM behind an optical module: on CRS rev5, passive copper
loopbacks fail in LPM and SR4 optical modules work best in LPM. Every action saves its data (JSON) and figures
(PNG) to ``~/ber_dashboard/qsfp/``.

Notes:

- After every TX change the receivers are retrained for 2 s before the error count, and a lock proof (the transmitted
  pattern is stopped for 0.3 s) separates "0 errors" from "no lock".
- A loopback tests the TX and RX of the same board together. To tell a TX problem from an RX problem, test the
  board against a reference board.
