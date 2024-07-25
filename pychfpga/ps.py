#!/usr/bin/env python
"""
REST Server and clients for the Agilent N5700-series power supplies.

"""

# Python Standard Library packages
import aiohttp
import argparse
import asyncio
import sys
import time

# PyPi packages

# External private packages
from wtl import log
from wtl.rest import AsyncRESTServer, AsyncRESTClient # generic REST servers and clients
from wtl.rest import endpoint, run_client, SocketContext
from wtl.namespace import NameSpace
from wtl.config import load_yaml_config
from wtl.metrics import Metrics

class AgilentN5700(SocketContext):
    """
    A class to communicate with an Agilent N5700- or N8700-type power supply
    """
    SUPPORTED_PS = {
        # model : (name, IDN substring, Vmax, Imax)
        'N5764A': ('Agilent Power Supply', 'Agilent Technologies,N5764A', 21, 79.8 ), # CRATE PSU
        'N8731' : ('Agilent Power Supply', 'Agilent Technologies,N8731A', 8, 400), # CHIME Rx hut FLA PSU
        'N5743A' : ('Agilent Power Supply', 'Agilent Technologies,N5743A', 8, 50) # PCO FLA PSU
    }

    def __init__(self,  hostname, port=5025, timeout=0.5, verbose=1):

        super().__init__(hostname=hostname, port=port, timeout=timeout, close_socket=True)
        self.log = log.get_logger(self)
        print(f"Initializing direct LAN Connection at {hostname}:{port}")
        self.locked = True
        self.verbose = verbose
        self.instrument_name = None
        self.instrument_model = None
        self.log.debug('Initializing instrument')

        # self.device_clear()


    def __repr__(self):
        if self.instrument_model:
            return f'{self.instrument_name} {self.instrument_model} @{self.ip_addr}:{self.ip_port}'
        else:
            return f'Unknown Instrument @{self.ip_addr}:{self.ip_port}'

    ###################################
    # Basic read/write commands
    ###################################


    def command(self, comstr, flush=False):
        """
        Sends a command to the instrument. The terminator is added automatically.
        """
        with self.socket(flush=flush):
            self._check_instrument_type()
            self.send(comstr + "\n")

    def query(self, command, flush=False, **kwargs):
        """
        Sends a command to the instrument and returns the reply string without the terminator or trailing spaces.
        """
        with self.socket(flush=flush):
            self._check_instrument_type()
            self.send(command + '\n')
            # self.command('*WAI')
            try:
                reply_string = self.recv(16384)
            except IOError:
                raise IOError(f'{self!r}: timeout while waiting for reply for command {command}')
            return reply_string.rstrip() # remove trailing spaces or CR or LF

    def _check_instrument_type(self):
        """
        Make sure the instrument type and model is known and supported.
        """
        if self.instrument_model:
            return
        # print(self.instrument_model, self.instrument_name)
        with self.socket(flush=True):
            self.send('*IDN?\n')
            id_string = self.recv(timeout=min(1, self.timeout))
            self.log.debug(f'Instrument Identification string: {id_string}')
            for (instrument_code, (instrument_name, instrument_id_string, vmax, imax)) in self.SUPPORTED_PS.items():
                #print('checking if %s is in %s' % (instrument_id_string, id_string))
                if instrument_id_string in id_string:
                    self.log.debug(f'Connected to: {instrument_name}')
                    self.send(f'STATus:OPERation:ENABle {0x0500}\n' )
                    self.instrument_model = instrument_code
                    self.instrument_name = instrument_name
                    self.instrument_vmax = vmax
                    self.instrument_imax = imax
                    break

            if self.instrument_model is None:
                self.log.warning(f'{self!r}: Instrument {id_string} is not supported')
                raise RuntimeError('The identification command did not return the expected instrument ID string')

    def open(self):
        with self.socket():
            self._check_instrument_type()

    def query_float(self, *args,  **kwargs):
        return float(self.query(*args, **kwargs))

    def query_int(self, *args,  **kwargs):
        return int(self.query_float(*args, **kwargs))

    def waituntilready(self):
        while not(self.query_float('*OPC?')):
            time.sleep(0.01)

    def lock(self):
        self.locked = True

    def unlock(self):
        self.locked = False

    def _check_lock(self):
        if self.locked:
            raise RuntimeError(
                'Instrument is locked: cannot change its state. Call unlock() '
                ' to allow changes to the instrument state')


    def set_output(self, state=None):

        """
        Turns on and off the output and measures current output state input
        state can be varius spellings of 'on'/ 'off', None, 0, or 1   (default
        is None)

        Returns status dictionary
        """
        self._check_lock()
        outstate = []
        if state is None:
            pass
        if state in ['on', 'On', 'ON', 1, True]:
            state = 1
        elif state in ['off', 'Off', 'OFF', 0, False]:
            state = 0
        else:
            raise ValueError('Unknown desired output power state')
        with self.socket(flush=True):
            self.command(f'OUTP:STAT {state}', flush=True)
            self.waituntilready()
            # self.command('*WAI')

        return {'PowerEnabled': self.get_output_state()}


    def get_output_state(self):
        return bool(self.query_float('OUTP:STAT?'))

    def set_power_on_state(self, state):
        if state.upper() not in ('RST','AUTO'):
            raise ValueError(f'{self!r}: Power on state is either RST or AUTO')
        self.command(f'OUTPut:PON:STATe {state}')

    def get_power_on_state(self):
        return self.query('OUTPut:PON:STATe?')

    def is_enabled(self):
        return self.get_output_state()

    def power_on(self):
        self.set_output(state=True)

    def power_off(self):
        self.set_output(state=False)

    def power_enable(self, state):
        self.set_output(state=state)

    def power_cycle(self, delay=4):
        with self.socket():
            self.set_output(state=False)
            time.sleep(delay)
            self.set_output(state=True)

    def get_state(self):
        """ Return the power supply operational state of the power supply.

        Returns:

            'OK': power supply is turned on and operates normally
            'OFF': power supply is turned off
            'ILIMIT': power supply is in current limit mode
            'FAULT': A fault has occured
        """
        with self.socket():
            failmode = int(self.query_float('STAT:QUES:COND?'))
            if failmode != 0:
                state = 'FAULT'
            else:
                op_state = int(self.query_float('STATus:OPERation:CONDition?'))
                if bool((op_state & (1 << 8)) >> 8):  # Voltage regulating
                    state = 'OK'
                elif bool((op_state & (1 << 10)) >> 10):  # Current limiting
                    state = 'ILIMIT'
                else:
                    state = 'OFF'
            return state


    def is_ok(self):
        """ Return the operational state of the power supply.
        """
        return self.get_state() == "OK"



    def status(self):

        """
        Returns a dictionary containting the output voltage and current
        """
        with self.socket():
            meas = {'current': 0, 'voltage': 0}
            current = self.query_float('MEAS:CURR?', timeout=2)
            voltage = self.query_float('MEAS:VOLT?', timeout=2)
            power = round(current * voltage, 3)
            meas['current'] = current
            meas['voltage'] = voltage
            meas['power'] = power
            meas['status'] = self.get_state()
            return meas

    def set_voltage(self, voltage):
        """
        Sets the output voltage - Valid range is 0 to 21V - default is None
        Returns the power supply setpoint voltage
        """
        self._check_lock()
        if voltage > self.instrument_vmax or voltage < 0:
            raise ValueError('Invalid voltage - must be in range [0..21] - no action performed')
        with self.socket():
            self.command(f'VOLT {voltage}')
            # self.command('*WAI')
            self.waituntilready()

    def get_voltage_setting(self):
        """
        Returns the power supply setpoint voltage
        """
        return self.query_float('VOLT?')

    def set_current_limit(self, current=None, ocp=None):
        """
        Sets the current limit and can enable disable ocp  - Valid range is 0
        to 76A - by default current is None and ocp is None

        Returns the power supply current limit
        """
        self._check_lock()

        if ocp is not None:
            self.set_protection(ocp=ocp)
        if current > self.instrument_imax or current < 0:
            raise ValueError('Invalid current limit - must be in range [0..76] - no action performed')
        with self.socket():
            self.command(f'CURR {current}')
            # self.command('*WAI')
            self.waituntilready()

    def get_current_limit(self):
         """
         Returns the power supply current limit
         """
         return self.query_float('CURR?')

    def clear(self):
        """
        If any of the protection has triggered will need to clear it. Will
        return a False if everything is good
        """
        self._check_lock()
        with self.socket():
            self.set_protection(clear=True)
            problem = self.get_protection()[0]
            return problem

    def set_protection(self, uvl=None, ovp=None, ocp=None,ilim=None, clear=None):
        """
        Adjusts power supply protection settings
        Returns two dictionaries the first with the current protection settings, the second with the fail modes
        Warning - when clearing - return status is 'dont trust anything' - run protection another time
        """
        self._check_lock()
        with self.socket(flush=True):
            if clear == 1:
                self.command('OUTPut:PROT:CLEar')
                #self.command('*WAI')
                self.waituntilready()

            if uvl != None:
                self.command(f'VOLT:LIM:LOW {uvl}')
            if ovp != None:
                self.command(f'VOLT:PROT {ovp}')
            if ocp != None:
                self.command(f'CURR:PROT:STAT {ocp}')
                #Note that OCP is not the current limit, only behaviour on current limit (can be 0 or 1)
                #With OCP active current switches to triggered current (by default and not changed by this program so far 0A)
            if ilim != None:
                self.command(f'CURR {ilim}')

    def get_protection(self, history=False):

        with self.socket():
            uvlmeas=self.query_float('VOLT:LIM:LOW?')
            ovpmeas= self.query_float('VOLT:PROT?')
            ocpmeas=self.query_float('CURR:PROT:STAT?')
            ilimmeas=self.query_float('CURR?')

            if not(history):  #By default just read the main status register not the register that clears itself after reading
                failmode=int(self.query_float('STAT:QUES:COND?'))
            else: #If you really want the register that clears itself set History=True
                failmode=int(self.query_float('STAT:QUES?'))  #Will spot if previously things went wrong or if currently things are wrong
                print("Not that reliable and it clears itself after!")

            problem = False
            if failmode != 0:
                self.log.warn('A power supply problem is present')
                problem = True

            UNR = bool( ( failmode & ( 1 << 10 ) ) >> 10 )  #True if Unregulated output
            if UNR == 1:
                UNRMes = 'Unregulated output'
            else:
                UNRMes = 'Output is regulated'

            INH = bool( ( failmode & ( 1 << 9 ) ) >> 9 )  #True if output turned off by J1 inhibit signal
            if INH == 1:
                INHMes = 'Inhibt signal on J1 turned off output'
            else:
                INHMes = 'No Inhibt signal on J1 has been detected'

            OT = bool( ( failmode & ( 1 << 4 ) ) >> 4 ) #True if output turned off by power supply temperature monitor
            if OT == 1:
                OTMes = 'Power supply got too hot and turned off output'
            else:
                OTMes = 'Power supply temperature okay'

            PF = bool( ( failmode & ( 1 << 2 ) ) >> 2 ) #True if output turned off because AC power failed
            if PF == 1:
                PFMes = 'Input Power faliure and output turned off '
            else:
                PFMes = 'Input power okay'

            OC = bool( ( failmode & ( 1 << 1 ) ) >> 1 ) #True if output turned off because of Over current
            if OC == 1:
                OCMes = 'OCP triggered, output off '
            else:
                OCMes = 'OCP did not trigger'

            OV = bool( ( failmode & ( 1 << 0 ) ) >> 0 ) #True if output turned off because of Over voltage
            if OV == 1:
                OVMes = 'Over voltage protection triggered, output turned off '
            else:
                OVMes = 'No over voltage detected'
            failmode = {'UNR': [UNR,UNRMes], 'INH': [INH, INHMes], 'OT': [OT, OTMes], 'PF': [PF, PFMes], 'OC':[OC, OCMes], 'OV':[OV, OVMes]}


            protectionstatus={'uvl':uvlmeas, 'ovp':ovpmeas, 'ocp':ocpmeas, 'ilim':ilimmeas}
            #if clear == 1 and problem == 0:
            #    problem = 'dont trust anything'

            return problem, protectionstatus, failmode

    def configure_power_on_state(self, voltage, current):
        """
        Configure the power supply so it will automatically power up at the specified voltage and current limit.

        The power supply is turned off before the new settings are applied.
        """
        with self.socket():
            self.power_off()
            self.set_voltage(voltage)
            self.set_current_limit(current)
            self.set_power_on_state('AUTO')


