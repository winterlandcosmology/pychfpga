# F-Engine / FUNCGEN — firmware test inventory and HDL coverage

This note ties the **pytest** suite in `firmware_tests/funcgen_test.py` to the **RTL** in the chFPGA tree (`chfpga/hdl/chan/funcgen.vhd`) and the **Python register model** (`pychfpga/.../f_engine/funcgen.py`). It is meant for planning and review: **full register/interface coverage is not claimed**; several gaps and RTL–Python mismatches are called out explicitly.

**Primary references**

| Artifact | Path |
|----------|------|
| FUNCGEN RTL | `chfpga/hdl/chan/funcgen.vhd` |
| Channel pipeline (instantiation) | `chfpga/hdl/chan/chan.vhd` |
| Python MMI | `pychfpga/pychfpga/fpga_firmware/chfpga/f_engine/funcgen.py` |
| Board / host tests | `pychfpga/firmware_tests/funcgen_test.py` |

---

## HDL interfaces

From the entity in `funcgen.vhd`:

| Interface | Role |
|-----------|------|
| `s_axis_adc_*` | AXI-Stream **from ADCDAQ**: sample words, per-sample overflow in `tuser`, framing in `tlast`. Used in **FN_ADC** (`fn_sel = "000"`). |
| `m_axis_adc_*` | AXI-Stream **to FFT**: same sample packing; `tuser` is **1 bit**, valid on the last word of the frame — **frame-level** overflow aggregate. |
| `s_axis_bsb_*` / `m_axis_bsb_*` / `bsb_clk` / `bsb_rst` | Byte-stream **register + RAM** access (`bsb_regs` + waveform RAM). |

Operational **modes** (`fn_sel`, 3 bits): `FN_ADC`, `FN_RAM`, `FN_NOISE`, `FN_FRAME_WORD_CTR_RAM_FLAGS`, `FN_FRAME8`, `FN_FRAME4`, `FN_RAM_NIBBLE4`, and **`FN_UNUSED` ("111")** (ties off data path in the `case`).

---

## Memory-mapped map (RTL vs Python)

The authoritative bit layout is the comment block in `funcgen.vhd` (approximately lines 265–291). The Python `BitField` names are the MMI view of the same bytes.

### Control bytes (FUNCGEN → host writes)

| RTL / byte | Field | Python (`funcgen.py`) | Notes |
|------------|--------|----------------------|--------|
| 0 | `soft_reset` | `RESET` | Pulse reset. |
| 0 | `enable` | `ENABLE` | Frame generation / `send_frame` alignment. |
| 0 | `ram_page[2:0]` | `RAM_PAGE_LSB` | Waveform RAM paging (512 B pages). |
| 0 | `fn_sel[2:0]` | `FUNCTION` | Mode select. |
| 1 | `byte_a` | `BYTE_A` | LFSR seed MS half; also used when **`IMPLEMENT_DELAY_CAPTURE`** is true for delay-measurement gating. |
| 2 | `byte_b` | `BYTE_B` | LFSR seed LS half. |
| 3–4 | `stats_period` | `STATS_PERIOD` | **16-bit** `overflow_ctr_period`: number of **completed frames** after which `overflow_ctr_reg` is latched, counter cleared, and `stats_done` asserted (see stats FSM in RTL). **Not** “number of frames to send” — the Python docstring on `STATS_PERIOD` is misleading. |
| 5 | `ram_page[6:3]` | `RAM_PAGE_MSB` | RAM page MSBs. |
| 5 | `right_shifting[3:0]` | `RIGHT_SHIFT` | Arithmetic right shift **after** left shift / saturation. |
| 6 | `stats_enable` | `STATS_ENABLE` | Enables accumulation into `overflow_ctr`; when cleared, `stats_done` is cleared (CDC to datapath clock). |
| 6 | `rst_lfsr` | `RST_LFSR` | LFSR reseed path. |
| 6 | **5:4 unused** | — | **No separate programmable “clip width”** in current RTL; comment in VHDL explicitly marks these bits unused. |
| 6 | `left_shifting[3:0]` | `LEFT_SHIFT` | Left shift **with saturation** (see below), then right shift. |
| 7 | `function_number` | `FN_ID` | Software-only tag; **does not** change the waveform in hardware. |

