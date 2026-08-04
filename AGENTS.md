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