class PowerSupplyAsyncRESTServer(AsyncRESTServer):
    """
    Web server with REST interface to operate an array of Agilent/Keysight power supplies.

    Supports Agilent N5764 and N8731 supplies (although all related supplies will also work).

    The server is based on the asynchrounous Tornado web server.

    The following REST endpoints are provided by the web server:



    """

    DEFAULT_PORT = 54324

    POWER_SUPPLY_CLASSES = {
        'AgilentN5764': AgilentN5700,
        'AgilentN8731': AgilentN5700
        }

    def __init__(self,  address='', port=DEFAULT_PORT, logging_params={}):
        """ power_supplies list of dict with entries 'type', 'name', and 'address'
        """
        self.power_supplies = {}
        # self.name = name
        # self.ps_port = 5025
        super().__init__(address=address, port=port, heartbeat_string='Ps')

    def _parse_names(self, ps_names):
        # print('*********************_parse_names',ps_names)
        if not ps_names:
            return []
        if isinstance(ps_names, str):
            ps_names = ps_names.replace(' ', ',').split(',')
        ps_names = [name.strip() for name in ps_names]
        # Expand aliases

        self.log.warning(f'{ps_names!r}, {self.config.aliases!r}')
        for ps_name in list(ps_names):  # make a copy, we modify the list
            if ps_name in self.config.aliases:
                ps_names.remove(ps_name)
                ps_names.extend(self.config.aliases[ps_name])
        if 'all' in ps_names or '*' in ps_names:
            ps_names = self.power_supplies.keys()
        unknown_supplies = [name for name in ps_names if name not in self.power_supplies]
        if unknown_supplies:
           raise RuntimeError(f"{self!r}: Unknown power supply names {unknown_supplies}")
        return ps_names


    async def _get_metrics(self):
        """ Return a Metrics object containing power supply monitoring data
        """
        self.log.info(f'{self!r}: Received monitoring metrics request')
        metrics = Metrics()
        for ps_name, ps in self.power_supplies.items():
            try:
                status = NameSpace(ps.status())
                metrics.add('fpga_power_supply_voltage', name=ps_name, value=status.voltage, type='gauge')
                metrics.add('fpga_power_supply_current', name=ps_name, value=status.current, type='gauge')
                metrics.add('fpga_power_supply_power', name=ps_name, value=status.power, type='gauge')
                metrics.add('fpga_power_supply_status', name=ps_name, value=int(status.status == 'OK'), type='gauge')
            except IOError:
                pass
        return metrics

    def _set_is_ready_later(self, name):
        """ Sets the is_ready flag for power supply `name` to True after the power supply power-up delay has elapsed """
        def callback():
            self.is_ready[name] = True
        self.call_later( self.power_up_delay[name], callback)

    ##################
    # Server commands
    ##################

    @endpoint('start')
    async def start(self, **config):
        """ ``POST endpoint: /start`` Initializes the power supply server with the provided configuration.

        This creates a power supply instance for each of the supply specified under the ``units`` key.
        Units that are already powered on are marked immediately as ready.
        """
        if self.power_supplies:
            raise RuntimeError(f'{self!r}: Power Supply server is already started')
        self.config = NameSpace(config)
        self.power_supplies = {}
        self.power_up_delay = {}
        self.is_ready = {}

        units = self.config.units or {}
        for name, ps in units.items():
            type_ = ps.pop('type')
            self.power_up_delay[name] = ps.pop('power_up_delay')
            cls = self.POWER_SUPPLY_CLASSES[type_]
            ps_instance = cls(**ps)
            self.power_supplies[name] = ps_instance
            # self.power_supplies[name].open()
            self.is_ready[name] = False

        # If a power supply is already up and running (for an unknown period of time),
        # schedule the `is_ready` flag to be true after the power-up delay
        for ps_name, ps in self.power_supplies.items():
            if ps.is_ok():
                self.is_ready[ps_name] = True
                #self._set_is_ready_later(ps_name)

        return 'Power supply server started'

    @endpoint('stop')
    async def stop(self):
        """ ``GET endpoint: /stop`` Uninlitializes the server and keep it running so it can be started with a new configuration."""
        if not self.power_supplies:
            self.log.warning(f'{self!r}: Power Supply server is not started')
        else:
            for ps_name, ps in self.power_supplies.items():
                ps.close()
            self.power_supplies = {}
        return 'Power supply server stopped'

    @endpoint('status')
    async def status(self):
        """ ``GET endpoint: /status`` Return the status of all the supplies handled by this server.

        Returns:
            dict, with the following contents:

            - is_started (bool): true when the server is initialized
            - ps_names (list): list of str describing the names of all the supplies handled by
                the current running configuration.
            - name1 (dict): Dict that describes the status of the powert supply unit named
                ``name1``, as returned by the :meth:`AgilentN5700.status()` method. In the format:

                - current (float): output current, in Amps
                - power (float): output power, in Watts
                - voltage (float):output voltage, in Volts
                - status (str): 'OK', 'OFF', 'ILIM' or 'FAULT'

            - name2 ...
        """
        # ps_names = self._parse_names(ps_names)
        # self.log.info('%.32r: Received status request for %r' % (self, ps_names))
        stati = dict(is_started=bool(self.power_supplies),
                     ps_names=list(self.power_supplies.keys()))
        for ps_name, ps in self.power_supplies.items():
            stati[ps_name] = ps.status()
            self.log.info(f'{self!r}: Status of {ps_name} is {stati[ps_name]}')
        return stati


    @endpoint('is-started')
    async def is_started(self):
        """ ``GET endpoint: /is-started`` indicates if the server is initialized.

        This information is also included in the status() dict.

        Returns:
            bool: True if the power supply server is initialized
        """
        return bool(self.power_supplies)


    @endpoint('list-names')
    async def listNames(self):
        """ ``GET endpoint: /list-names`` Return the list of the names of the supplies handled bu
        the current running configuration.

        This information is also included in the status() dict.

        Returns:
            list of str: names of the supplies, as defined in the configuration
        """
        self.log.info(f'{self!r}: Received list names request')
        return list(self.power_supplies.keys())

    @endpoint('power-on')
    async def power_on(self, ps_names=None):
        """ ``POST endpoint: /power-on`` Powers up the specified supplies.

        Parameters:

            ps_names (str or list of str): List of power supplies to power on. If a string, the
                string is splitted into a list at the space or comma delimiters. Each string in the
                list can be a supply name (the keys in the ``units`` dict), or an alias (found as a
                key in the ``alias`` dict). If 'all', all the supplies are affected.

        Returns:
            str: Message indicating the result of the operation
        """
        ps_names = self._parse_names(ps_names)
        self.log.info(f'{self!r}: Received power on command for {ps_names}')

        for ps_name in ps_names:
            ps = self.power_supplies[ps_name]
            # status = ps.status()  # todo: make async
            # if status['status'] == 'OK':
            if ps.is_enabled():
                self.log.info(f"{self!r}: Power supply '{ps_name}' is already ON")
            else:
                self.log.info(f"{self!r}: Turning ON power supply '{ps_name}'")
                ps.unlock()
                ps.power_on() # todo: make async
                ps.lock()
                self.is_ready[ps_name] = False
                self._set_is_ready_later(ps_name)
                self.log.info(f"{self!r}: {ps_name} is powered ON")

                # await asyncio.sleep(self.config.power_on.delay) # make this asynchronous so all the delay happen in parallel
        return f"{ps_names} are powered ON"

    @endpoint('power-off')
    async def power_off(self, ps_names=None):
        """ ``POST endpoint: /power-off`` Powers down the specified supplies.

        Parameters:

            ps_names (str or list of str): List of power supplies to power off. If a string, the
                string is splitted into a list at the space or comma delimiters. Each string in the
                list can be a supply name (the keys in the ``units`` dict of the configuration), or
                an alias (found as a key in the ``alias`` dict). If 'all', all the supplies are
                affected.

        Returns:
            str: Message indicating the result of the operation
        """
        ps_names = self._parse_names(ps_names)
        self.log.info(f'{self!r}: Received power off command for {ps_names!r}')

        for ps_name in ps_names:
            ps = self.power_supplies[ps_name]
            self.is_ready[ps_name] = False
            if not ps.is_enabled():
                self.log.warning(f"{self!r}: Power ouput already disabled for {ps_name}")
            else:
                ps.unlock()
                ps.power_off()
                ps.lock()
                self.log.info(f"{self!r}: {ps_name} is powered OFF")
        return f'{ps_names} are powered OFF'


    @endpoint('is-enabled')
    async def is_enabled(self):
        is_enabled = {name: ps.is_enabled()
                    for name, ps in self.power_supplies.items()}
        return is_enabled

    @endpoint('is-ready')
    async def get_is_ready(self):
        is_ready = {name: (ps.is_enabled() and ps.is_ok() ) #and self.is_ready[name]
                    for name, ps in self.power_supplies.items()}
        return self.is_ready


    @endpoint('get-metrics')
    async def get_metrics(self):
        metrics = await self._get_metrics()
        return metrics.as_dict()

    @endpoint('get-monitoring-data')
    async def monitoringMetrics(self):
        self.log.info(f'{self!r}: Received monitoring metrics request')
        metrics = await self._get_metrics()
        return aiohttp.web.Response(text=str(metrics))