### Status bytes (host reads)

| RTL / byte | Field | Python | Notes |
|------------|--------|--------|--------|
| 0 | `word_ctr_pre2[7:0]` | `RAMP_CTR` | Low 8 bits of word counter (debug). |
| 1 | `frame_ctr[7:0]` | `FRAME_CTR` | Low 8 bits of frame counter. |
| 2 | `frame_overflow_flag` | `FRAME_OVERFLOW_FLAG` | Sticky / pipeline of frame overflow into `m_axis_adc_tuser`. |
| 2 | `stats_done` | `STATS_DONE` | New `OVERFLOW_CTR` snapshot ready. |
| 2 | `send_frame` | `SEND_FRAME` | Debug: aligned with `enable` on frame boundaries. |
| 3–4 | `delay_capture_ctr` | `DELAY_CTR` | Only meaningful if **`IMPLEMENT_DELAY_CAPTURE`** is true. **`chan.vhd` does not set this generic** → entity default **`FALSE`** → delay-capture process is **not generated**; `delay_capture_ctr` may be **undriven** in RTL (implementation-dependent). `test_delay_capture` should be interpreted cautiously. |
| 5–8 | `overflow_ctr_reg` | `OVERFLOW_CTR` | Latched sum of **per-sample overflow flags** (`out_dat_shifted_overflows`) over `stats_period` frames. RTL comment says “saturated samples”; the implementation counts **ones on the per-sample overflow vector**, which includes **ADC/RAM overflow bits** and **left-shift saturation**. Python doc (“ADC overflows only”) is **too narrow**. |

---

## Register verification and test traceability

Each row ties a **Python MMI name** (`funcgen.py`) to **how you prove the register works** on hardware (or what is still missing). **Covered** means an existing test in `firmware_tests/funcgen_test.py` asserts or necessarily relies on that behavior. Host-only tests (`test_udp_buffers_size`, `test_mtu_size`, `test_packets_number`) are omitted here—they do not exercise FUNCGEN registers.

**Legend:** **Y** = covered by listed test(s); **Partial** = used indirectly or read without strong assertion; **N** = no dedicated check (add test or sim).

### Control registers

