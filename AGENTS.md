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
