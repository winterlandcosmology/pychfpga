# Documenting style

How documentation is actually written in pychfpga (Python docstrings, `docs/source`) and in the matching chfpga firmware notes (VHDL comments, `docs/src`, especially `coding_conventions.rst`). This is a description of that style, then a rule set an AI can follow, then a few changes that would make the docs more useful without changing the voice.

The useful documentation is the part a reader cannot get by reading the next ten lines: units, off-by-one, ordering, what breaks if you get it wrong, and why a number or a disabled check is the way it is.

## Voice

Direct and operational. Write to someone who will run the code or wire the board, not to someone reviewing a paper.

- Short declarative sentences. A method docstring often starts with a verb: "Sets the scaler gains…", "Return the stream_ids…", "Initializes UCAP module". Capitalization and the final period are inconsistent; that is fine on a one-liner.
- Concrete names: register fields (`READOUT_DELAY`, `base_period`), clock domains, IP addresses, crate/slot, sampling rates, frame sizes. "The capture period" is weaker than "number of frames MINUS ONE".
- Parenthetical asides when the name is misleading or the code is a hack: "(sorry, non intuitive)", "temporary hack", "Sigh", "that is the hacky part".
- Warnings are about equipment and downtime, with the failure mode stated: corrupting an SD card, a power-supply setpoint that can destroy hardware, a UDP buffer that looks empty but still holds a partial timestamp.
- Typos and rough grammar are common and are left in. Do not "polish" a note into formal prose. Do fix a typo when it changes a unit, a bit range, or an off-by-one.
- Older modules carry a `History:` block (date, initials, what changed). New work relies on git. Do not invent a history section.

## Where the detail lives

Detail is split on purpose. `chfpga/docs/src/coding_conventions.rst` already says this: short text that is tied to the code stays next to the code; longer notes on how to use a block, and on constraints and tradeoffs, go in the RST page. The page is not a narration of the source.

| Place | What belongs there |
| --- | --- |
| Module docstring | One or two sentences: file name and what the module is for. Sometimes a `History:` block on old files. |
| Class docstring | What the object is, and what it does *not* do yet if that is surprising (`__init__` creates an empty handler; `open_async` talks to the board; `init_async` configures firmware). |
| Method docstring | What the call does to the hardware or the data, then the parameters whose meaning is not the name. |
| `BitField(..., doc=)` | One phrase of register semantics. A short encoding table when the field is a mode, not a number. |
| Inline comment | Why: a calculation, a unit conversion, a board revision hack, a check that is disabled and the reason, an assumption about a socket or a clock. |
| VHDL comment | Same as the inline comment, plus the ASCII memory-map table. |
| RST howto | The commands you type, what they return, and what goes wrong. |
| RST entity page | Overview, how to set generics and ports, non-obvious design constraints. Memory map is included from the VHDL comment, not rewritten. |

A fact lives in one of those places. The Python class does not restate the memory map. The RST page does not restate the process line by line.

## Level of detail

### One line, or nothing

- Getters, thin wrappers, and private helpers whose name is the behavior (`get_stream_ids`, `normalize_source`, `_collect_gain_calc_server_maps`).
- `init()` methods that only program a known reset state. "Initializes the FFT module" is enough.
- Status bits that exist for bring-up. The docstring is `debug`.
- Parameters whose name, type, and default already say what they are.

### A sentence plus the non-obvious arguments

Most public methods. The first sentence says what changes (source, rate, bank, address). `Parameters:` follows only for arguments that have a unit, a range, an encoding, or a surprising meaning.

`set_data_capture` is the pattern: one sentence on what is selected, then `source` as the legal names and indices, `periods` as frames MINUS ONE, and which argument is ignored in which mode.

### A real explanation

Use this when a wrong call wastes time or damages something, or when the behavior is split across calls.