| Register | Map (byte : bits) | How to verify it works | Covered? | Test name(s) |
|----------|-------------------|-------------------------|----------|----------------|
| `RESET` | 0 : 7 (`soft_reset`) | Pulse reset; pipeline stops or re-arms; key status fields behave per RTL after reset. | Y | `test_reset` (`FUNCGEN.reset()`) |
| `ENABLE` | 0 : 6 | After reset, read `SEND_FRAME` vs `ENABLE` (aligned on frame boundary per RTL). | Y | `test_reset` |
| `RAM_PAGE_LSB` | 0 : 5–3 | Select RAM page; full-frame buffer write/read via `set_buffer` / capture must be consistent across pages. | Partial | All tests that call `set_buffer` / `set_channelizer` with buffer data (implicit paging); **no** isolated page-boundary assertion |
| `FUNCTION` | 0 : 2–0 (`fn_sel`) | Program each `fn_sel` mode; captured data or counters must match mode-specific RTL. | Y | `FUNCTION` exercised per mode: `test_funcgen_*` patterns (`FN_RAM`), noise suite (`FN_NOISE`), `test_word_and_frame_ctr` (`word_ctr_buffer_flags`), `test_frame_counter` (`frame8`/`frame4`), `test_frame_counter_nibble` (`nibble4`), `test_overflow_ctr` (inject modes); **`FN_ADC` not isolated** |
| `BYTE_A` | 1 : 7–0 | Program LFSR seed MS byte; noise sequence / statistics should change with seed. | Y | `test_funcgen_random_pval`, `test_funcgen_noise_*`, `test_funcgen_deterministic` (via `set_channelizer(..., seed=...)` → `FUNCGEN.noise()`); also delay-capture gating in RTL if generic enabled (**not verified** on typical `chan` build) |
| `BYTE_B` | 2 : 7–0 | Program LFSR seed LS byte; same as `BYTE_A`. | Y | (same as `BYTE_A`) |
| `STATS_PERIOD` | 3–4 : 16-bit | Set `overflow_ctr_period`; after `STATS_DONE`, `OVERFLOW_CTR` must match expected total (parameterized: `stats_frames × expected_overflows_per_frame` when aligned with test’s `STATS_PERIOD = stats_frames - 1` vs RTL). | Y | `test_overflow_ctr` |
| `RAM_PAGE_MSB` | 5 : 7–4 | With `RAM_PAGE_LSB`, address correct 512 B page for buffer I/O. | Partial | Same as `RAM_PAGE_LSB` |
| `RIGHT_SHIFT` | 5 : 3–0 | Program shift; captured samples match arithmetic right shift of post-saturation value. | Y | `test_right_shift`, `test_shift_clip_width` |
| `STATS_ENABLE` | 6 : 7 | When 1, stats accumulate and `STATS_DONE` latches; when 0, `stats_done` cleared per RTL (CDC). | Partial | `test_overflow_ctr` (enables, waits `STATS_DONE`); **de-assert / re-arm sequencing not tested** |
| `RST_LFSR` | 6 : 6 | Pulse resets LFSR state; sequence restarts from seed. | Y | `test_funcgen_deterministic` (`reset_noise()`); other noise tests use seeds |
| *reserved* | 6 : 5–4 | Must be ignored by RTL (unused). | N | No write-pattern test |
| `LEFT_SHIFT` | 6 : 3–0 | Program shift; output matches left shift with **RTL saturation rule**, then optional right shift. | Partial | `test_shift_clip_width` (reference model may diverge from `all_same` path); `test_overflow_ctr` / counter tests often force **0** |
| `FN_ID` | 7 : 7–0 | Software tag only—read back matches last `set_data_source` ID; **no DUT waveform change**. | Partial | Written by all `set_channelizer` calls; **no** test asserts readback vs expected ID |

### Status registers

| Register | Map (byte : bits) | How to verify it works | Covered? | Test name(s) |
|----------|-------------------|-------------------------|----------|----------------|
| `RAMP_CTR` | 0 : 7–0 | Low bits of internal word counter; optional cross-check vs decoded stream / timing. | Partial | Visible during tests; **not** asserted against golden counter sequence |
| `FRAME_CTR` | 1 : 7–0 | Low bits of frame counter; cross-check vs `frame8`/`frame4`/`word_ctr_buffer_flags` captures. | Partial | Implied by `test_frame_counter`, `test_word_and_frame_ctr` on **captured data**; **direct readback of `FRAME_CTR` not asserted** |
| `FRAME_OVERFLOW_FLAG` | 2 : 2 | Sticky / pipeline of per-frame overflow; compare to packet/capture `tuser` on last word. | N | — |
| `STATS_DONE` | 2 : 1 | Asserts after full stats integration when enabled; poll until set before reading `OVERFLOW_CTR`. | Y | `test_overflow_ctr` (wait loop) |
| `SEND_FRAME` | 2 : 0 | Tracks `enable` alignment; compare to `ENABLE` after reset. | Y | `test_reset` |
| `DELAY_CTR` | 3–4 : 16-bit | Delay-measurement counter when `IMPLEMENT_DELAY_CAPTURE` true; compare across channels. | Partial | `test_delay_capture` (**may be meaningless** if generic false in `chan.vhd`) |
| `OVERFLOW_CTR` | 5–8 : 32-bit | After `STATS_DONE`, latched sum matches injected + saturated + ADC overflows over period. | Y | `test_overflow_ctr`; `test_reset` asserts **0** after reset path |

