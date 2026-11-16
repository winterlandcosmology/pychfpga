# shuffle256 lane-ID payload: what each fibre should carry

This page is for checking the ICE crate's output fibres from the receiving end.
In lane-ID mode, every packet a crate sends carries a fixed payload that names
the board slot and output lane it came from. Every frame is identical, and no
two lanes in the crate carry the same payload.

## Turning it on and off

- **On:** run `.venv/bin/python deployment_scripts/ct_lane_id_payload.py --hwm 'bp16 <serial>:0'`.
  - It brings the crate up in shuffle256, switches every board to the lane-ID payload, and prints the table below from the live crate.
  - The crate keeps sending the payload after the script exits.
  - `--table-only` prints the table without touching hardware.
- **Off:** bring the crate up with `prog=2`, which forces the FPGAs to be reprogrammed.
  - A bring-up that skips reprogramming leaves the FFT bypassed: board init resets the function generator and the scaler, but not the FFT bypass bit.

## What changes in the crate

- Only the first stages of every channel change:
  - the FFT and the scaler are bypassed;
  - each channel's function generator replays a fixed buffer instead of the ADC data.
- All 256 inputs of the crate get identical buffers.
- The shuffle256 corner turn is **not** bypassed. It runs exactly as in production, so the packet format, frequency map, rates and Ethernet framing are the production ones.
- What this checks: routing from bin to lane to fibre. It cannot reveal two source boards swapped inside a bin, because all inputs carry the same data. The firmware test `test_corner_turn_shuffle256` covers that case.

## The payload rule

Every data byte of output lane `k` of the board in backplane slot `s` (0-15) is

    byte = (s << 4) | (k + 1)        read in hex as the digits  s, k+1

- **High hex digit:** the backplane slot 0-F, counted from 0 as in `IceBoard((0, s))`.
- **Low hex digit:** the lane + 1, i.e. 1-8.
  - 1-4 = lanes 0-3, which go out on QSFP A.
  - 5-8 = lanes 4-7, which go out on QSFP B.
- **No lane ever carries 0x00 or 0xFF**, so a zero-filled or missing receive buffer can't pass for a valid payload.

**Decoding the same byte as a 4+4-bit complex sample:**
- The high nibble is the real part and the low nibble the imaginary part, both two's complement. Offset binary is not applied in this mode.
- Real = slot, with slots 8-15 reading as −8..−1.
- Imaginary = lane + 1, with lane 7 reading as −8.

## Expected packet on every lane

These are the current crate defaults: 2 frames per packet, flags on. Each packet is 1189 32-bit words, a 4756-byte UDP payload. The IP packet is 4784 bytes, so the receiving interface needs jumbo frames.

Each 32-bit word is sent least-significant byte first.

| Words | Content | Bytes on the wire |
|---|---|---|
| 0 | `stream_id << 16 \| 0x14CF`, with `stream_id = 0x2000 \| crate << 8 \| s << 4 \| k` | `CF 14 {s k} {20 + crate}` |
| 1 | `0x42000000` | `00 00 00 42` |
| 2 | frame number, bits 47:32 | `00 00 00 00` for the first ~3 h after the crate starts |
| 3 | frame number, bits 31:0; goes up by 2 on each packet | changes every packet |
| 4-1027 | data: 2 frames × 8 frequency bins × 256 inputs, one byte each | 4096 × the lane byte |
| 1028-1155 | per-bin data flags | all `00` |
| 1156-1187 | per-frame flags | all `00` |
| 1188 | packet-flags trailer: nonzero means an error inside the crate | `00 00 00 00` |

Only words 2-3 (the frame number) change from packet to packet. The rest of a lane's packets is byte-for-byte identical, frame after frame. The UDP checksum is not filled in.

**Example: fibre 64, the second lane in the cage** (slot 5, QSFP A, lane 1) starts as:

    CF 14 51 20  00 00 00 42  00 00 00 00  <frame number>  52 52 52 52 ...

## Lookup table

- **Crate number:** 0, as in `hwm 'bp16 12:0'`.
- **Payload bytes:** listed in lane order. Each byte fills the whole data section of that lane.
- **Stream IDs:** in header word 0. They name the same slot and lane as the payload byte, but in their low digit the lane is counted from 0, not 1.

