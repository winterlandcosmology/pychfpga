# Coding Preferences

These preferences apply throughout this repository. Update this document when the user clarifies or adds a preference.

## Design and scope

- Start from the actual production workflow and implement only the states and transitions that workflow requires.
- Prefer the simplest design that satisfies the current workflow. Do not add synchronization, lifecycle management, or abstractions for unsupported scenarios without an explicit requirement.
- Do not add state flags to guard operations that the owning lifecycle cannot normally invoke.
- Keep changes narrowly scoped. Preserve unrelated behavior and avoid opportunistic refactors during fixes or rollbacks.
- Reuse established code paths and existing APIs when they already model the required operation.

## Compatibility and naming

- Preserve existing public function names, argument names, variable names, configuration keys, and calling conventions unless a rename is explicitly requested.
- Avoid API migrations for internal neatness alone.
- When reverting a recent change, restore the known earlier implementation from history where practical instead of recreating an equivalent implementation from memory.
- Clearly distinguish restored behavior from newly implemented behavior in summaries and review notes.

## Validation and errors

- Keep validation blocks visually separate from the code that applies changes, using a short comment where the boundary is not otherwise obvious.
- Prefer using simple expressions directly at their point of use instead of assigning one-use formatting or conversion variables.
- Retain low-risk quality-of-life improvements when rolling back larger design changes, especially input validation, sanity checks, actionable error messages, and state restoration with `try/finally`.
- Validate complete operations before performing hardware writes or modifying any member of an array, so failures do not leave partially updated state.
- Reject unsupported or conflicting operations explicitly rather than attempting a complex recovery mechanism.

## Verification and reporting

- Add focused regression coverage for intended behavior and failure restoration.
- Remove tests that exist solely for behavior being intentionally discarded, while preserving tests for retained fixes.
- In the final report, state precisely what was restored, what was newly implemented, and what was intentionally left unchanged.
- Do not describe a pre-existing mechanism as newly added or reimplemented.

# Agent working notes

Standing instructions and durable knowledge for AI agents working in this
repository. Maintained at the user's request; keep this file curated and
concise.

## Ground rules

- **Never create git commits unless explicitly asked.** Implement changes in
  the working tree and report; the user decides when to commit. When asked,
  use atomic commits with descriptive messages in the `type(scope): summary`
  style; bug fixes go in separate commits explaining both cause and solution.
- Store durable repo knowledge in this file, not in custom side files.

## Multi-platform rule

pychfpga drives both ICE/IceBoard (`CAPTURE_TYPE == 'PROBER'`) and CRS/ZCU111
(`CAPTURE_TYPE == 'UCAP'`) platforms, and their firmware feature sets diverge
(e.g. the ct_prober exists only in ICE corner-turn bitstreams and is neither
implemented nor generated for CRS). Every ICE-only feature must account for
CRS explicitly: accessing or instantiating a missing feature raises a clear
error naming the platform/capability mismatch — never a generic
invalid-argument message or a silent no-op — and firmware tests carry skip
guards so the suite runs cleanly with `config_crs.yaml`. Follow the existing
three-way `if CAPTURE_TYPE == 'PROBER' ... elif 'UCAP' ... else raise` idiom.

## Test workflow notes

- Firmware tests: `.venv/bin/python -m pytest <file>` from `firmware_tests/`
  (config via pytest.ini → config_ice.yaml). `prog=1` skips reprogramming
  when its check deems the loaded image current — after swapping a
  same-named bitstream file, force with `prog=2` once.
- `read_raw_frames()` has no timeout: capture tests must gate on a live
  dataflow indicator first (see ct_common.require_ct_dataflow).
- The corner-turn capture probe's design decisions and register map live in
  chfpgalite's AGENTS.md.
- Crate pre-deployment QC: `python f_engine_tests.py` from `qc/ICE_crate/`
  opens the xreport menu (it must be run from that folder — the menu resolves
  test paths relative to the config). Crate health there is measured by
  `pychfpga.fpga_firmware.chfpga.ct_health.measure_crate_health`, the same
  pass the firmware suite's `test_crate_health` uses, so both judge a crate by
  identical criteria; change it in one place only.