- **Ordering.** `__init__` does not touch the board. Raw capture is not synced until `sync()`. Base period changes need a reset; the power-of-two sub-period does not.
- **Units and encodings.** Frames vs seconds vs register counts. ADC frames vs the old "register units × 256". Offset binary vs two's complement. "MINUS ONE" on integration and capture periods. Bit slices of a stream id (crate in 11:4, channel in 2:0).
- **Side effects and assumptions.** Opening a short TCP connection to discover the host interface, and that this must not disturb the board. A sticky overrun. A default delay that starts dropping packets below 75 frames on a particular switch.
- **System setup the code cannot do.** Jumbo MTU, `sysctl` UDP buffers, the exact commands, and how to watch `/proc/net/udp`.
- **Safety.** Power-supply voltage and current that persist across power loss. Do not interrupt an SD-card rewrite.
- **Tradeoffs.** UCAP modes are a fixed budget: 2 frames × 8 channels, 4 × 4, 8 × 2, or 16 × 1. Say that once.
- **Firmware/software mismatch.** Pipeline delay is not measurable yet on CRS, so the check is commented out and the comment says why.

`RawFrameReceiver` and the pocket-correlator module docstring are the long form: a few sentences on packet size and rate, then a `System Requirements:` block of commands. That length is for I/O and for anything you configure outside Python. It is not the default for a register write.

### Comments, not docstrings, for the "why this line"

```text
# Default stagger step in ADC frames between crate slots (~1024 frames; was 4 register units x256).
DEFAULT_DELAY_PER_SLOT = 80  # Seems to be sufficient for the TP-Link switch. Starting to lose packets below 75.
```

```text
# For the CRS, the pipeline delay is not yet measured at this point, ...
# So we can't check if it is right until the pipeline is running.
```

```text
-- data rate ratio: 3000 Gsps * 2 bytes/sample * 2 frames/capture * 8 channel / 100 Mbyte readout = 960.
signal base_period: ... -- 2 frames * 16 bytes, 3000e6/16384 = 183105.46875 =1s, max period = 91.6s
```

The arithmetic stays next to the constant. A docstring that repeated it would rot.

## Python docstring shape

Homemade reStructuredText. Not Google style, not NumPy style (no `----------` underlines).

```text
""" Selects the source of the data to be captured (FUNCGEN or SCALER). Optionally enables streaming the captured data instead of the correlator data.

Parameters:

    source (str or int): Capture source. String names or logical indices:

        - ``'funcgen'`` / ``0``: function-generator (ADC/timestream) tap
        - ``'scaler'`` / ``1``: scaler capture (spectrum) tap

    periods (list of int): Time between data captures for each of the sources, expressed in
        number of frames MINUS ONE. The number of consecutive frames stored by each capture
        is set by the `mode`.
"""
```

Habits:

- Blank line after the summary, then `Parameters:` if needed.
- Each argument is `name (type):` then the description, indented under the name.
- Nested choices are a bullet list with ``backticks``.
- `Returns:` when the caller must unpack a tuple or a dict and the shape is not obvious. Skip it when the function returns `None` or the name says it (`get_stream_ids`).
- `Example:` or `Example::` on REST endpoints, usually one `curl` line or a short IPython snippet. Rare on firmware methods.
- `Note:` for a constraint that is not a parameter (what `__init__` refuses to do).
- Sphinx roles (`:meth:`, `:class:`, `:mod:`) appear when a cross-reference is actually useful. They are not applied to every name.
- `Raises:` is almost never written. Exceptions are raised in the code; document one only when the condition is non-obvious.

`BitField` docs are a separate, tighter dialect:

- Flag: `When '1', offset binary encoding is used.` or `Resets the overflow counter.`
- Multi-line `doc=` when the field is a small enum (`DATA_TYPE`, `CAP_DATA_TYPE`): one line per code, with the combination of bits that selects it (`BYPASS=1, DATA_TYPE=2: ...`).
- Sticky, width, and "last 8 bits" belong in that phrase.
- `debug` is a finished docstring.

Attribute comments use `#:` when the value is an identifier a reader will look up (`_PLATFORM_ID_CRS`).

Module docstring, when it is more than one line:

```text
"""
fpga_array.py module. Defines the objects that represent and handles
operations on an array of FPGA motherboards.
"""
```