---

## Logic paths and failure-mode coverage (DFMEA-style)

This table is **not** a formal FMEDA; it lists **functional paths**, **what can go wrong**, **how to observe it**, and **which existing tests** (if any) target that risk. Add rows as the RTL evolves.

| Functionality / concern | Failure mode or path | Detection / test approach | Covered? | Test name(s) |
|-------------------------|----------------------|---------------------------|----------|----------------|
| **Mode: `FN_RAM` buffer playback** | Wrong RAM packing, page, or endian → bad waveform | Compare capture to known buffer | Y | `test_funcgen_ramp`, `test_funcgen_sin`, `test_funcgen_arb`, `test_funcgen_const`, `test_funcgen_ab`, `test_funcgen_real_ramp`, `test_right_shift`, `test_shift_clip_width` |
| **Mode: `FN_NOISE` / LFSR** | Wrong polynomial, seed, or CRS vs Ice cadence | Statistical / determinism checks | Y | `test_funcgen_deterministic`, `test_funcgen_random_pval`, `test_funcgen_noise_histogram`, `test_funcgen_noise_min_max`, `test_funcgen_noise_psd`, `test_funcgen_noise_fft_avg_squared` |
| **Mode: `FN_ADC` passthrough** | ADC not forwarded; `tuser` not merged to frame `tuser` | Known ADC pattern + overflow flags | N | — |
| **Mode: `word_ctr_buffer_flags`** | CRS vs Ice 32-bit packing wrong | Decode words vs expected frame/word count | Y | `test_word_and_frame_ctr` |
| **Mode: `frame8` / `frame4`** | Frame ID bits wrong place or width | Decode per-sample vs expected sequence | Y | `test_frame_counter` |
| **Mode: `nibble4`** | Even/odd frame nibble swap wrong | Constant buffer; decode MS nibble per frame | Y | `test_frame_counter_nibble` |
| **Mode: `FN_UNUSED`** | Spurious decode if selected | Formal / sim only | N | — |
| **Mode switching** | Glitch or stale RAM on `fn_sel` change | Ordered mode A → B captures | Partial | Tests usually set mode once per case; **no** dedicated rapid switch test |
| **Left shift + saturation** | `all_same` / saturate bug; wrong overflow flag | Golden vectors vs RTL | Partial | `test_shift_clip_width` (model risk) |
| **Right shift (attenuation)** | Wrong arithmetic shift | Known buffer, compare capture | Y | `test_right_shift`, `test_shift_clip_width` |
| **Per-sample overflow → frame `tuser`** | OR-reduction or TLAST alignment wrong | Capture `tuser` last word of frame | N | — (see `FRAME_OVERFLOW_FLAG`) |
| **RAM LSB overflow inject** (`frame8`/`frame4`/`word_ctr_buffer_flags`) | Inject path not wired; wrong bit | `STATS_PERIOD` + known inject count | Y | `test_overflow_ctr` |
| **Stats integration / latch** | Wrong period, early latch, CDC glitch | Poll `STATS_DONE`; math on `OVERFLOW_CTR` | Partial | `test_overflow_ctr` covers happy path; **no** `STATS_ENABLE` toggle, multi-period, or `stats_period` edge tests |
| **Left-shift saturation in stats count** | Saturated samples not counted in `OVERFLOW_CTR` | Combine inject mode + `LEFT_SHIFT` | N | — |
| **`FN_RAM` / `FN_NOISE` stats baseline** | Spurious counts with no inject | Enable stats, expect 0 (unless ADC/sat) | N | — |
| **Soft reset (`RESET`)** | Counters / enable state wrong after pulse | Read `SEND_FRAME`, `ENABLE`, `OVERFLOW_CTR` | Partial | `test_reset`; **full pipeline flush not fully enumerated** |
| **Enable / frame alignment** | `send_frame` not synced to `enable` | Same as `test_reset` assertion | Y | `test_reset` |
| **Word / frame counter free-run** | Counters stuck or slip | Long capture decode | Partial | Embedded in counter tests; **not** explicit `RAMP_CTR`/`FRAME_CTR` readback test |
| **Delay capture** | Counter not implemented or not counting | Cross-channel `DELAY_CTR` match | Partial | `test_delay_capture` (hardware may not implement) |
| **BSB / register CDC** (`stats_enable`, `enable`) | Metastability or torn read | Directed sim or repeated toggle tests | N | — |
| **Multi-channel consistency** | Per-channel FUNCGEN diverges | Same config all chans; compare captures or status | Partial | `test_overflow_ctr`, `test_delay_capture` touch all channels; **not** exhaustive mismatch injection |
| **Host UDP / MTU** | Lost packets → false FUNCGEN failures | sysctl / MTU checks | Y | `test_udp_buffers_size`, `test_mtu_size`, `test_packets_number` |

