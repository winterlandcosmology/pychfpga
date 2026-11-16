#!/usr/bin/env python3
"""Bring an ICE crate up in shuffle256 and make every output fibre send its lane-ID payload.

Every channel's FFT and SCALER are bypassed and fed identical function
generator buffers, so every data byte of corner-turn output lane k of the
board in 0-based backplane slot s equals lane_id_byte(s, k) = 0x{s}{k+1}, on
every frame. The corner turn itself runs as in production. The script prints
the per-fibre lookup table of the expected payload; the receiver-side details
are in docs/shuffle256_lane_id_payload.md.

The crate keeps sending this payload after the script exits. To restore sky
data, bring the crate up with prog=2: the FPGAs must be reprogrammed, because
board init resets the function generator and SCALER but not the FFT bypass.

Run with the pychfpga environment:

    .venv/bin/python deployment_scripts/ct_lane_id_payload.py               # crate 'bp16 12:0'
    .venv/bin/python deployment_scripts/ct_lane_id_payload.py --hwm 'bp16 29:0'
    .venv/bin/python deployment_scripts/ct_lane_id_payload.py --table-only  # no hardware
"""
import argparse
import logging
import sys
import time

from pychfpga.fpga_firmware.chfpga.chfpga_array_methods import lane_id_byte

# Current fibre cabling: (0-based backplane slot, QSFP cage) -> fibre number
FIBRE_MAP = {
    (0, 'A'): 15, (0, 'B'): 43,
    (1, 'A'): 57, (1, 'B'): 53,
    (2, 'A'): 47, (2, 'B'): 17,
    (3, 'A'): 52, (3, 'B'): 2,
    (4, 'A'): 63, (4, 'B'): 3,
    (5, 'A'): 64, (5, 'B'): 13,
    (6, 'A'): 1, (6, 'B'): 58,
    (7, 'A'): 49, (7, 'B'): 50,
    (8, 'A'): 36, (8, 'B'): 61,
    (9, 'A'): 5, (9, 'B'): 32,
    (10, 'A'): 6, (10, 'B'): 54,
    (11, 'A'): 45, (11, 'B'): 59,
    (12, 'A'): 14, (12, 'B'): 62,
    (13, 'A'): 4, (13, 'B'): 46,
    (14, 'A'): 30, (14, 'B'): 20,
    (15, 'A'): 60, (15, 'B'): 33,
}

# Corner-turn output lanes of each QSFP cage, from the GTX pin constraints
# (chfpgalite hdl/platforms/ice/constraints/ice_physical_constraints_ct.xdc).
# Which QSFP TX lane (1-4) carries which of a cage's lanes is not confirmed;
# the low digit of the payload byte identifies the lane on the receiving end.
QSFP_LANES = {'A': (0, 1, 2, 3), 'B': (4, 5, 6, 7)}


def fibre_table(lane_bytes, crate):
    """Return the markdown lookup table: fibre number -> expected payload byte of each lane."""
    def stream_id(slot, lane):
        return 0x2000 | (crate << 8) | (slot << 4) | lane

    lines = ['| Fibre | Slot | QSFP | Lanes | Payload byte per lane (hex, lane order) | Stream IDs |',
             '|---|---|---|---|---|---|']
    for (slot, cage), fibre in sorted(FIBRE_MAP.items(), key=lambda item: item[1]):
        lanes = QSFP_LANES[cage]
        payload = ' '.join(f'{lane_bytes[(crate, slot, lane)]:02X}' for lane in lanes)
        lines.append(f'| {fibre:02d} | {slot} | {cage} | {lanes[0]}-{lanes[-1]} | {payload} | '
                     f'{stream_id(slot, lanes[0]):04X}-{stream_id(slot, lanes[-1]):04X} |')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--hwm', default='bp16 12:0',
                        help="crate hardware map, 'bp16 <backplane serial>:<crate number>'")
    parser.add_argument('--prog', type=int, default=1,
                        help='0: never reprogram, 1: reprogram if outdated, 2: always reprogram')
    parser.add_argument('--max-sync-time-difference', type=float, default=100,
                        help='limit in ns for the array-wide sync after programming the payload')
    parser.add_argument('--table-only', action='store_true',
                        help='print the lookup table for crate number 0 without touching any hardware')
    args = parser.parse_args()

    if args.table_only:
        crate = 0
        lane_bytes = {(crate, slot, lane): lane_id_byte(slot, lane) for slot in range(16) for lane in range(8)}
        print(fibre_table(lane_bytes, crate))
        return

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    from pychfpga.fpga_array import FPGAArray

    ca = FPGAArray(hwm=args.hwm, mode='shuffle256', prog=args.prog, open=1, mdns_timeout=40)
    lane_bytes = ca.set_lane_id_payload(max_sync_time_difference=args.max_sync_time_difference)
    crates = {crate for crate, slot, lane in lane_bytes}
    if len(crates) != 1:
        raise RuntimeError(f'Expected the lanes of a single crate, got crate numbers {sorted(crates)}')

    before = {mb.slot: int(mb.GPU.FRAME_CTR) for mb in ca.ib}
    time.sleep(1)
    after = {mb.slot: int(mb.GPU.FRAME_CTR) for mb in ca.ib}
    static = [slot for slot in before if before[slot] == after[slot]]
    print('\n===== GPU link transmit check =====')
    print(f'GPU FRAME_CTR advancing on {len(before) - len(static)}/{len(before)} boards'
          + (f'; STATIC on slots {static}' if static else ''))
    print('\n===== Expected payload per fibre (every data byte of each lane) =====')
    print(fibre_table(lane_bytes, crates.pop()))
    print('\nThe crate keeps sending the lane-ID payload; a bring-up with prog=2 restores sky data.')


if __name__ == '__main__':
    main()
