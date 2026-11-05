# Backplane QSFP I2C: expander address and pin map

## Problem

On the 4-slot CRS backplane (4SBP), the QSFP ports P5A, P5B, P6A and P6B are
controlled by the I/O expander U11 (PCAL6524). U11 drives ModSelL, ResetL and
LPMode, and reads IntL and ModPrsL. The board in slot 1 reaches U11 on I2C0 (the
backplane bus).

U11 did not answer. The board could not select a backplane QSFP module, and
could not read one.

Hardware probe from slot 1 (2026-10-23):

| I2C0 address | Device         | Result  |
|--------------|----------------|---------|
| 0x51         | slot EEPROM    | answers |
| 0x4E         | TMP421         | answers |
| 0x44         | U11 (old code) | timeout |
| 0x50         | QSFP module    | timeout |

## Causes

1. Wrong address. The backplane schematic gives "0b0100 010X (0x44)". X is the
   R/W bit, so 0x44 is the 8-bit form. The 7-bit address is 0x22. The pychfpga
   I2C API uses 7-bit addresses (TMP421 0x4E, EEPROM 0x51 on the same bus).
2. No pin map. `i2c0_qsfp` received the raw expander as its GPIO object. The
   QSFP class needs a GPIO table with ModSelL, ResetL, IntL, ModPrsL and LPMode.
3. Pins stay inputs. PCAL6524 pins power up as inputs. `pca6524.write()` set
   only the output register, so a write did not drive the pin.

`BP_RESETN` (U11 RESET) is not a cause. Neither the board nor the backplane
drives it: the board pulls it up (R206, 4.7 kΩ) and uses it as an input
(U26 EN1). The backplane pulls it up too (R130, 5 kΩ). Do not drive it low:
on the board it goes to the reset supervisor.

## Fix

- `crs.py`: `i2c0_bp_gpio` at 0x22.
- `CRS.BP_QSFP_PINS`, `CRS.bp_qsfp_gpio_table()`: U11 pin map (below).
- `i2c0_bp_qsfp`: one QSFP object per connector, `{'P5A': QSFP, ...}`. It
  replaces `i2c0_qsfp`. All modules are at 0x50; ModSelL selects one.
- `pca6524.write()`: sets the output register, then makes the written bits
  outputs. Fixed register key `POL2` (was a second `POL1`).
- Test: `tests/bp_qsfp_gpio_tests.py` (fake I2C bus, no hardware).

The code does no I2C access when the board opens. U11 is on slot 1 only, and an
access to an absent device times out (10 s).

## U11 pin map

| Connector | Data lanes        | ModSelL | ResetL | IntL | ModPrsL | LPMode |
|-----------|-------------------|---------|--------|------|---------|--------|
| P5A       | to backplane BD1  | P1.4    | P1.5   | P1.6 | P1.7    | P2.3   |
| P5B       | to backplane BD2  | P0.0    | P0.1   | P0.2 | P0.3    | P2.0   |
| P6A       | to backplane BD3  | P0.4    | P0.5   | P0.6 | P0.7    | P2.1   |
| P6B       | none (sideband only) | P1.0 | P1.1   | P1.2 | P1.3    | P2.2   |

Each data port carries one lane of each slot: Tx1/Rx1 slot 4, Tx2 slot 3,
Tx3 slot 2, Tx4 slot 1. P6B has no data lanes: a loopback plug in P6B loops nothing.

Default state (4.7 kΩ pull-ups, U11 not configured): all modules deselected, out
of reset, LPMode on (1.5 W limit). An optical module in LPMode can stay dark.

## Use

```python
ib = ca.ib[0]                      # the board in slot 1
q = ib.i2c0_bp_qsfp['P5A']
q.is_present()                     # ModPrsL
q.read('VendName')                 # ModSelL low for the access, then high
q.set_control_bit('LPMode', 0)     # high-power mode (optical loopbacks)
```

## Backplane QSFP TX drive

The backplane QSFP lanes (GTY 7, 9, 11) power up with the firmware's TX drive
fields at 0/0/0 and nothing in the link bring-up sets them. The corner-turn
engine init now writes one setting to those lanes on every board: swing field 13
(GTY code 27), pre-cursor 6, post-cursor 8, receivers in DFE (`BP_QSFP_TX` in
`xxvglink.py`). That is the copper optimum from the 2026-10-26 grids on crate
SN008: 33 of 33 DAC and loopback-plug legs clean in 10 s, and again 24 of 24 on
a four-backplane layout. The dashboard's Map and Sweep pages start from the same
values.

Optical modules are not supported on these ports: the two sets of SR4 modules
tested wanted different, strong de-emphasis and no single setting was clean on
all of them. The dashboard boot (corr modes) reads every backplane port through
U11 and logs a warning when a port holds anything but copper
(`check_bp_qsfp_copper`); the media comes from SFF-8636 bytes 130 and 147.

## Status

Hardware 2026-10-26, crate SN008: U11 at 0x22 answers from slots 1, 5, 9, 13;
every port's module reads through ModSelL; LPMode writes work; DACs read
connector 0x23 / technology 0xA0, loopback plugs 0x00 / 0x80, SR4 modules
0x0C / 0x00. Copper legs clean at 13/6/8 on all four backplanes.