or the older header:

```text
""" Implements the interface to the PFB/FFT FPGA firmware module

.. History:
    2011-07-12 : JFC : Created from test code in chFPGA.py
"""
```

## Firmware comments and RST

VHDL entity banner: company, name, date, one-line description, dependencies (UltraRAM, VHDL-2008, Vivado version). The entity comment under that is two or three lines: what a frame is, and the channel/frame tradeoff.

Ports and generics: a trailing comment when the constraint matters (`current design works only with NUMBER_OF_CHANNELS = 8`, clock domain, synchronous reset).

Memory map: an ASCII table in the source, included into the RST with `include-docs`. Columns are page, address, bits, name, description. Empty description cells are acceptable for unused or obvious bits. A constraint goes in the description cell (`Keep base_period >> delay+burst drain or overrun may stick.`).

Entity RST pages, as set in `coding_conventions.rst`:

1. **Overview** — a short paragraph: what it does, and the main tradeoff.
2. **Entity documentation** — `parse` + `autoentity`. Do not hand-write the port list that this already emits.
3. **Parameters** — generics grouped by function, enough to configure the block. More than the one-line generic comment. Clock domain and data format belong here.
4. **Interface** — ports by group (input stream, output stream, BSB). Say which clock, and whether a reset is synchronous.
5. **Implementation details** — only non-obvious technique, constraint, or tradeoff (FIFO threshold vs pipeline delay, why the base rate needs a reset, clock-domain crossing).
6. **Memory map** — included from the VHDL table.

Howtos (`docs/source/howto`):

- Second person. "Open an ipython session, then…"
- A command block, then one or two sentences on what the variable is and what the flags changed.
- Numbers from the lab: 50 seconds per SD card, 120 seconds of flush, 25 seconds to boot, netmask `/22`, DHCP must not hand out `*.*.3.*`.
- A warning when the next step is hard to undo.

`introduction.rst` is the long overview: one paragraph of scope, then a bullet per instrument with the configuration name and the numbers that distinguish it (boards, sample rate, FFT length, link speed). That is the right depth for a top-level page.

## Compactness

- One dense sentence instead of a paragraph that restates it.
- Lists for modes, legal values, and steps. Prose for the reason.
- Do not document a parameter just because it is in the signature.
- Do not open with "This function is used to" or "This module provides a comprehensive".
- Do not add a Summary / Features / Limitations scaffold unless there is a real constraint to put in it.
- Bold is rare. It marks an assumption the reader will skip (`**This assumes that the brief socket connection will not cause side-effects**`) or a single dangerous fact in a howto.

## What has drifted toward generic docs

Some newer pages read like a walkthrough of the process: every state of the read sequencer, every pipeline stage of the scaler, bold labels on every bullet. That repeats the VHDL. It also goes against the line in `coding_conventions.rst` that the RST exists to explain use and non-obvious design choices, not what is already in the code.

A few docstrings have drifted the other way: they describe parameters the function no longer takes (`open_mmi_async`, the `select` argument on `set_data_capture`). The style is fine; the text is stale. Stale units and parameter lists are worse than a missing docstring.

---

## Rules for an AI

Follow these when writing or editing docstrings, comments, or RST in this project.

1. Write for the person about to call the function or instantiate the entity. State what changes, in what units, and what they must do before or after (reset, sync, power-cycle).
2. Match the existing voice. Short sentences, real register and signal names, lab numbers. An aside in parentheses is appropriate when a name lies or a workaround is temporary. Do not formalize that aside into a "Note" box.
3. Leave existing comments and docstring text in place unless they contradict the code. Update them; do not replace a whole docstring with a cleaner rewrite.
4. Do not correct grammar, spelling, or tone in prose you were not asked to change. Do correct a unit, bit range, count, or parameter name that is wrong.
5. Put each fact in one place:
   - Register bits and the memory-map table stay in the VHDL comment and in `BitField(doc=)`.
   - Why a constant or a disabled check exists stays in the inline comment next to it.
   - How to call it, including units and ordering, stays in the docstring or the RST parameter section.
