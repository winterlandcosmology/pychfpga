100G link signal integrity (CRS)
================================

The CGE (100G) link has array methods to set the FPGA TX drive and the FPGA receiver equalizer, and
to read and set the QSFP28 optical module signal-integrity settings. They need a ``crs_ct`` bitstream
with the CGE TX drive control bytes; on older bitstreams the FPGA methods refuse to write.

.. code-block:: python

    ca = await FPGAArray('crs 247 224', mode='shuffle8')

    ca.get_fpga_tx()                                     # (swing, pre, post) codes per lane
    ca.set_fpga_tx(*ca.nearest_tx_codes(950, 0.01, 3.99))  # 950 mV, 3.99 dB post-cursor (= the reset value)
    ca.set_fpga_tx(0, 0, 21, lanes=[2], serials=['0224'])  # one lane of one board

    ca.read_optical()                                    # module settings, adaptive EQ enables, latched flags
    ca.retrain_optical()                                 # module TX input equalizer adapts to the new FPGA signal
    ca.set_optical(adaptive=False, tx_eq=5)              # manual module settings (volatile)

    ca.restore_si_defaults()                             # FPGA TX 950 mV / 3.99 dB, DFE, module reset

Notes:

- Codes are the GTY codes 0-31; ``CGE.TXDIFF_MV``, ``CGE.TXPRE_DB`` and ``CGE.TXPOST_DB`` give the values.
- After a change of the FPGA TX settings, run ``retrain_optical()`` if the optical module has TX input
  adaptive equalization (``read_optical()`` shows it). Otherwise it can keep the equalization it learned for the
  old signal.
- Optical module settings are lost when the module is reset, power-cycled or replugged.
- ``set_fpga_rx_lpm(True)`` selects the LPM receiver equalizer. It only affects what the CRS receives.
