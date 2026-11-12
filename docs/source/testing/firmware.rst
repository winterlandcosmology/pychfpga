----------------
Firmware testing
----------------

Tests of the chFPGA firmware on a live board, under ``firmware_tests/``. A test programs the
board, injects a known waveform with the function generator (FUNCGEN) or reads a status register,
and compares the captured output with a host model.

They are pytest tests. The ``wtl.pytest_xreport`` plugin adds the menu, the prompts, and the HTML
and PDF reports.

Installation
============

The test dependencies are not in the base install:

.. code-block:: bash

    uv sync --extra qc          # or: pip install -e ".[qc]"

Running the tests
=================

Always run from ``firmware_tests/`` so that ``pytest.ini`` is used:

.. code-block:: bash

    cd firmware_tests
    pytest -v                                      # CRS: config_crs.yaml (default)
    pytest --xreport-config=config_ice.yaml -v     # IceBoard

.. code-block:: bash

    pytest funcgen_test.py -v
    pytest scaler_test.py::TestScaler::test_flags -v
    pytest corr_test.py -m corr -v

``-k`` selects tests by name. It is a substring match, not a shell glob: the word
``ramp`` runs every test whose name contains ``ramp``, with no ``*``. Matching is
case-insensitive. ``and``, ``or``, and ``not`` combine words. Quote the expression
when it contains those words:

.. code-block:: bash

    pytest -k ramp                              # test_funcgen_ramp, test_ramp_gains, ...
    pytest -k "funcgen and not noise"           # FUNCGEN tests except the noise ones
    pytest -k "not simulation"                  # skip the CASPER simulation comparisons

``-m`` selects by marker instead of by name. The markers are listed in
``firmware_tests/pytest.ini`` (``funcgen``, ``funcgen_patterns``, ``funcgen_noise_gen``,
``funcgen_counters``, ``fft``, ``scaler``, ``complex``, ``floating``, ``statistical``,
``corr``, ``ct_prober``, ``ct_flags``, ``ct_shuffle256``):

.. code-block:: bash

    pytest -m funcgen_patterns
    pytest -m "scaler and not floating"

Plots and XML results go to ``fw_test_results/`` (see ``--xreport-path``).

Edit the YAML file before a run:

- ``connection_config.hwm``: Board to test, e.g. ``crs 55``.
- ``connection_config.mode``: Firmware mode, e.g. ``corr8`` or ``shuffle8``.
- ``connection_config.prog``: ``0``: do not program; ``1``: program if outdated; ``2``: always program.
- ``test_config.supported_modes``: Modules that may run in each mode. Tests of other modules are skipped.

The helpers in ``firmware_tests/fp_fft/`` and ``firmware_tests/casper_simulations/`` are not part
of the pytest collection. ``casper_simulations/`` supplies the reference spectra used by the FFT
simulation comparisons.

Function generator
==================

``funcgen_test.py``. The FFT and the scaler are bypassed, so the captured samples are the
FUNCGEN output. Pattern tests compare one frame with the waveform the host programmed. Noise
tests check the LFSR statistically. A few tests read FUNCGEN status registers instead of the
sample stream.

``test_packets_number`` does not exercise the FUNCGEN. It captures one period and checks that
every expected packet arrived.

Waveforms (``FN_RAM``)
----------------------

- ``test_funcgen_ramp``: A ramp across the sample range.
- ``test_funcgen_sin``: One cycle of a sine wave, amplitude 127.
- ``test_funcgen_arb``: An arbitrary buffer, here a constant 1.
- ``test_funcgen_const``: A constant value (13).
- ``test_funcgen_ab``: The two-value pattern ``a, b, a, b, ...``.
- ``test_funcgen_real_ramp``: A ramp on the real samples, with the imaginary samples held at 0.

Noise (``FN_NOISE``)
--------------------

- ``test_funcgen_deterministic``: Channels programmed with the same seed produce the same sequence. ``reset_noise()`` restarts it.
- ``test_funcgen_random_pval``: The fraction of set bits matches a fair random source.
- ``test_funcgen_noise_histogram``: The sample histogram matches the expected distribution.
- ``test_funcgen_noise_min_max``: The extreme samples stay inside the ADC range and reach both ends.
- ``test_funcgen_noise_psd``: The spectrum of the noise is flat.

Counters, shift, and overflow
-----------------------------

- ``test_word_and_frame_ctr``: ``word_ctr_buffer_flags`` mode: the captured words carry the expected frame and word counts. The low 8 bits of the ``FRAME_CTR`` register are not checked on their own.
- ``test_frame_counter_byte``: ``frame8`` and ``frame4`` modes: the frame number is in the expected sample bits. Skipped when the PROBER burst is longer than 4 frames.
- ``test_frame_counter_nibble``: ``nibble4`` mode: the frame number is in the high nibble, including the even/odd swap.
- ``test_reset``: After ``FUNCGEN.reset()``, ``SEND_FRAME`` follows ``ENABLE`` and ``OVERFLOW_CTR`` is 0.
- ``test_shift_clip_width``: A programmed left shift (with saturation) and right shift matches the host model. The host model can disagree with the RTL saturation rule, so a failure here is not always a firmware fault.
- ``test_overflow_ctr``: ``OVERFLOW_CTR``, latched when ``STATS_DONE`` is set, equals the injected overflows over ``STATS_PERIOD`` frames. Clearing ``STATS_ENABLE`` and re-arming is not tested.
- ``test_delay_capture``: Reads ``DELAY_CTR``. The channel build leaves ``IMPLEMENT_DELAY_CAPTURE`` false, so this counter is not generated and the test result does not mean the delay measurement works.

