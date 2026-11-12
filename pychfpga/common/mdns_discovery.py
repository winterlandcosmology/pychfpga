""" MDNS hardware discovery
"""

# Standard library
import logging
import functools  # Used in iceboard discovery
import time  # Used in iceboard discovery
import socket  # used in iceboard discovery (itoa())
import asyncio
import threading # Used for Rlock

# PyPI packages
from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf, AsyncServiceInfo

# Local packages
from pychfpga.hardware import Motherboard, Crate


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


def match(target, value, case_sensitive=True):
    return target == '*' or ((target==value) if case_sensitive else (target.upper() == value.upper()))

def match_int(target, value):
    """ Return True if target is the wildcard character or if the value match the target when both are converted to integers. Returns False if both cannot be coonverted to integers.
    """
    try:
        return target == '*' or int(target) == int(value)
    except (TypeError, ValueError):
        return False

def tuple_match(target, value, case_sensitive=True):
    return (match(target[0], value[0], case_sensitive=case_sensitive)
            and (match(target[1], value[1], case_sensitive=case_sensitive)
                 or match_int(target[1], value[1])))


class ThreadData:
    def __init__(self, **kwargs):
        self._lock = threading.RLock()
        self.__dict__.update(kwargs)

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, type, value, traceback):
        self._lock.release()