**Related:** `firmware_tests/scaler_test.py` uses FUNCGEN (e.g. inject patterns, `BYTE_A`/`BYTE_B` in some scenarios) for **SCALER** validation—not duplicated in the tables above. Extend this doc with a cross-link if scaler/FUNCGEN interactions become a signoff item.

---

## Left shift and saturation (RTL vs `test_shift_clip_width`)

In `funcgen.vhd` (PHASE 4), left shift uses a **sign-consistency check** via `all_same` (`chfpga/hdl/chfpga_package.vhd`) before `shift_left`. If the value cannot be shifted without overflowing the signed `ADC_BITS_PER_SAMPLE` range, the sample is **saturated** to a specific bit pattern and the corresponding **per-sample overflow** bit is set.

Important points for test maintenance:

- There is **no** dedicated **clip-width** register: control byte 6 bits **5:4 are unused**. The pytest name **`test_shift_clip_width`** and its parameter `clip_width` are **historical**: the test only programs **`LEFT_SHIFT` / `RIGHT_SHIFT`**; `clip_width` is just the **left-shift amount** used in that scenario.
- The test’s expected values use a **naive** model: `value << left` then **clamp** to signed min/max, then arithmetic `>> right`. The RTL uses a **different** saturation rule (overflow detection via `all_same` then alternate assignment). **Corner cases may disagree** with the Python reference — if the test fails after an RTL change, **update the reference model to match `funcgen.vhd`**, not the other way around, unless the RTL change was a bug.

An **`int_clip_width`** variable is **declared** in the main process of `funcgen.vhd` but **never used** — likely leftover from an older clip implementation; safe to remove in a cleanup PR.

---

## Overflow statistics (expanded in RTL)

The stats block (PHASE 5) **accumulates** `num_ones(out_dat_shifted_overflows)` on every valid beat while enabled, rolls through **`overflow_frame_ctr`** vs **`overflow_ctr_period`**, latches **`overflow_ctr_reg`**, clears running **`overflow_ctr`**, and sets **`stats_done`**. **`stats_done` is cleared when `stats_enable` is low** (resync’d to the datapath clock).

### What gets counted

`out_dat_shifted_overflows` is the **per-sample overflow vector** after the left-shift / saturation stage. A **1** on a given sample’s flag increments the running stats sum for that clock beat. Sources include:

- **Per-sample ADC overflows** on the **`FN_ADC`** path (input `tuser`).
- **Left-shift saturation** (PHASE 4 sets the flag when saturation triggers).
- **RAM-driven “injected” overflows** in the dedicated test modes below.

### RAM modes that inject overflows (for bench / stats validation)

RTL ties **per-sample overflow bits** from the **waveform RAM** when `use_ram_overflow` is true (`funcgen.vhd`): for each sample index, **`out_dat_overflows(i) <= ram_dout(ADC_BITS_PER_SAMPLE*i)`** — i.e. **bit 0 of each packed sample in RAM** is interpreted as that sample’s overflow flag (documented in RTL header: set sample value **1** to simulate an overflow).