Not covered:

- ADC passthrough (``FN_ADC``) and the unused function select.
- ``FRAME_OVERFLOW_FLAG``.
- An isolated check that RAM paging crosses a 512-byte page boundary.
- ``test_udp_buffers_size``, ``test_mtu_size``, and ``test_lfsr_output`` are commented out.

FFT
===

``fft_test.py``. FUNCGEN plays a known time series into the FFT. The scaler uses a fixed complex
gain so the captured bins can be compared with a reference. Every test runs on channel 0 only.

Peak and shape (against ``numpy.fft``)
--------------------------------------

These check which bins are occupied and the shape of the spectrum. They are not a bit-exact
comparison.

- ``test_fft_dc_peak``: A constant input. The energy is in bin 0.
- ``test_fft_single_tone_peak``: A tone in bin 8, 11, or 37. The peak is in that bin.
- ``test_fft_two_tone_peaks``: Tones in bins 11 and 53. Both peaks are present.
- ``test_fft_ramp_spectrum_shape``: A large ramp. The spectral envelope matches NumPy. The amplitude is high so quantization does not dominate after the FFT shift.
- ``test_fft_noise_spectrum_shape``: Seeded noise, same shape comparison.

Bit-exact comparison with the CASPER simulation
-----------------------------------------------

``test_compare_to_simulation_ramp``, ``test_compare_to_simulation_two_tone``, and
``test_compare_to_simulation_noise`` capture one frame and compare it with a stored simulation
(``casper_simulations/sim_data``). The tolerance is 0 for the configurations that are defined.
The capture must report no overflows.

The comparison is defined for IceBoard (``fft_full``, 8-bit ADC, 4 inputs) and CRS
(``fft_2x``, 12-bit ADC, 8 inputs). Other platforms are skipped. A board whose firmware reports
no FFT is skipped.

Not covered: channels other than 0, FFT shift schedules other than the ones in
``SIM_COMPARE_CONFIGS``, and a sweep of the scaler gain in front of the FFT.

Scaler
======

``scaler_test.py``. FUNCGEN feeds a ramp (or another waveform) with the FFT bypassed, so the
scaler sees the samples directly. The test computes the same gain, shift, rounding, and
saturation in integers and compares the captured stream with that result.
``connection_config.mode`` must be listed under ``test_config.supported_modes.scaler_test``
(typically ``shuffle8`` or ``corr8``).

Gains
-----

- ``test_all_zeroes``: A gain of 0 forces every output sample to 0 over the full input range.
- ``test_all_ones``: Unity gain reproduces a full-range ramp.
- ``test_constant_gain``: One real gain, with a chosen postscaler. Skipped when the platform has no floating-point gains and the test asks for them.
- ``test_alternating_gain``: The gain alternates with a programmed period.
- ``test_ramp_gains``: The gain changes from bin to bin as a ramp.
- ``test_alternating_complex_gains``, ``test_periodic_complex_gains``: Complex gains. Skipped on a platform without them.
- ``test_uniform_common_log_gain``, ``test_common_log_gain``, ``test_ramp_float_gains``: The common logarithmic gain and floating-point gains. Skipped when the platform has no floating-point gains, and when the requested log gain is outside 0–31.
- ``test_gain_bank_switching``: Banks 0 and 1 hold different gains. After the switch, the captured data and the bank-status register follow the new bank.

Rounding, saturation, encoding
------------------------------

- ``test_rounding_modes``: Truncation, normal rounding, and convergent rounding, for several fractional-bit widths.
- ``test_post_rounding_saturation``: Values that overflow after rounding saturate. Both symmetric and asymmetric saturation.
- ``test_zero_on_sat``: With zero-on-saturation enabled, an overflow of either the real or the imaginary part zeros both.
- ``test_offset_encoding``: Offset-binary output. Skipped in a correlator mode that requires two's complement.
- ``test_overflow_detection``: The overflow indication follows the samples that actually saturate.
- ``test_flags``: Saturation flags in the captured word. CRS uses scaler capture type 6 (``sat_flags``): +1, −1, or 0 per real and imaginary field. IceBoard uses the PROBER low nibble on capture type 0. Skipped when the output word is too wide to carry flags.

Capture path
------------