async def mdns_discover(
        crates=None,
        boards=None,
        timeout=None,
        inter_reply_timeout=None,
        clear_hwm=False,
        case_sensitive=False,
        retries=2):
    """ Automatically detect IceBoards and IceCrates on the network using mDNS
    and add them to the hardware map.

    Parameters:

        crates (list): List of crates whose boards we want to add to the hardware map. It can contain:

                [(model1, serial1), (model2, serial2), ...]: Matches specific
                model and serial number. model can be "*" to match any
                mmodel. serial can be "*" to match any serial. serial can be
                a string or an integer.

                "*" is equivalent to [('*', '*')]  : matches any crate model
                and serial, i.e. all discovered boards from all crates
                are added.

                None or an empty list: no boards are added on a crate
                membership basis.


        boards (list):  List of boards we want to add to the hardware map.

                [(model1, serial1), (model2, serial2), ...]: Matches specific
                model and serial number. model can be "*" to match any
                mmodel. serial can be "*" to match any serial. serial can be
                a string or an integer.

                "*" is equivalent to [('*', '*')]  : matches any board model
                and serial, i.e. all discovered boards from all crates
                are added.

                None or an empty list: no boards are added on a crate
                membership basis.

        timeout (float): Maximum amount of time (in seconds) to wait for mDNS replies on each attempt.

        inter_reply_timeout (int or float): If non-zero, the current attempt
            stops when no reply has arrived for `inter_reply_timeout` seconds
            (measured from the start of the attempt if no reply has been
            received yet, otherwise from the last reply). Two use cases:

                - Shorten the search once the expected burst of replies has
                  arrived (set it slightly larger than the inter-reply
                  spacing within the burst).

                - Give up fast on a dropped query so the caller's retry loop
                  can launch a fresh attempt instead of waiting the full
                  `timeout` (set it to ~2-3x the expected first-reply
                  latency).

            The attempt still stops after `timeout` regardless. Disabled if
            `inter_reply_timeout` is `None`.

        case_sensitive (bool): If true, motherboard and crate model names matching will be case sensitive.

        retries (int): Number of times to retry the mDNS discovery if not all
            specifically-requested boards/crates were found. Each retry uses a
            fresh ``AsyncZeroconf`` instance. Wildcard targets (``'*'``) do not
            trigger retries because they have no completion criterion.
            Discovered boards from previous attempts are kept (``get_unique_instance``
            is idempotent), so a retry only needs to find the still-missing ones.
            The total wall-clock cost is at most ``(retries + 1) * timeout``.

    """
    # from pychfpga.hardware import Motherboard, Crate

    logger = logging.getLogger(__name__)

    if clear_hwm:
        Motherboard.clear_hardware_map()

    boards = boards or []
    crates = crates or []
    # Normalize board and crate target lists to the [ (model,[serial1, serial2]), ...] format
    if boards == '*':
        boards = [('*', '*')]
    if crates == '*':
        crates = [('*', '*')]
    # List of explicitely-specified (non-wildcard) boards and crates
    expected_boards = {(model, serial): None for model, serial in boards if model != "*" and serial != "*"}
    expected_crates = {(model, serial): None for model, serial in crates if model != "*" and serial != "*"}
    # Check if we expect an open-ended number of boards or crates
    wild_boards = [(model, serial) for model, serial in boards if model == "*" or serial == "*"]
    wild_crates = [(model, serial) for model, serial in crates if model == "*" or serial == "*"]

    logger.debug(f'mdns_discover: looking for boards={boards}, crates={crates}')

    async def _attempt(attempt_idx):
        """Run one mDNS discovery attempt with a fresh AsyncZeroconf/browser.

        Updates ``expected_boards`` / ``expected_crates`` from the enclosing scope
        as services are discovered. Returns True if all explicitly-requested
        boards/crates have been seen by the time the attempt ends.
        """
        t0 = time.time()
        time_info = ThreadData(t0=t0, last_time=t0, dt_max=0, n=0)

        # Track in-flight per-service handler tasks so we can drain them at
        # shutdown. Without this, repeated calls to mdns_discover() on a
        # long-lived event loop can leak tasks that race against zeroconf
        # teardown and eventually break discovery.
        #
        # We keep two collections per attempt:
        #   - pending_tasks: tasks still running. Drained (cancelled+awaited)
        #     in the per-attempt `finally`.
        #   - finished_tasks: tasks already done. We still keep references to
        #     them so we can retrieve any exception in `finally` and avoid
        #     "Task exception was never retrieved" warnings, which under
        #     Python 3.12 + nest_asyncio (Jupyter Lab) are more aggressive
        #     about being routed through the loop's exception handler and
        #     have been observed to interact badly with subsequent
        #     mdns_discover() calls on the same loop.
        pending_tasks: set[asyncio.Task] = set()
        finished_tasks: list[asyncio.Task] = []
        shutting_down = [False]  # mutable wrapper so closures can flip it

        def _on_task_done(task: asyncio.Task) -> None:
            pending_tasks.discard(task)
            finished_tasks.append(task)

        def on_service_state_change(zeroconf: Zeroconf,
                                    service_type: str,
                                    name: str,
                                    state_change: ServiceStateChange) -> None:
            if state_change is not ServiceStateChange.Added:
                return
            if shutting_down[0]:
                return
            task = asyncio.ensure_future(async_on_service_state_change(zeroconf, service_type, name))
            pending_tasks.add(task)
            task.add_done_callback(_on_task_done)

        async def async_on_service_state_change(zeroconf: Zeroconf,
                                                service_type: str,
                                                name: str) -> None:
            try:
                info = AsyncServiceInfo(service_type, name)
                await info.async_request(zeroconf, 3000)
                if not info:
                    return
                ipv4_addrs = info.parsed_addresses(version=IPVersion.V4Only)
                # Bail out if we dont have at least one IP address: mdns_discover
                # guarantees that the discovered boards will have one
                if not ipv4_addrs:
                    logger.warning(f'mdns_discover: ignored service with no IPV4 address. info={info}')
                    return
                addr = ipv4_addrs[0]
                port = info.port

                tr = info.properties
                ib_serial = _get_txt_field(tr, 'motherboard-serial')
                ib_part_number = _get_txt_field(tr, 'motherboard-part')
                bp_slot = _get_txt_field(tr, 'backplane-slot')
                bp_part_number = _get_txt_field(tr, 'backplane-part')
                bp_serial = _get_txt_field(tr, 'backplane-serial')

                # find the integer representation of the serial number if possible, in case the user specified them that way
                slot = _to_int(bp_slot)

                logger.debug(f"mdns_discover: Discovered motherboard {ib_part_number} SN{ib_serial} @{addr}:{port}"
                             + (f" in crate {bp_part_number} SN{bp_serial} slot {bp_slot}." if bp_part_number else " (no crate info)"))

                board_match = ib_part_number and ib_serial and any(
                    match(target_model, ib_part_number, case_sensitive=case_sensitive)
                    and (match(target_serial, ib_serial, case_sensitive=case_sensitive) or match_int(target_serial, ib_serial))
                    for target_model, target_serial in boards)

                crate_match = bp_part_number and bp_serial and any(
                    match(target_model, bp_part_number, case_sensitive=case_sensitive)
                    and (match(target_serial, bp_serial, case_sensitive=case_sensitive) or match_int(target_serial, bp_serial))
                    for target_model, target_serial in crates)

                if crate_match or board_match:
                    if ib_part_number and ib_serial:

                        ib_cls = Motherboard.get_class_by_ipmi_part_number(ib_part_number if case_sensitive else ib_part_number.upper())

                        if not ib_cls:
                            known_part_numbers = ','.join(Motherboard.get_supported_ipmi_part_numbers())
                            raise RuntimeError(f'mdns discover: cannot find a class for motherboard with part number {ib_part_number}. Supported part numbers are {known_part_numbers}.')
                        ib_obj = ib_cls.get_unique_instance(serial=ib_serial, hostname=addr)
                        for tib in expected_boards:
                            if tuple_match(tib, (ib_part_number, ib_serial), case_sensitive=case_sensitive):
                                expected_boards[tib] = True
                    if bp_part_number and bp_serial:
                        bp_cls = Crate.get_class_by_ipmi_part_number(bp_part_number)
                        crate_number = ib_obj.get_crate_number()
                        bp_obj = bp_cls.get_unique_instance(new_class=bp_cls, serial=bp_serial, crate_number=crate_number)
                        ib_obj.update_instance(crate=bp_obj, slot=slot)
                        for tib in expected_crates:
                            if tuple_match(tib, (bp_part_number, bp_serial), case_sensitive=case_sensitive):
                                expected_crates[tib] = bp_obj
                else:
                    logger.debug(
                        f"mdns_discover: Motherboard {ib_part_number} SN{ib_serial} "
                        f"(crate {bp_part_number} SN{bp_serial} slot {bp_slot}) was detected "
                        f"but was not added because it did not match the motherboard serial {boards} "
                        f"or crate serial {crates}")
                t = time.time()
                with time_info as ti:
                    ti.dt_max = max(ti.dt_max, t - ti.last_time)
                    ti.last_time = t
                    ti.n += 1

            except Exception as e:
                logger.error(f'mdns_discover: The following error occured during service discovery:\n{e}')
                raise

        zeroconf = None
        browser = None
        try:
            zeroconf = AsyncZeroconf(
                ip_version=IPVersion.V4Only,
                # Standard multicast replies. unicast=True (requesting unicast
                # replies) was tried to avoid ICMP errors on replies, but on some
                # hosts it finds no boards at all. Multicast replies do.
                unicast=False
                )
            # `_tcpipe._tcp.local.` is no longer registered by the firmware,
            # so we only browse for the JSON-RPC service.
            browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                ['_tuber-jsonrpc._tcp.local.'],
                handlers=[on_service_state_change],
                delay=100  # ms
                )
            last_msg_time = None
            while True:
                t = time.time()
                found_all_boards = not expected_boards or all(expected_boards.values())
                found_all_slots = not expected_crates or all(ic and len(ic.slot) == ic.NUMBER_OF_SLOTS for ic in expected_crates.values())
                if not wild_boards and not wild_crates and found_all_boards and found_all_slots:
                    logger.info('mdns_discover: All the boards and/or crates that were requested were found. Stopping the search')
                    break
                with time_info as ti:
                    if last_msg_time is None or t - last_msg_time > 1:
                        logger.debug(f'mdns_discover: searching (attempt {attempt_idx + 1}): elapsed={t - t0:.1f}, elapsed since last reply={t - ti.last_time:.1f}, dt_max={ti.dt_max:.1f}, n={ti.n}')
                        last_msg_time = t
                    if (timeout and t - t0 > timeout):
                        break
                    if inter_reply_timeout and t - ti.last_time > inter_reply_timeout:
                        # `ti.last_time` is initialised to the attempt start
                        # time, so this also short-circuits the case where
                        # no reply ever arrives (typically a dropped first
                        # query): bail after `inter_reply_timeout` seconds
                        # of silence so the caller's retry loop can fire a
                        # fresh attempt instead of waiting the full
                        # `timeout`.
                        break
                await asyncio.sleep(.1)

        except BaseException as e:
            logger.error(f'mdns_discover: got the exception {e}')
            raise

        finally:
            logger.debug(f'mdns_discover: closing zeroconf (attempt {attempt_idx + 1})')
            # Block any new handler tasks from being spawned by late callbacks
            # from the browser before we tear down zeroconf.
            shutting_down[0] = True
            if browser is not None:
                try:
                    await browser.async_cancel()
                except Exception as e:
                    logger.warning(f'mdns_discover: error while cancelling browser: {e}')
            # Drain any handler tasks that are still in flight before closing
            # zeroconf, otherwise they keep running against a closing client
            # and accumulate from one call to the next.
            if pending_tasks:
                still_pending = list(pending_tasks)
                for t in still_pending:
                    t.cancel()
                try:
                    await asyncio.gather(*still_pending, return_exceptions=True)
                except Exception as e:
                    logger.warning(f'mdns_discover: error while draining handler tasks: {e}')
                pending_tasks.clear()
            # Retrieve exceptions from already-completed handler tasks so they
            # don't get logged by the loop's default "Task exception was never
            # retrieved" handler. Under Python 3.12 with nest_asyncio (Jupyter
            # Lab) we have observed that orphan exceptions from a previous
            # mdns_discover() call can interfere with the next call's discovery.
            for t in finished_tasks:
                if t.cancelled():
                    continue
                try:
                    exc = t.exception()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    continue
                if exc is not None:
                    logger.warning(f'mdns_discover: handler task ended with exception: {exc!r}')
            finished_tasks.clear()
            if zeroconf is not None:
                try:
                    await zeroconf.async_close()
                except Exception as e:
                    logger.warning(f'mdns_discover: error while closing zeroconf: {e}')

        # Did this attempt satisfy all explicitly-requested targets? Wildcard
        # targets ('*') do not have a completion criterion, so they don't
        # block "all expected found".
        all_boards_found = not expected_boards or all(expected_boards.values())
        all_crates_found = not expected_crates or all(
            ic and len(ic.slot) == ic.NUMBER_OF_SLOTS for ic in expected_crates.values())
        return all_boards_found and all_crates_found

    # Outer retry loop. We retry only when there are explicit (non-wildcard)
    # targets that haven't all been found yet, since wildcard searches have
    # no completion criterion and would always retry.
    has_explicit_targets = bool(expected_boards) or bool(expected_crates)
    for attempt_idx in range(retries + 1):
        all_found = await _attempt(attempt_idx)
        if all_found or not has_explicit_targets:
            break
        if attempt_idx < retries:
            missing_boards = [tib for tib, v in expected_boards.items() if not v]
            missing_crates = [tic for tic, v in expected_crates.items()
                           if not (v and len(v.slot) == v.NUMBER_OF_SLOTS)]
            logger.warning(
                f'mdns_discover: attempt {attempt_idx + 1}/{retries + 1} did not find '
                f'all expected targets (missing boards={missing_boards}, '
                f'missing crates={missing_crates}); retrying.')