- The ICE QC suite drives the bench supply through the in-repo
  `pychfpga/hardware/Agilent_N5764A` driver, not McGill's `labpy`. labpy was
  dropped in 2026-09 because it imports `pymeasure` 0.10.0, which needs
  `pkg_resources`, removed in setuptools >= 81 — the whole suite stopped
  importing. labpy is not a declared pychfpga dependency, so nothing pinned
  it. The old code path and its config keys are kept as a commented reference
  block in `qc/ICE_crate/f_engine_tests.py`; restoring it needs a
  `setuptools<81` pin. Note the two drivers report supply state differently:
  the in-repo one never returns `'ON'`, only `'OK'`/`'OFF'`/`'ILIMIT'`/`'FAULT'`.
- Host UDP receive buffers: one recommendation for every mode, defined once in
  `network_utils.RECOMMENDED_UDP_SYSCTLS` (rmem_max = rmem_default =
  udp_rmem_min = 256 MiB) and checked by `test_udp_buffers_size` in both
  funcgen_test.py and ct_shuffle256_test.py (first test there, before any
  capture). CRS receives data over UDP too, so the same values apply; the
  TCPipe control link uses tcp_rmem and is unaffected. Derivation: the kernel charges each queued packet's buffer
  (truesize), not its payload — 4352 B per 690-B UCORR packet on igb/MTU 9000
  (measured on SN0516, 2026-11-18), so the 208 KiB default holds ~49 packets
  (2 ms of corr16) and the old 25 MiB ~0.25 s. 256 MiB rides out ~1 s at 1 GbE
  line rate, the ceiling of every mode. raw_acq and the test receivers never
  set SO_RCVBUF, so rmem_default is what matters. Check for kernel drops with
  `RcvbufErrors` in /proc/net/snmp; NIC-level drops show as `missed` in
  `ip -s link` (raise the ring with `ethtool -G <if> rx <max>`).
- Crate power cycling in QC goes through `TestUtils.power_cycle()`, which
  honours `ps_off_dwell` (>= 5 s with the output off) and `ps_t_sleep` (the
  crate's ARM boot takes 2-3 minutes). A boot wait shorter than that races the
  boot and manufactures init failures; do not shorten it to speed a run up.

## Deployment scripts

- Only `ps_power_on.py`/`ps_power_off.py` are tracked (their `power_on()`/
  `power_off()` are tested in `tests/test_ps_power.py`). The crate scripts
  (`crate_common.py`, `crate_start_shuffle256.py`, `crate_watchdog.py`),
  their `README.md` runbook and `tests/test_deployment_scripts.py` are
  site-local and gitignored: they exist only in the site's working tree.
- `tm4_time_check.py` (stdlib-only, tested in `tests/test_tm4_time_check.py`)
  reads the site TM-4 GPS clock (NPort 10.10.8.1:1001) and fails unless it has
  Time Valid and agrees with the host's UTC. It never changes a setting
  (`#13` requests only; gps.py `poll_metrics()` sends `#17` and does). The
  TM-4 cannot be set by hand, only by GPS: from 2026-11-16 it ran without an
  antenna at 2035-06-25 (+271424896 s), so the IRIG-B time and the correlator
  files' `irigb_time` were off by that much (correct from `ctime`). The
  IRIG-B decoder reads the TM-4 correctly; connect the antenna.
- Site crate: MGK7BP16 SN012, ARMs at fixed addresses (slot N at
  10.10.2.(N-1)), bench supply at 10.10.5.1. The correlator host
  (hirax-corr-00) reaches 10.10.2.x through a router, so mDNS never sees the
  boards: the crate scripts list the ARMs by IP (`crate_common.DEFAULT_HWM`),
  and an hwm by backplane serial ('bp16 012:0') does not work from there.
- Boards given by IP without a known crate go through
  `FPGAArray.init()`'s `discover_crate_async` gather, where all 16 ARMs read
  the backplane IPMI at once. On SN012 that call ended in "Server
  disconnected" twice, killing the Tuber servers of 2 and then 12 ARMs (only
  a power cycle recovers them); reading it one board at a time was harmless.
  `DEFAULT_HWM` therefore names the crate and attaches every board to it
  (`MGK7BP16 012:0 MGK7MB 10.10.2.0:1:0 ...`), which skips that step. Do not
  drop the crate or the `:0` crate fields from it.