- ``test_bypass``: With the scaler bypassed, the FFT data is forwarded unchanged, either onto the science output or into the capture path.
- ``test_mult_resolution_capture``: Capture of several data types in one setup. Skipped in 4-bit capture mode.
- ``test_scaler_overflow_stats``, ``test_adc_overflow_stats``: The scaler and ADC overflow counters match the number of overflowing samples. On the CRS the ADC overflow count is checked here, not by stuffing the overflow bit into the captured sample.
- ``test_delay_counter``: After a reset, ``DELAY_CTR`` is the same on every channelizer: they all see the first sample on the same clock.
- ``test_counter_reset``: While ``SCALER.RESET`` is held, the monitor counters stay at 0. After it is released they move.
- ``test_averages``: Integrated scaler output matches the host sum over ``num_frames``, for a range of postscalers and rounding modes.

Not covered: 8-bit capture is fixed at 4 bits in the test constants (there is a TODO). The
complex-gain and floating-point-gain tests do not run on a platform that lacks those features.

Correlator
==========

``corr_test.py``. Requires a correlator mode such as ``corr8``. The FFT is bypassed and FUNCGEN
writes a complex spectrum straight into the bins ("spectrum injection"). The test integrates
that spectrum in the correlator and compares the products with ``spectrum * conjugate(spectrum)``
summed on the host. Only bins the firmware marks as valid are compared.

The first three tests check the injection itself, by reading the spectrum back. The rest fail
if that path is wrong.

Spectrum injection
------------------

- ``test_constant_spectrum_injection``: Every bin is a constant (1, −1, or 2). Read back in capture modes 0 and 4. No shift is applied.
- ``test_complex_ramp_spectrum_injection``: A complex ramp. Adjacent bins differ, so a swapped bin order fails.
- ``test_periodic_ramp_spectrum_injection``: A ramp from −8 to 7 that repeats across the band.

Autocorrelation
---------------

- ``test_constant_autocorr``: Every channel is one complex value. The autocorrelation is its squared magnitude times the number of frames.
- ``test_alternating_autocorr``: Each channel alternates between two values. The power lands in the bins those values occupy.
- ``test_periodic_autocorr``: A 1 every ``period`` bins, among zeros. Checks that bin order survives the correlation.
- ``test_full_range_autocorr``: The spectrum uses the full sample range.
- ``test_zero_except_one_autocorr``: One channel is non-zero. Only that channel's autocorrelation is non-zero.

Cross-correlation
-----------------

- ``test_zero_except_two_crosscorr``: Two channels are non-zero. Checks the auto products and, unless ``autocorr_only`` is set, the cross product. The other products stay zero.
- ``test_crosscorr_conjugation``: Channels hold ``2`` and ``1+1j``. The cross product must be ``a * conjugate(b)``, not ``a * b``.
- ``test_sat_detection``: A large complex ramp is integrated until the 18-bit accumulator overflows. The saturation field is set on those bins. On the CRS this field is a count, not a one-bit flag.
- ``test_software_integration``: ``raw_acq`` sums 1, 2, or 4 firmware integrations. The result scales with the number of frames.

Not covered: the check that the number of received correlator packets equals the number expected
is commented out. Injection through a real FFT (instead of the bypass) is not part of this file;
that is what the FFT tests do.

Corner-turn capture probe
=========================

``ct_prober_test.py``. These tests need a bitstream that implements the corner-turn capture
probe (an ICE board with corner-turn level 3). On any other firmware every test in this file
skips, except ``test_ct_source_rejected_without_probe``, which checks that ``start_ct_capture``
raises when the probe is absent.

The probe snapshots one corner-turn output lane and sends those packets on the capture link
with cookie ``0xA2``. The tests check the lane select, the burst-length limit, the packet
header, and the decoded samples against the host model of the first crossbar. A second group,
marked ``ct_flags``, turns packet flags on and checks the data-flags, frame-flags, and trailer
words.

The parameters live under ``test_config`` in ``config_ice.yaml`` (``global_lane``,
``global_fpp``, and the per-test blocks). ``config_crs.yaml`` does not define them.
Its ``skip_tests`` entry skips every probe test except
``test_ct_source_rejected_without_probe`` before pytest builds the cases, so a CRS run
does not connect for the ones it cannot execute.

Shuffle256 crate
================

``ct_shuffle256_test.py``. A 16-board ICE crate in mode ``shuffle256``.
``config_crs.yaml`` lists this file under ``skip_tests``, so a CRS run skips it before
connecting. Any other config skips it, still without connecting, unless
``connection_config.mode`` is ``shuffle256``. Use
``firmware_tests/config_ice_crate.yaml`` and edit its ``hwm`` to the crate's backplane serial.
That file does not set ``bitfile``; the platform table loads ``chfpga_ice_ct_pbg_sep24.bit``.

.. code-block:: bash

    pytest --xreport-config=config_ice_crate.yaml ct_shuffle256_test.py -v

Run the tests in file order and stop at the first failure. They check backplane lock and the
two shuffle crossbars, then packet geometry, the byte-exact corner-turn output, frame flags,
and the GPU link. Crossbar objects are ``board.CT.CROSSBAR2`` and ``board.CT.CROSSBAR3``.
The crate health check is ``ct_health.measure_crate_health``.