def mdns_resolve(name, timeout=1):
    """
    Return the IP address for the specified name by issuing a mDNS query directly.

    Parameters:

        name (str): Host name to lookup. Normally ends with ".local" or
            ".local.". Thhe '.local' will be added automatically if missing.

        timeout (float): time to wait for an answer, in seconds. The function will return
            as soon as there is an answer. mDNS devices are not obligated to
            respond more than once a second, so setting a value less than 1
            might cause a timeout if the device had just been queried by some
            other system.

    Returns:
        IPV4 address as a str; None if not found

    """
    if name.lower().endswith('local'):
        name += '.'
    if not name.lower().endswith('.local.'):
        name += '.local.'
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        info = zeroconf.get_service_info('.local.', name, timeout=timeout * 1000)
    finally:
        zeroconf.close()
    if info:
        ips = info.addresses_by_version(version=IPVersion.V4Only)
        if ips:
            return socket.inet_ntop(socket.AF_INET, ips[0])
    else:
        return None

def test():
    # from pychfpga.hardware import Motherboard

    logging.basicConfig(level=logging.DEBUG)
    Motherboard.clear_hardware_map()
    ibs, ics = asyncio.run(mdns_discover(boards='*', crates='*', timeout=5))
    for ib in sorted(ibs, key=lambda ib:ib.slot):
        if ib.crate:
            crate_txt = f' in slot {ib.slot:2d} of crate {ib.crate.part_number}_SN{ib.crate.serial}'
        else:
            crate_txt = f' (standalone board in virtual slot {ib.slot})'
        print(f'Motherboard {ib.part_number}_SN{ib.serial} @ {ib.hostname}' + crate_txt)