This applies only to these **`fn_sel`** modes:

| Mode | RTL name | Role |
|------|-----------|------|
| `word_ctr_buffer_flags` | `FN_FRAME_WORD_CTR_RAM_FLAGS` | 32 LSBs carry frame/word counters; **overflow flags from RAM** sample LSBs. |
| `frame8` | `FN_FRAME8` | Frame counter in samples; **overflow flags from RAM** LSBs. |
| `frame4` | `FN_FRAME4` | Frame counter in upper nibble; **overflow flags from RAM** LSBs. |

**`FN_RAM`**, **`FN_NOISE`**, and **`FN_RAM_NIBBLE4`** do **not** use that path — RTL keeps injected flags off, so stats integration over those modes counts **only** real ADC overflows (ADC mode) or **left-shift saturation** overflows, not buffer LSB injection.

In Python, the **`overflow_buffer`** argument to `set_channelizer` / `set_data_source` programs the buffer used for injection alongside the counter/test pattern in the three RAM-overflow modes (`funcgen_test.test_overflow_ctr` uses `overflow_buffer=injected_data`).

### Existing pytest

- **`test_overflow_ctr`** — Parameterized over **`word_ctr_buffer_flags`**, **`frame8`**, and **`frame4`**: builds an **`overflow_buffer`** with **`expected_overflows_per_frame`** samples having LSB **1**, sets **`STATS_PERIOD = stats_frames - 1`** (matches RTL **`overflow_ctr_period`**), waits for **`STATS_DONE`**, asserts **`OVERFLOW_CTR == stats_frames * expected_overflows_per_frame`**.

### Stats testing — suggested additions (not yet in repo)

| Scenario | Why it matters |
|----------|----------------|
| **`STATS_ENABLE` de-assert before `STATS_DONE`** | RTL clears **`stats_done`** when **`stats_enable`** goes low; verify no partial latch / counter freeze glitches across CDC. |
| **Second integration period** | After first **`STATS_DONE`**, arm again and confirm **`OVERFLOW_CTR`** updates to a **new** total (latch + clear + re-accumulate). |
| **`stats_period` boundaries** | e.g. **0**, **1**, **max** (65535): define expected latch behavior and document RTL corner cases. |
| **Left-shift saturation + RAM injection** | Same integration window: use **`frame8`/`frame4`/`word_ctr_buffer_flags`** **`overflow_buffer`** **plus** **`LEFT_SHIFT > 0`** and amplitudes that **saturate** — expect counts to include **both** injected LSB flags **and** saturation flags per sample. |
| **`FN_ADC` + stats** | Real or known ADC stream with **`tuser`** asserts; compare **`OVERFLOW_CTR`** to hand-calculated per-frame overflow counts over **`stats_period`**. |
| **`FN_RAM` + stats** | Confirm counter stays **0** (or only reflects left-shift overflows if shift programmed) when no ADC/RAM injection path applies. |
| **Concurrent multi-channel** | `test_overflow_ctr` already loops all channels; extend with **mismatched** per-channel injection if the design should stay independent. |

---

## Pytest catalog (`funcgen_test.py`)

The module-wide `pytestmark = pytest.mark.funcgen` applies to all tests below.

### Host / transport prerequisites

- **`test_udp_buffers_size`** — Fails if Linux UDP buffer sysctls are below the documented minimum for reliable capture.
- **`test_mtu_size`** — Requires MTU ≥ 9000 on the interface used toward the board.
- **`test_packets_number`** — Short capture; asserts all expected packets received (not FUNCGEN-specific but gates the suite).

### Waveform / `FN_RAM` path (internal sources + buffer)

Exercised with FFT/SCALER bypass and capture at FUNCGEN output (`source='funcgen'`).

