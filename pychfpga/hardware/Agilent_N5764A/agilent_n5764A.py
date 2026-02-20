"""Object for the Agilent N5764A power supply.
"""

import time

from .agilent_N5700 import agilent_N5700


class AgilentN5764A:
    """
    Provide the basic methods to operate an Agilent N5700-series power supply.

    Available commands:
    | Command      | Action                                                                                                                                                           | Example                 |
    | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
    | unlock       | Unlocks the access to configuring the PS                                                                                                                         | ps.unlock()             |
    | lock         | Locks the access to configuring the PS                                                                                                                           | ps.lock()               |
    | status       | Shows current config and status                                                                                                                                  | ps.status()             |
    | is_enabled   |                                                                                                                                                                  | ps.is_enabled()         |
    | is_ok        | True: power supply is turned on and operates normally<br>False: can either be turned off, faulted or in current limit mode                                       | ps.is_ok()              |
    | power_enable | Enables or disables power output. The only argument value can be:<br>'on', 'On', 'ON', 1, True – Enables power<br>'off', 'Off', 'OFF', 0, False – Disables power | ps.power_enable(False)  |
    | power_on     | Shortcut to ps.power_enable(True)                                                                                                                                | ps.power_on()           |
    | power_off    | Shortcut to ps.power_enable(False)                                                                                                                               | ps.power_off()          |
    | power_cycle  | Runs power_off and then power_on after a delay in seconds                                                                                                        | ps.power_cycle(delay=4) |
    """

    hostname = None

    def __init__(self, hostname=None, port=5025, timeout=0.5,  **kwargs):
        """
        Create an empty AgilentN5764 object. Communication with the power supply is not established yet (see `open()`)

        Arguments:
            hostname (str): the hostname or IP address of the power supply
        """
        super().__init__(**kwargs)
        self.hostname = hostname
        self.port = port
        self.timeout = timeout
        self.locked = True
        self.ps = None  # The actual power supply object is created only during open() to defer actual communications with the unit.

    def open(self):
        if self.ps:
            raise RuntimeError('Power supply is already opened()')
        self.ps = agilent_N5700(interface='lan', ip_addr=self.hostname, ip_port=self.port, timeout=self.timeout, verbose=0)

    def __repr__(self):
        if self.ps:
            return '%s %s @%s' % (self.ps.instrument_name, self.ps.instrument_model, self.hostname)
        else:
            return 'Unknown power supply'

    def lock(self):
        self.locked = True

    def unlock(self):
        self.locked = False

    def _check_lock(self):
        if self.locked:
            raise RuntimeError('Instrument is locked: cannot change its state. Call unlock() to allow changes to the instrument state')

    def power_on(self):
        self._check_lock()
        self.ps.output(state=True, readonly=False)

    def power_off(self):
        self._check_lock()
        self.ps.output(state=False, readonly=False)

    def power_enable(self, state):
        self._check_lock()
        self.ps.output(state=state, readonly=False)

    def power_cycle(self, delay=4):
        self._check_lock()
        self.ps.output(state=False, readonly=False)
        time.sleep(delay)
        self.ps.output(state=True, readonly=False)

    def status(self):
        return self.ps.status()

    def is_enabled(self):
        return self.ps.get_output_state()

    def is_ok(self):
        """ Return the operational state of the power supply.
        """
        return self.ps.get_state() == "OK"

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