| Fibre | Slot | QSFP | Lanes | Payload byte per lane (hex, lane order) | Stream IDs |
|---|---|---|---|---|---|
| 01 | 6 | A | 0-3 | 61 62 63 64 | 2060-2063 |
| 02 | 3 | B | 4-7 | 35 36 37 38 | 2034-2037 |
| 03 | 4 | B | 4-7 | 45 46 47 48 | 2044-2047 |
| 04 | 13 | A | 0-3 | D1 D2 D3 D4 | 20D0-20D3 |
| 05 | 9 | A | 0-3 | 91 92 93 94 | 2090-2093 |
| 06 | 10 | A | 0-3 | A1 A2 A3 A4 | 20A0-20A3 |
| 13 | 5 | B | 4-7 | 55 56 57 58 | 2054-2057 |
| 14 | 12 | A | 0-3 | C1 C2 C3 C4 | 20C0-20C3 |
| 15 | 0 | A | 0-3 | 01 02 03 04 | 2000-2003 |
| 17 | 2 | B | 4-7 | 25 26 27 28 | 2024-2027 |
| 20 | 14 | B | 4-7 | E5 E6 E7 E8 | 20E4-20E7 |
| 30 | 14 | A | 0-3 | E1 E2 E3 E4 | 20E0-20E3 |
| 32 | 9 | B | 4-7 | 95 96 97 98 | 2094-2097 |
| 33 | 15 | B | 4-7 | F5 F6 F7 F8 | 20F4-20F7 |
| 36 | 8 | A | 0-3 | 81 82 83 84 | 2080-2083 |
| 43 | 0 | B | 4-7 | 05 06 07 08 | 2004-2007 |
| 45 | 11 | A | 0-3 | B1 B2 B3 B4 | 20B0-20B3 |
| 46 | 13 | B | 4-7 | D5 D6 D7 D8 | 20D4-20D7 |
| 47 | 2 | A | 0-3 | 21 22 23 24 | 2020-2023 |
| 49 | 7 | A | 0-3 | 71 72 73 74 | 2070-2073 |
| 50 | 7 | B | 4-7 | 75 76 77 78 | 2074-2077 |
| 52 | 3 | A | 0-3 | 31 32 33 34 | 2030-2033 |
| 53 | 1 | B | 4-7 | 15 16 17 18 | 2014-2017 |
| 54 | 10 | B | 4-7 | A5 A6 A7 A8 | 20A4-20A7 |
| 57 | 1 | A | 0-3 | 11 12 13 14 | 2010-2013 |
| 58 | 6 | B | 4-7 | 65 66 67 68 | 2064-2067 |
| 59 | 11 | B | 4-7 | B5 B6 B7 B8 | 20B4-20B7 |
| 60 | 15 | A | 0-3 | F1 F2 F3 F4 | 20F0-20F3 |
| 61 | 8 | B | 4-7 | 85 86 87 88 | 2084-2087 |
| 62 | 12 | B | 4-7 | C5 C6 C7 C8 | 20C4-20C7 |
| 63 | 4 | A | 0-3 | 41 42 43 44 | 2040-2043 |
| 64 | 5 | A | 0-3 | 51 52 53 54 | 2050-2053 |

**If the cabling changes:** edit `FIBRE_MAP` in `deployment_scripts/ct_lane_id_payload.py` and regenerate the table with `--table-only`. The payload itself does not depend on the cabling.

## What is confirmed and what is still open

**Confirmed from the firmware source (chfpgalite):**
- **Cages:** lanes 0-3 are on QSFP A and lanes 4-7 on QSFP B (`hdl/platforms/ice/constraints/ice_physical_constraints_ct.xdc`).
- **Byte order:** words are sent least-significant byte first.
- **Word 2** holds the high bits of the frame number.
- **Flags:** in this mode the function generator forces the ADC-overflow flag to 0, and the scaler bypass does not apply offset binary.

**Still open:**
- **Lane order inside a cage:** which QSFP TX lane (1-4) carries which of the cage's four lanes is not in the firmware source. It needs the board schematic. The low digit of the payload byte answers it on the first capture.
- **Bitstream version:** the deployed `sep24` bitstream was built from a firmware version newer than the source checked. Its documented changes (per-lane output strobes, the packet-flags trailer, capture-probe arming) don't touch anything on this page.
