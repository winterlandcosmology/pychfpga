""" MDNS hardware discovery
"""

# Standard library
import logging
import functools  # Used in iceboard discovery
import time  # Used in iceboard discovery
import socket  # used in iceboard discovery (itoa())
import asyncio
import threading

# PyPI packages
from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

# Local packages
from . import IceBoard, IceCrate

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _get_txt_field(tr, key):
    value = tr.get(key.encode('ascii'), None)
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    return value

def match(target, value):
    return target == '*' or target == value

class ThreadData:
    def __init__(self,**kwargs):
        self._lock = threading.RLock()
        self.__dict__.update(kwargs)
    def __enter__(self):
        self._lock.acquire()
        return self
    def __exit__(self, type, value, traceback):
        self._lock.release()

# How many time the longest delay betwen mdns replies do we wait until we
# call it quits?
AUTO_TIMEOUT_DELAY_FACTOR = 5

async def mdns_discover(
    icecrates=None,
    iceboards=None,
    timeout=None,
    auto_timeout=10,
    clear_hwm=False):
    """ Automatically detect IceBoards and IceCrates on the network using mDNS
    and add them to the hardware map.

    Parameters:

        icecrates: If ``icecrates`` is specified,  all the IceBoards that
            are on crates having the model number and serial number listed in
            ``icecrates`` are selected.

            ''icecrates''   can be in the format

                [(model1, [serial1, serial2 ...]), (model2, [serial3, serial4, ...]), ...]

                [(model1, serial1), (model2, serial2), ...]

                [serial1, serial2, ...] # will match any crate model

                "*" # will match any crate


            If ``icecrates`` is None or an empty list, no crate is added.

            If ``icecrates='*'``, all discovered Iceboards from all crates are added.

        iceboards:  If ``iceboards`` is specified,  all the IceBoards with the
            serial number found in the ``iceboards`` list are selected. If
            None or an empty list, no board is added. If ``iceboards='*'``,
            all discovered Iceboards are added.


        auto_timeout (int or float): If non-zero, mDNS search will stop when the delay
            since the last reply  exceeds either :
               1) AUTO_TIMEOUT_DELAY_FACTOR times the longest delay between replies so far
               2) `auto_timeout`



    """
    logger = logging.getLogger(__name__)

    if clear_hwm:
        IceBoard.clear_hardware_map()

    # Normalize iceboard and icecrate target lists to the [ (model,[serial1, serial2]), ...] format
    if iceboards == '*':
        iceboards = [('*', '*')]
    # if isinstance(iceboards, str): # include '*'
    #     iceboards = [('*', [iceboards])]
    # iceboards = [entry if isinstance(entry, (list, tuple)) else ('*', [entry]) for entry in iceboards or []]
    # iceboards = [(model, serials if isinstance(serials, (list, tuple)) else [serials]) for model, serials in iceboards]

    # if isinstance(icecrates, str): # include '*'
    #     icecrates = [('*', [icecrates])]
    if icecrates == '*':
        icecrates = [('*', '*')]
    # icecrates = [entry if isinstance(entry, (list, tuple)) else ('*', [entry]) for entry in icecrates or []]
    # icecrates = [(model, serials if isinstance(serials, (list, tuple)) else [serials]) for model, serials in icecrates]

    print(f'looking for ib={iceboards}, ic={icecrates}')
    t0 = time.time()
    time_info = ThreadData(t0=t0, last_time=t0, dt_max=0, n=0)

    def on_service_state_change(zeroconf: Zeroconf, service_type: str,
                                name: str, state_change: ServiceStateChange,
                                time_info=time_info) -> None:
        # print("Service %s of type %s state changed: %s" % (name, service_type, state_change))

        if state_change is not ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name)
        # print("Info from zeroconf.get_service_info: %r" % (info))
        if not info:
            return
        addr = socket.inet_ntop(socket.AF_INET, info.addresses_by_version(version=IPVersion.V4Only)[0])
        port = info.port
        # print(f"  Address: {addr}:{port}")
        # print("  Weight: %d, priority: %d" % (info.weight, info.priority))
        # print("  Server: %s" % (info.server,))
        tr = info.properties

        ib_serial = _get_txt_field(tr, 'motherboard-serial')
        ib_part_number = _get_txt_field(tr, 'motherboard-part')
        bp_slot = _get_txt_field(tr, 'backplane-slot')
        bp_part_number = _get_txt_field(tr, 'backplane-part')
        bp_serial = _get_txt_field(tr, 'backplane-serial')
        # find the integer representation of the serial number if possible, in case the user specified them that way
        int_bp_serial = _to_int(bp_serial)
        int_ib_serial = _to_int(ib_serial)
        slot = _to_int(bp_slot)

        logger.debug(f"DNS-SD: Discovered IceBoard SN{ib_serial} ({addr}) in "
                     f"IceCrate {bp_part_number} SN{bp_serial}  slot {bp_slot}.")

        # Check if the motherboard matches the search criteria
        iceboard_match = ib_part_number and ib_serial and any(
            match(target_model, ib_part_number)
            and (match(target_serial, ib_serial) or match(target_serial, int_ib_serial))
            for target_model, target_serial in iceboards)

        # Check if the backplane matches the search criteria
        icecrate_match = bp_part_number and bp_serial and any(
            match(target_model, bp_part_number)
            and (match(target_serial, bp_serial) or match(target_serial, int_bp_serial))
            for target_model, target_serial in icecrates)

        if icecrate_match or iceboard_match:
            if ib_part_number and ib_serial:
                # ib_cls = IceBoard.get_class_by_ipmi_part_number(ib_part_number)
                ib_obj = IceBoard.get_unique_instance(serial=ib_serial, hostname=addr)
                # print(f'ib obj {ib_obj} has hostnme {ib_obj.hostname}')

            # Add the backplane if it does not already exist
            if bp_part_number and bp_serial:
                bp_cls = IceCrate.get_class_by_ipmi_part_number(bp_part_number)
                crate_number = ib_obj.crate.crate_number if ib_obj.crate else None
                bp_obj = bp_cls.get_unique_instance(new_class=bp_cls, serial=bp_serial, crate_number =crate_number)
                ib_obj.update_instance(crate=bp_obj, slot=slot)
        else:
            logger.debug(
                f"DNS-SD: IceBoard SN{ib_serial} (crate {bp_part_number} SN{bp_serial} slot {bp_slot}) was detected "
                f"but was not added because it did not match the IceBoard serial {iceboards} "
                f"or crate serial {icecrates}")
        t = time.time()
        with time_info as ti:
            ti.dt_max = max(ti.dt_max, t - ti.last_time)
            ti.last_time = t
            ti.n += 1
    try:
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        browser = ServiceBrowser(
            zeroconf,
            '_tuber-jsonrpc._tcp.local.',
            handlers=[on_service_state_change])
        while True:
            t = time.time()
            with time_info as ti:
                print(f'elapsed={t-t0:.1f}, elapsed since last time={t-ti.last_time:.1f}, dt_max={ti.dt_max}, n={ti.n}, last_time={ti.last_time}')
                if (timeout and t - t0 > timeout):
                    break
                if auto_timeout and ti.n and t-ti.last_time > auto_timeout:
                    break
                # if (auto_timeout and ti.n > 1 and ti.dt_max and (t - ti.last_time > AUTO_TIMEOUT_DELAY_FACTOR * ti.dt_max)):
                #     break
            await asyncio.sleep(.1)
    finally:
        zeroconf.close()
    return IceBoard.get_all_instances(), IceCrate.get_all_instances()


def test():
    logging.basicConfig(level=logging.DEBUG)
    IceBoard.clear_hardware_map()
    ibs, ics = asyncio.run(mdns_discover(iceboards='*', icecrates='*', timeout=5))
    for ib in sorted(ibs, key=lambda ib:ib.slot):
        if ib.crate:
            crate_txt = f' in slot {ib.slot:2d} of crate {ib.crate.part_number}_SN{ib.crate.serial}'
        else:
            crate_txt = f' (standalone board in virtual slot {ib.slot})'
        print(f'IceBoard {ib.part_number}_SN{ib.serial} @ {ib.hostname}' + crate_txt)