- **`test_funcgen_ramp`** — `ramp` buffer pattern vs expected samples.
- **`test_funcgen_sin`** — `sin` vs reference.
- **`test_funcgen_arb`** — arbitrary buffer (all ones) vs capture.
- **`test_funcgen_const`** — constant `a` mode.
- **`test_funcgen_ab`** — alternating `a`/`b` bytes.
- **`test_funcgen_real_ramp`** — `real_ramp` I/Q-style pattern.

*Note:* Many other **named buffer functions** exist in `funcgen.py` (`cos`, `4bit_ramp`, `freq_test`, …). They still use **`FN_RAM`** in hardware; separate tests are optional if those **patterns** matter for a given program — they do not add new `fn_sel` values.

### `FN_NOISE` / LFSR

- **`test_funcgen_deterministic`** — After `reset_noise`, consecutive frames should not be identical (uses ADC capture path for streaming).
- **`test_funcgen_random_pval`** — NIST-style monobit over many seeds.
- **`test_funcgen_noise_histogram`**, **`test_funcgen_noise_min_max`**, **`test_funcgen_noise_psd`**, **`test_funcgen_noise_fft_avg_squared`** — Distributional / spectral sanity vs Python references (not bit-exact RTL proofs).

### Counter / test modes (`FN_FRAME_WORD_CTR_RAM_FLAGS`, `FN_FRAME8`, `FN_FRAME4`, `FN_RAM_NIBBLE4`)

- **`test_word_and_frame_ctr`** — Decodes **`word_ctr_buffer_flags`**; **CRS vs IceBoard** packing differs (test branches on `PLATFORM`).
- **`test_frame_counter`** — **`frame8`** / **`frame4`** frame-id embedding vs expected sequence (parameterized `mode`).
- **`test_frame_counter_nibble`** — **`nibble4`** low/high nibble alternation per frame.

### Reset, delay, shifting, stats

- **`test_reset`** — After buffer playback and **`FUNCGEN.reset()`**, checks **`SEND_FRAME` vs `ENABLE`** and **`OVERFLOW_CTR == 0`** (see test for precise assertions; note **`RAMP_CTR`/`FRAME_CTR`** may be non-zero immediately after reset by design).
- **`test_delay_capture`** — All channels’ **`DELAY_CTR`** equal; see **§ Status** re **`IMPLEMENT_DELAY_CAPTURE`** default **false**.
- **`test_right_shift`** — **`RIGHT_SHIFT`** only, **`FN_RAM`**, several signed inject values.
- **`test_shift_clip_width`** — **`LEFT_SHIFT` + `RIGHT_SHIFT`** with saturation reference model (**may need realignment to RTL**; see above).
- **`test_overflow_ctr`** — **`STATS_ENABLE` / `STATS_PERIOD` / `STATS_DONE`** and **`OVERFLOW_CTR`**; uses **`overflow_buffer`** in **`word_ctr_buffer_flags`**, **`frame8`**, and **`frame4`** so RAM sample **LSB=1** injects per-sample overflows (parameterized counts and modes).

---

## Coverage matrix (concise)

**Coverage status**

- **Covered** — Existing pytest meaningfully validates the feature for regression.
- **Partial** — Some exercise via current tests; important cases or RTL paths still missing or fragile.
- **Missing** — No adequate automated test; add new cases (or simulation-only check).

