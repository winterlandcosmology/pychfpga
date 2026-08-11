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
  dataflow indicator first (see the ct_prober suite's `_require_ct_dataflow`).
- The corner-turn capture probe's design decisions and register map live in
  chfpgalite's AGENTS.md.