6. Docstring length:
   - Module: one or two sentences. No feature list.
   - Class: what it represents. Add a short paragraph only to say what construction does *not* do.
   - Trivial or private method: one line, or no docstring if the name is enough.
   - Public method: one summary sentence. Add `Parameters:` only for arguments with a unit, range, encoding, default that surprises, or a value that is ignored in some mode.
   - `Returns:` only when the structure is non-obvious. Never `Returns: None`.
   - No `Raises:` section unless the trigger is not visible from the code.
   - `Example:` only for REST endpoints and interactive entry points. One command.
7. Parameter block format, exactly:

   ```text
   Parameters:

       name (type): description, wrapping under the name.
           Units and off-by-one in this description.

           - ``value``: meaning
   ```

   No underline. No `Args:`. Types in parentheses on the same line as the name.
8. `BitField` `doc=` is one sentence. Use `When '1', ...` for flags. Use a short code list inside the string when the field selects modes. Use `debug` for bring-up status bits. Mention sticky and "minus one" in that sentence.
9. Inline and VHDL comments explain why, with the arithmetic visible (`3000e6/16384 = 183105 frames = 1 s`). Comment a constraint on the declaration (`works only with N = 8`, clock domain, synchronous reset). Do not comment what the next statement obviously does.
10. RST entity pages use Overview, autoentity, Parameters, Interface, Implementation details, Memory map. Implementation details describe a constraint or a tradeoff in a few sentences. Do not walk states, pipeline stages, or mux trees that are already in the source. Do not add a Features or Limitations section to restate the overview.
11. RST howtos: the command, then what the user now has, then the failure mode if the step is expensive to undo. Include the real numbers (timeouts, netmasks, rates).
12. Cross-references: ``double backticks`` for code, registers, and values. A Sphinx role only when the reader should jump somewhere (`:meth:`, `:class:`, `:doc:`).
13. Do not add a `History:` block.
14. Do not open with "This module provides", "In summary", or a restatement of the section heading.
15. If the code and an existing docstring disagree, fix the docstring to match the code and say so. Do not document a parameter that the signature does not have.
16. Keep hardware caveats that are already written (board revision lists, switch-specific delays, "do not change this constant, it is hardcoded below"). Those are the documentation.

## Recommendations

These are optional. They keep the same density and voice.

1. **Stop the code walkthroughs in entity RST.** `ucap.rst` and `scaler.rst` Implementation sections currently narrate processes the VHDL already shows. Keep the parts that are not obvious from a read: the frame/channel budget, why a base-period change needs a reset, FIFO `prog_full` versus pipeline depth, packet header bit fields (those are a wire format, not an implementation diary). Drop the numbered "for each source, the process…" sequences.
2. **Treat a mismatched docstring as a bug.** `UCAP.set_data_capture` documents a `select` argument it does not take. `open_mmi_async` documents `udp_retries`, `fpga_ip_addr_fn`, and `interface_ip_addr`, which are no longer parameters. When editing a function, update the parameter list in the same change. A short docstring that matches the signature is better than a long one that describes an older API.
3. **One convention for "minus one" and for frames.** Several periods are stored as N−1 (`INTEGRATION_PERIOD`, UCAP `periods`). Say "frames, minus one" in the `BitField` doc and do not repeat a second definition in every caller. Point callers at that field or at one method.
4. **Howtos: keep the old operational tone.** `quick_start.rst` is the voice (what you type, what is hacky, what bricks a board). Newer pages such as `hardware_setup.rst` are a bit smoother and that is fine, as long as they stay a checklist with numbers rather than a feature tour. When cleaning typos, fix the ones that send someone to the wrong supply or the wrong command; leave the rest.
5. **Comments on disabled code should say when the check becomes valid again.** The FFT pipeline-delay note does this. The same one-liner is worth adding wherever a check is commented out "for now".
6. **Do not document `debug` bits in the RST memory-map prose.** The table already says they exist. Extra text there hides the bits that matter (`overrun`, `fifo_overflow_sticky`, `readout_delay`).