- `deployment_scripts/ps_power_*.py` stay stdlib-only (system `python3`);
  the crate scripts (`crate_start_shuffle256.py`, `crate_watchdog.py`)
  re-exec themselves under the repo `.venv` via `crate_common.ensure_venv()`.
- `crate_watchdog.py` reads only the ARMs (Tuber `MB_FPGA_DIE`) and opens
  the array with no `mode` (init 0). Keep it that way: opening the FPGA link
  writes networking/GPIO/reset registers on a live crate. Tuber calls have no
  timeout (a hung ARM blocks for aiohttp's 300 s default), so every poll is
  wrapped in `asyncio.wait_for`.

## Corner-turn crate (shuffle256) notes

- `CTProber.get_packet_geometry` derives the geometry from the CROSSBAR3
  bin-sel registers whenever `CROSSBAR3.BIN_SEL[0].BYPASS == 0` (shuffle256/
  512/chord16). The old CB1-only readback under-reported shuffle256 packets
  (326 vs the real 597 words) and broke both the capture FIFO ceiling and
  the receiver configuration. `four_bits`/`use_offset_binary`/`send_flags`
  always come from CB1: the CB2/CB3 header-population registers
  (FOUR_BITS, NUMBER_OF_OUTPUT_BINS_PER_FRAME, ...) are never programmed by
  init_crossbars and read zero — the same is true of the inner packet
  header's bins/channels fields at CB3.
- `ShuffleCrossbar.get_lane_map` had a read-length bug (u2 element count →
  reads twice the bytes and decodes only every other one); fixed to the
  nibble-packed u1 readback matching `set_lane_map`. `get_reverse_lane_map`
  and `get_shuffle_output` depend on it.
- Dead status registers on the shuffle crossbars — never assert on:
  `DATA_TIMEOUT_MON`, `HAD_TIMEOUT`, `CAPTURE_DONE`, `TIMEOUT_PERIOD`
  (write is a no-op) and BP_SHUFFLE `RX_FRAME_CTR`. The real lane-absent
  flag is `MISSING_FRAME_MON`. `PACKET_ERROR_CTR`/`OUTPUT_FRAME_CTR` (and
  all CROSSBAR3 per-lane arrays) are only valid for `LANE_MONITOR_SEL`
  0..7. Backplane link health = `gtx[n].BLOCK_LOCK` (64b66b sync);
  `RX_RESETDONE` is timer-based and asserts with no cable.
- Crate test discipline: never call `board.sync()` or `board.reset()` on a
  crate member (board sync is local-only and breaks the array-wide frame
  alignment), and never reconfigure the corner turn (`init_crossbars`) on a
  live crate — connecting `FPGAArray(mode='shuffle256', open=1)` performs
  the whole bring-up and the tests read the operating point from registers.
  The array-wide sync is `ca.sync(max_sync_time_difference=...)`; pass the
  limit explicitly (the provider-attribute fallback in
  chfpga_array_methods.sync is fixed but don't rely on it).
- sep24 bitstream (`chfpga_ice_ct_pbg_sep24.bit`, the ICE default): the 8
  corner-turn output lanes emit on per-lane strobes and legitimately leave up
  to ~230 ns apart - associate packets across lanes by header timestamp,
  never by simultaneous arrival. The packet-flags trailer is a real error
  register (zero when clean; bits 0-7 CB3 per-lane, 8-11 CB3 summary
  missing/long/CRC/overflow, 12-27 CB2 per-input-lane, 28-30 CB2 summary).
  The ct_prober arms on packet boundaries. Pre-sep24 builds had all three
  broken (8*lane+1 duplicated headers on lanes 1-7, data-polluted trailers
  that saturated CROSSBAR3's PACKET_ERROR_CTR unconditionally, ~1-in-30
  one-word-shifted captures) - do not chase those symptoms on sep24+.
- One 0.5 s health pass misses intermittent single-link CRC faults (crate
  012, 2026-10-16: 2 CRC errors on one link in one pass, none in five
  later passes). `test_crate_health_soak` repeats the shared ct_health pass
  (`health_soak_passes`, default 60, ~2.5 min, the QC backplane_err
  standard); `-k test_crate_health` runs the single pass and the soak.
- The PRBS BER path (`get_ber`, used by qc/MGK7BP16 `test_bitErrorRate`) is
  not usable on ICE on this branch: the QC test never awaits the coroutine,
  `get_link_map` always builds the QSFP map (KeyError on a crate without QSFP
  cables), and once those are bypassed the GTX `ERR_CTR` reads 0xFFFFFFFF or a
  fixed-rate count on links that are clean under traffic (crate 012,
  2026-10-16). Use `test_backplane_error_rate` instead: live-traffic CRC
  counts over 60 s windows (default 30 min), scored with get_ber's
  conventions against BER 1e-10 (fewer than one error per 16 s per link).
  The backplane `RX_ERROR_CTR` is 16-bit, saturating at 0xFFFF.
- CROSSBAR2's SOF window is 100 (the RTL default). The 55 that software
  forced until 2026-09 sat on the measured lane arrival-skew tail (44-57
  ct_clk cycles) and silently dropped ~1% of frames;
  test_alignment_window_margin now guards the margin every run.
- Remote-crate workflow: `firmware_tests/config_ice_crate.yaml` is the
  single user-edited file (crate hwm placeholder). Run only
  ct_shuffle256_test.py against it, stage by stage in file order (health →
  alignment → geometry → algorithm → flags → gpu), stopping at the first
  failure; every assertion message is written to be actionable from the log
  alone. Captures at frames_per_packet=2 with flags need ~4.8 KB UDP
  packets on the capture path — if geometry tests time out with zero
  packets, check the receiving host's interface MTU (needs ≥ 5000).
- Receiver-end fibre checks use the lane-ID payload:
  `deployment_scripts/ct_lane_id_payload.py` (calls
  `FPGAArray.set_lane_id_payload`) makes every data byte of lane k of 0-based
  slot s equal `(s << 4) | (k + 1)`. It bypasses only the FFT and SCALER;
  the corner turn runs as in production. The fibre map and lookup table are
  in that script and `docs/shuffle256_lane_id_payload.md`, and
  `test_lane_id_payload_shuffle256` checks the payload through the prober.
  `chFPGA.set_channelizer_outputs` works again and is crate-safe with
  `local_sync=False`. It is ICE-only (16 channels, 8-bit samples) and
  refuses CRS/UCAP boards before any write.
- Facts confirmed in the chfpgalite RTL:
  - Lanes 0-3 go to QSFP A and lanes 4-7 to QSFP B (xdc pin constraints);
    the TX lane order inside a cage is not in the source.
  - Header word 2 = frame counter [47:32].
  - 10G words are sent LSB first.
  - Funcgen buffer mode forces the ADC-overflow flag to 0.
  - Scaler bypass applies no offset binary.
  - Board init resets FUNCGEN and SCALER but **not** `FFT.BYPASS`. After any
    FFT-bypass test (the algorithm/flags stages or the lane-ID payload), only
    a `prog=2` bring-up restores sky data.
  - `PROBER_USER_FLAGS` is 1 at power-up (`prober.vhd:440-445`): on
    scaler-source prober captures it overwrites the low nibble of every byte
    with TUSER flags. Harmless for 4-bit captures (the decoder drops the
    nibble), but it corrupts every wider scaler capture (fft_full, fft_2x,
    MSB modes). It only affects the prober's capture FIFO, never the scaler's
    main 4-bit + TUSER output to the corner turn/correlator. Until 2026-11-18
    fpga_master left it set, so ICE gain calibration measured an fft_full RMS
    ~25x too high and set gains ~25x too low (all-zero corr16 data);
    `start_fpga_raw_data_transmission` now clears it for PROBER gain capture.
    Firmware tests clear it themselves (`prober_user_flags=False`).

## ICE corr16 correlator (UCORR)

- Default corr16 bitstream is `chfpga_ice_corr16_2026_11_20.bit` (since
  2026-11-20; adds the saturation flags, below). It superseded
  `chfpga_ice_corr16_2026_11_18.bit` (readout fix, no flags), which was removed
  from the repo. Earlier corr16 builds (`chfpga_ice_corr16_pbg_jun16.bit` and
  before) have a UCMAC44_ARRAY readout fault: every bin except the readout
  bin holds only what accumulated between the bank switch and its readout
  (a ramp across the band, ~10% of the true power, independent of the
  integration period). Do not use them for data.
- Verified on SN0516 with the new build: identical 4-bit input on all inputs
  gives exactly `N * |x|^2` per bin per firmware integration (1022/1024 bins;
  30660/30660 values through fpga_master + raw_acq + HDF5).
- Bin 0 of each core (bins 0 and 512) is the `READOUT_BIN` whose slot is
  used for readout: its value is invalid by design. Integration periods below
  2048 frames are below the documented minimum and read out incompletely.
  Products are 18-bit and saturate at 131071 (e.g. integ 65536 at the 2.12
  RMS gain target).
- Saturation flags (2026-11-20 build): bit 0x20 (real) / 0x10 (imag) of
  byte 4 of each 5-byte product, the bits the decoder already reads. A
  component is flagged when it sits on a clamp rail (+131071 or -131072, the
  sticky saturation values), so an exact -131072 sum is flagged too.
  Verified on SN0516: real and imaginary flags match the expected overflow on
  every bin (autos and a phase-rotated cross product, N=16384/32768/65535),
  raw packets included; the 11-18 and earlier builds zero-pad the product
  (`UCMAC44_ARRAY.vhd:337`) and never set them. Full single-board suite on
  the 11-20 build: 226 passed, 108 skipped (platform limits), 1 failed only
  for lack of an external ADC signal. The jfc/merge-vb-ct
  `test_sat_detection` (FFT-bypassed buffer injection) does not set up its
  stimulus correctly on ICE on this branch, so it cannot judge the flags.
- Control-link outages (crate 012 slot 8, 2026-11-20 to 25): about every
  2.5 h the FPGA's UDP control replies arrive with the wrong first byte for
  ~35 s, mostly 0xCF (the UCORR cookie, 1555 of 1874), at the right reply
  length. The FPGA's outgoing UDP path is mis-framing (data bytes in control
  replies); it is digital, not the SFP (no CRC loss, no temperature link).
  fpga_master recovers with `clear_fpga_udp_errors` (SFP switch + UDP stack
  reset). Suspected RTL: `udp_mac_tx` sends by frame-FIFO length without
  checking TLAST, and `bsb_master` ignores TX back-pressure. The recovery
  logs register 19 (sticky tx/rx_overflow, bits 17/16) before the reset and
  "Invalid sequence number" messages carry the reply's first bytes: check
  those at the next outage before chasing the firmware.
- raw_acq UCORR receiver hardening (after an 18 h loss, 2026-11-24): three
  mis-framed packets that started with 0xCF carried timestamps more than
  18 h ahead, took 3 of the 4 assembly rows, and every record kept only its
  first firmware integration (counts 1024 of 131072). The decoder now
  requires the exact packet length, drops keys more than `max_timestamps`
  ahead (following a real jump once a full firmware integration arrives
  there), and logs incomplete records once a minute (metric
  `raw_acq_corr_incomplete_integrations`). Reproduce with
  `tests/test_ucorr_stray_packets.py`.
- Frame-time capture (`capture_frame_time_async`, every 10 s with IRIG-B
  sync): register 12 is now written from the IRIG-B source cached by
  `set_irigb_source_async` (a read-modify-write during an outage could write
  a garbage source back), with a 0.5 s timeout and one re-arm. The frame
  trigger `manual_event_sample_trig` has no clock-domain synchroniser in
  `gpio.vhd` (the refclk trigger does); a likely cause of missed triggers,
  not confirmed on SN0505 (0 misses in 200).