#########################################
# Power Supply REST client
#########################################

class PowerSupplyAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote RawAcq server.

    This client is used by ch_master to start, configue and operate all the RawAcq servers in the array.

    The client is implemented using a Tornado AsyncHTTPClient. It exposes the RawAcq server methods
    (i.e REST endpoints) as local methods. The local methods are Tornado coroutines so requests to
    multiple clients can be made in parallel. This is especially beneficial since the data requests
    from the server are slow IO operations which benefit the mist from co-execution.

    The client will operate only if the IOloop in which is was created is running.

    Parameters:

        name (str): Name of the client, to be used in logging etc.

        hostname (str): The hostname of the RawAcq REST server. If `host` is None, an (experimental,
             Python-based) RawAcq REST server will be created locally.

        port (int): The port number to which the RawAcq REST server is listening. Default is port 80.

        ps_names (list of str): list of power supply names on which this client will operate. Other
            supplies will not be affected.
    """
    DEFAULT_PORT = PowerSupplyAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname='localhost', port=DEFAULT_PORT):
         super().__init__(
            hostname=hostname,
            port=port,
            server_class=PowerSupplyAsyncRESTServer,
            heartbeat_string='Pc')

    async def start(self, **config):
        """ If the PowerSupply remote server is not started, start it with the specified configuration

        Parameters:

            config (str, dict or NameSpace): If a string, the configuration is
                loaded from the specified configuration file and name. if a
                dict, it is passed directly to the server. If a `Namespace`,
                it is converted to a dict befoe being passed to the server.

        """
        self.log.info(f'{self!r}: Starting remote PowerSupply server at {self.hostname}:{self.port}')

        if isinstance(config, str):
            config = load_yaml_config(config)
        if isinstance(config, NameSpace):
            config = config.as_dict()

        server_info = NameSpace(await self.status())
        ps_names = list(config['units'].keys())


        if not server_info.is_started:
            self.log.info(f'{self!r}: Server not started. Starting it with the provided configuration')
            start_results = await self.post('start', **config)  # start the server if not already started
        else:
            self.log.info(f'{self!r}: Server is already started')
            if set(server_info.ps_names) != set(ps_names):
                self.log.warning(f'{self!r}: The server does not support the same supplies as the current config ({server_info.ps_names} instead of {ps_names})')
            start_results = 'Already started'

        # result = await self.post('start', **config)

        # if server_info.name != name:
        #     raise RuntimeError('%.32r: The remote server does not have the expected name (%s instead of %s)' % (self, server_info.name, name))


        return start_results

    async def is_started(self):
        return (await self.get('is-started'))

    async def stop(self):
        result = await self.get('stop')
        return result

    async def status(self):
        result = await self.get('status')
        return result

    async def is_enabled(self):
        """ Indicates if the power supplies are enabled (but do not necessarily produce a valid output) """
        result = await self.get('is-enabled')
        return result

    async def is_ready(self):
        """ Indicates if the power supplies are enabled, have a valid output and the power up delay has elapsed.
        """
        result = await self.get('is-ready')
        return result

    async def list_names(self):
        result = await self.get('list-names')
        return result

    async def power_on(self, *ps_names):
        result = await self.post('power-on', ps_names=ps_names)
        return result

    async def power_off(self, *ps_names):
        result = await self.post('power-off', ps_names=ps_names)
        return result

    async def power_cycle(self, *ps_names):
        off_result = await self.post('power-off', ps_names=ps_names)
        await asyncio.sleep(3)
        on_result = await self.post('power-on', ps_names=ps_names)
        return (off_result, on_result)

    async def get_metrics(self):
        result = await self.get('get-metrics')
        return Metrics(result)





def parse_cmdline_args(argv):
    parser = argparse.ArgumentParser(description="ps: Receiver hut power supply control server", epilog="""
        """)
    parser.add_argument('args', type=str, nargs='*', default='',  help='"server" or "client" ')
    parser.add_argument('-p', '--port', default=PowerSupplyAsyncRESTServer.DEFAULT_PORT, type=int, help="Server port")
    parser.add_argument('-n', '--host', default='localhost', type=str, help="Server hostname")
    parser.add_argument('-s', '--server', action='store_true', help='Start a server')
    return parser.parse_args(argv)

def main():
    """ Command-line interface to operate the Power Supply server.

    ./ch_ps.py [config [server_name]] [command {args}] [--host hostname] [--port port_number] [--no-run | --run] [--no-start]

    where:
        *config* : configuration in the format [[*filename*]:][*path_to_config_object*]
        *server_name* : power supply server config to use. Optional if there is only one.
        *command* : the name of a ChimeMaster client method.
        --host: hostname of the server. Overrides the hostname found in the config. Default is 'localhost'.
        --port: port number of the server. Overrides the port number found in the config.  Default is 54321.
        --run: run the client/server until Ctrl-C is pressed. Default when no command is provided.
        --no-run: Do not run the client/server even if no comman dis provided.
        --no_start: do not attempt to initialize the server even if a configuration is provided.

    The `ch_master` command is invoked from the command line with::

        ./ps.py arguments...  # linux only
        python ps.py arguments

    Or from an ipython interactive session::

        run -i ps arguments

    Operations done:

        1. Create client:

            - Always starts a client that connects to server at address specified in config or as
              overriden by --host and --port.

        2. Create server if none already esists:

            - If there is no server, a server is created at localhost on the port specified in the
              config or as overriden by --port, unless -no-server is specified

        3. Initialize server with config file if requested:

            - If no config is present, or if --no-start option is specified, the server is not started
            - If there is a config file, the 'start' command is sent along with the specified
              config. If the server is already started with a different config, an error will be
              raised.

        4. Execute command or run server:

            - If a command and arguments are specified, the corresponding client methods commands
              are invoked. Those generally pass on the command to the corresponding server endpoint.
            - If no command is specified and a local server was started, the client (and locally
              started server if any) are run continually until stopped by Ctrl-C. Bypassed if --no-
              run is specified

    Examples:

    Create and initialize and run a new local server  or initialize an existing server::

        ./ps.py jfc.erh

    Create an non-initialized server

        ./ps.py  # starts server on localhost:54321
        ./ps.py config --no-start # starts server at address specified in config

    Send a command to server:

        ./ps stop # send stop command to server on localhost:54321
        ./ps jfc.erh power_off # power off supplies used by server running at theaddress specified in the jfc.erh config
    """
    # Setup logging
    log.setup_basic_logging('DEBUG')
    client, server = run_client(
        sys.argv[1:],
        PowerSupplyAsyncRESTServer,
        PowerSupplyAsyncRESTClient,
        object_name ='PowerSupply',
        server_config_path='power_supplies.servers')
    return client, server

if __name__ == '__main__':
    client, server = main()