| RTL item | Covered by tests (primary) | Coverage status | Gap / comment |
|----------|-----------------------------|-----------------|---------------|
| `FN_ADC` | — | **Missing** | No dedicated pytest; passthrough, per-sample `tuser` → frame `tuser`. Needs known ADC stimulus or sim-backed check. |
| `FN_RAM` | ramp, sin, arb, const, ab, real_ramp, shift tests | **Covered** | Core buffer path OK. Many **named** Python buffer helpers untested individually (same `fn_sel` — optional). |
| `FN_NOISE` | noise suite, deterministic | **Covered** | LFSR **period** differs Ice vs CRS per RTL — document platform in test metadata if needed. |
| `FN_FRAME_WORD_CTR_RAM_FLAGS` | `test_word_and_frame_ctr`, `test_overflow_ctr` | **Covered** | Counter decode + **RAM LSB overflow injection** for stats. |
| `FN_FRAME8` / `FN_FRAME4` | `test_frame_counter`, `test_overflow_ctr` | **Covered** | Encoding tests + **overflow_buffer** stats path. |
| `FN_RAM_NIBBLE4` | `test_frame_counter_nibble` | **Covered** | No RAM-overflow injection in RTL for this mode (flags forced off). |
| `FN_UNUSED` | — | **Missing** | Should stay unreachable; optional formal/sim assert only. |
| `LEFT_SHIFT` / saturation | `test_shift_clip_width` | **Partial** | **Reference model may not match** `all_same` + saturation RTL; extend with RTL-accurate golden vectors. |
| `RIGHT_SHIFT` | `test_right_shift`, `test_shift_clip_width` | **Covered** | Arithmetic shift after left path. |
| Stats: latch / `STATS_DONE` / `OVERFLOW_CTR` | `test_overflow_ctr` (RAM inject modes) | **Partial** | **RAM modes** (`word_ctr_buffer_flags`, `frame8`, `frame4`) well used for inject; **`stats_enable`** sequencing, **multi-period**, **period=0/edge**, **FN_ADC**, **FN_RAM-only**, and **shift+saturate+inject** combos **need tests** (see § Overflow statistics). |
| `RESET` / `ENABLE` / `SEND_FRAME` | `test_reset` | **Partial** | Soft reset / post-reset registers checked; **ENABLE** stop/start without full reset not exhaustive. |
| `RAM_PAGE` / `write_ram` | implicit via `set_buffer` | **Partial** | No explicit **page walk** or wrong-page regression. |
| `FN_ID` | indirect (`set_data_source`) | **Covered** (SW-only) | No RTL effect; tracking-only — “coverage” is API smoke, not hardware. |
| `FRAME_OVERFLOW_FLAG` | — | **Missing** | Not read in pytest; compare to capture **`tuser`** on last word of frame. |
| `RAMP_CTR` / `FRAME_CTR` | indirect / debug | **Partial** | Not systematically cross-checked vs decoded capture streams. |
| `RST_LFSR` | `reset_noise` in deterministic test | **Covered** | |
| `DELAY_CTR` | `test_delay_capture` | **Partial** | **`IMPLEMENT_DELAY_CAPTURE`** default **false** in `chan` — feature may be inactive; consider skip or TB-only. |
| BSB register timing / CDC | implicit | **Missing** | No explicit test for `stats_enable` / `enable` CDC races; add directed cases or sim. |

---

## Recommended follow-ups

1. **Reconcile `test_shift_clip_width`** with the **exact** saturation equations in `funcgen.vhd` (and add a short comment in the test pointing to the `all_same` check).
2. **Fix Python docstrings** for **`STATS_PERIOD`** and **`OVERFLOW_CTR`** to match RTL.
3. Add a focused **`FN_ADC`** test (known ADC or injected sim) + **`FRAME_OVERFLOW_FLAG`** / capture **`tuser`** check.
4. Decide fate of **`test_delay_capture`** vs **`IMPLEMENT_DELAY_CAPTURE`** (enable generic on a test build, or skip test when delay hardware absent).
5. **Expand stats pytest** using existing **RAM inject modes** (`word_ctr_buffer_flags`, `frame8`, `frame4`) for: second integration period, **`stats_enable`** handshake, **`stats_period`** edges, and **combined** inject + **`LEFT_SHIFT`** saturation counts; add separate **`FN_ADC`** / **`FN_RAM`** stats baselines as in the table above.

---

## Revision

- Document generated from repository state; RTL line numbers refer to `chfpga` and `pychfpga` as sibling checkouts consistent with this workspace.
