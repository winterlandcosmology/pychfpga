#!/usr/bin/env python
"""
REST Server and clients to operate and monitor Spectrum Instruments TM-4D GPS units.
The units are accessed through a serial-to-ethernet adapter (e.g. the StarTech NETRS232).

"""
# Python Standard Library
import sys
import time
import datetime
import calendar
import queue
import asyncio
import aiohttp
import traceback

# External private packages
from wtl import log
from wtl.rest import AsyncRESTServer, AsyncRESTClient  # generic REST servers and clients
from wtl.rest import endpoint
from wtl.rest import run_client, SocketContext
from wtl.namespace import NameSpace
from wtl.config import load_yaml_config
from wtl.metrics import Metrics
try:
    import comet
except ImportError:
    comet = None

# Local imports
from pychfpga import __version__

class SpectrumInstrumentsTM4D(SocketContext):
    """
    A class to communicate with SpectrumInstruments TM4D GPS receiver
    """

    def __init__(self,  hostname, port=1001, timeout=0.5, verbose=1):

        super().__init__(
            hostname=hostname,
            port=port,
            timeout=timeout,
            close_socket=False)  # keep socket open because reconnecting causes data loss
        self.log = log.get_logger(self)
        self.log.info(f"Initializing direct LAN Connection at {hostname}:{port}")
        self.verbose = verbose
        self.log.debug('Initializing instrument')
        self.polling_mode = None
        self.last_gps_time = None
        self.gps_time_valid = False
        self.gps_leap_seconds = None
        self.use_gps_time = True
        self.buffer = '' # used in broadcast processing only
        self.get_methods = {
            '50': None, # Acknowledge
            '51': self.get_date_time,
            '52': self.get_position,
            '53': self.get_altitude,
            '55': self.get_mask_angle,
            '56': self.get_user_time_bias,
            '57': self.get_timing_mode,
            '59': self.get_geometric_quality_and_almanac_status,
            '60': self.get_mux1_output_source,
            '61': self.get_timing_status,
            '62': None,  # Event Time Tag
            '63': None,  # POP/ETT Status
            '64': self.get_oscillator_tuning_mode,
            '65': self.get_alarm_status,
            '66': None,  # Reserved, #66,T28F2A.OBJ,NEWEPPSD10A.HEX   ,312102035321F,28F2,031816
            '68': self.get_mux2_output_source,
            '69': self.get_tracking_channel_status,
            '70': None,  # Serial time message format
            '71': self.get_serial_time_code_format,  # Serial time code format
            '72': None,  # Reserved
            '73': None,  # ETT Parameters
            '74': None,  # POP Parameters
            '75': self.get_speed_and_heading,
            '76': self.get_nmea_info,
            '74': None,  # Phase lock status, old units (see #80)
            '78': self.get_user_options,
            '79': self.get_coast_timer,
            '80': self.get_phase_lock_status,
            '81': self.get_leap_seconds,
            '82': None,  # Undocumented, #82,0,1,8,8,F,F
            '84': None,  # Undocumented, #84,1,0,5,3,2,1,F  # probablu BNC output configuration
            '85': None,  # Undocumented, '#85,0058
            '86': None,  # Undocumented #86,99.99,99.99,00.41,99.99,00.41
            }

    ###################################
    # Basic read/write commands
    ###################################


    def command(self, *args, **kwargs):
        """
        Sends a command to the instrument. The terminator is added automatically.
        """
        flush = kwargs.get('flush', False)
        with self.socket(flush=flush):
            cmd = f"#{','.join(str(s) for s in args)}\r\n"
            if self.verbose:
                print(f'Sending command: {cmd}')
            self.send(cmd)

    def query(self, command, reply=None, flush=False):
        """
        Sends a command to the instrument and returns the reply string without the terminator or trailing spaces.
        """
        if reply is None:
            with self.socket(flush=flush):
                self.send(f'#13,{command}\r\n')
                try:
                    reply = ''
                    while True:
                        s = self.recv(16384)
                        has_crlf = '\r\n' in s
                        print(f"Received {s!r} (CRLF={has_crlf})")
                        reply += s
                        if '\r\n' in reply:
                            break
                except IOError:
                    raise IOError(f'{self!r}: timeout while waiting for reply for command {command}')
        args = reply.rstrip().split(',') # remove trailing spaces or CR or LF
        assert args[0] == '#' + command, f'Reply is not for command {command}'
        return args[1:]

    def add_metric(self, metrics, metric_name, value, type='gauge', **labels):
        if metrics is None:
            return
        if self.use_gps_time:
            if self.last_gps_time and self.gps_leap_seconds is not None:
                utc_time = self.last_gps_time - self.gps_leap_seconds
                local_time = time.time()
                if utc_time - local_time > 1:
                    self.log.warning(f'{self!r}: GPS time for metric {metric_name} is in advance from system time by {utc_time-local_time} seconds.')
            else:
                self.log.warning(f'{self!r}: No GPS time has been received yet. Using system time for the metric {metric_name}')
                utc_time = time.time()
            metrics.add(metric_name, value=value, type=type, time=utc_time * 1000, **labels)
        else:
            metrics.add(metric_name, value=value, type=type, **labels)

    ###################################
    # GPS commands
    ###################################

    # Set commands


    def enable_ntp_output(self, enable):
          self.command('04', 3123, 3, 1, 3 if enable else 2, 1, 0, 2, 0, 3, 9, 7, 6, 5, 3) # secret command from Tom Versaput

    def set_mask_angle(self, angle_code):
        """ Sets mask angle of the GPS.

        Parameters:
            angle_code (int): 0=5 deg, 1=15 deg, 2=20 deg
        """
        if angle_code not in [0, 1, 2]:
            raise ValueError(f'{self!r}: mask angle argument is 0 (5 deg), 1 (15 deg) or 2 (20 deg)')

        self.command('05', angle_code)

    def set_user_time_bias(self, bias):
        """ Sets the user time bias.

        Parameters:
            bias (int): time bias in ns (-99999 to 99999). Negative values cause the timing functions to occur later
                in absolute time while positive values cause them to occur earlier.
        """
        if not  -999999 <= bias <= 999999:
            raise ValueError(f'{self!r}: bias must be between -999999 and 99999 ns')

        self.command('06', f'{bias:+06d}')

    def set_timing_mode(self, mode):
        """ Sets the timing mode of the GPS.

        Parameters:
            mode (int): 0=Dynamic, 1=Static, 3=Auto survey
        """
        if mode not in [0, 1, 3]:
            raise ValueError(
                f'{self!r}: timing mode argument must be '
                f'0 (Dynamic), 1 (Static) or 3 (Survey)')

        self.command('07', mode)

    def master_reset(self):
        """ Resets the GPS.
        """
        self.command('08', 1)

    def set_multiplexer_output_source(self, mux1, mux2):
        """ Selects the output of the multiplexers.

        Parameters:
            mux1 (int):
                0: 10 MHz output
                1: 5 MHz output
                2: 1 MHz output
                3: 100 kHz output
                4: 10 kHz output
                5: 1 kHz output
                6: baseband IRIG output (if installed)
                7: PPS output
                8: OFF (newer TM-4's only)

            mux2 (int):
                0: for 10 MHz output
                1: for Mux1 mirror output
                2: for PPS
                3: for optional output 1
                4: for optional output 2
                5: for optional output 3
                6: for baseband IRIG (if installed)
                7: for baseband NASA-36 (if installed)
                8: for OFF (newer TM-4's only)
        """
        if not 0 <= mux1 <= 8:
            raise ValueError(f'{self!r}: Mux1 selector value must be between 0 and 8')
        if not 0 <= mux2 <= 8:
            raise ValueError(f'{self!r}: Mux2 selector value must be between 0 and 8')

        self.command('09', mux1)
        self.command('14', mux2)

    def set_broadcast_output(self, mode):
        """ Sets the broadcast output mode.

        Parameters:
            mode (int): 0= output all messages, 1=Output events and acknowledges only.
        """
        if mode not in [0,1]:
            raise ValueError(
                f'{self!r}: broadcast mode must be  '
                f'0 (all messages) or '
                f'1 (events or acknowledge only)')

        self.command('12', mode)

    def set_serial_time_code_format(self, format):
        """ Sets the serial time code format.

        Parameters:
            format (int):
                0= IRIG-B002/B122 (no year)
                1= NASA-36
                2= IRIG-B007/B127 (BCD year and SBS)
        """
        if format not in [0, 1, 2]:
            raise ValueError(
                f'{self!r}: broadcast mode must be '
                f'0= IRIG-B002/B122 (no year), '
                f'1= NASA-36, '
                f'2= IRIG-B007/B127 (BCD year and SBS)'
                )
        self.command('16', format)

    def set_polling_mode(self, mode=1):
        """ Sets the polling mode.

        Parameters:
            mode (int):

                0=automatic broadcast,

                1=polling with acknowledge,

                2=polling without acknowledge.
        """
        if mode not in [0,1,2]:
            raise ValueError(
                f'{self!r}: Polling mode must be '
                f'0=automatic broadcast, '
                f'1=polling with acknowledge, '
                f'2=polling without acknowledge')

        self.command('17', mode)

        self.polling_mode = mode
        if mode: # if we are not in broadcase mode, flush any data in the buffers
            with self.socket(flush=True, flush_timeout=0.1):
                pass

    def set_position(self, lat, lon, alt):
        """ Sets the position to use in the static timing mode.

        Parameters:
            lat (float): latitide in fractional degrees, positive=north, negative=south
            lon (float): longitude in fractional degrees, positive=east, negative=west
            alt : altitude in meters
        """
        self.command('19', '%02i%5.2f' % (abs(lat), abs(lat) % 1 * 60),
                           'N' if lat>=0 else 'S',
                           '%03i%5.2f' % (abs(lon), abs(lon) % 1 * 60),
                           'E' if lon>=0 else 'W',
                           '%+06.0f' % alt)

    def set_antenna_alarm_enable(self, enable):
        """ Enables or disables the antenna alarm.

        Parameters:
            enable (bool)
        """
        self.command('23', int(bool(enable)))

    def set_pps_output_source(self, source):
        """ Sets the source of the Pulse-Per-Second (PPS) signal.

        Parameters:
            source (int):

                0 = LOW at power-on/GPSPPS on Time Valid/FILPPS on lock,
                1 = LOW at power-on/FILPPS on lock,
                2 = LOW on power-up/GPSPPS on valid time and Lock,
                3 = GPSPPS always
        """
        if source not in [0, 1, 2, 3]:
            raise ValueError(
                f'{self!r}: PPS source '
                f'0=LOW at power-on/GPSPPS on Time Valid/FILPPS on lock, '
                f'1=LOW at power-on/FILPPS on lock, '
                f'2=LOW on power-up/GPSPPS on valid time and Lock, '
                f'3=GPSPPS always')

        self.command('24', source)

    def set_time_format(self, fmt):
        """ Sets the output time format to UTC or GPS time.

        Parameters:
            fmt (int): 0: GPS time, 1: UTC time

        """
        if fmt not in [0, 1]:
            raise ValueError(f'{self!r}: format can be 0=GPS or 1=UTC')
        self.command('26', fmt)

    def set_bnc_output_source(self, outa=None, outb=None, outc=None, outd=None, oute=None, outf=None):
        """ Selects the source of the TM4 BNC output.

        Parameters:
            outx (int): output source.
                0: Mux1
                1: Mux2
                2: 1PP2C or Custom-1
                3: Custom-2
                4: Custom-3
                5: POP
                6: Custom-4
                7: FILPPS
                8: GPSPPS
                9: Reserved
                10: Reserved
                None: OFF


        """
        self.command('28',
                     'F' if outa is None else outa,
                     'F' if outb is None else outb,
                     'F' if outc is None else outc,
                     'F' if outd is None else outd,
                     'F' if oute is None else oute,
                     'F' if outf is None else outf)


    # Get commands

    def get_method_for(self, reply):
        """Return the method that can process the specified reply string.
        """
        cmd = reply.rstrip().split(',')[0] # remove trailing spaces or CR or LF
        if not cmd.startswith('#'):
            raise IOError(f'{self!r}: Invalid reply format {reply}')
        cmd = cmd[1:]
        if cmd not in self.get_methods:
            raise IOError(f'{self!r}: Unknown reply code {reply}')
        else:
            return self.get_methods[cmd]

    def get_date_time(self, reply=None, metrics=None):
        """Return the current date and time.

        Returns:
            datetime: date and time as a Python datetime object
        """
        date, time_ = self.query('51', reply)

        t = datetime.datetime(
            int(date[4:]),  int(date[:2]), int(date[2:4]), # year, month, day
            int(time_[:2]), int(time_[2:4]), int(time_[4:6])) # hours, minutes, seconds

        self.last_gps_time = calendar.timegm(t.timetuple())
        self.add_metric(metrics, 'gps_time', value=self.last_gps_time, type='gauge')
        self.add_metric(metrics, 'gps_time_diff', value=(time.time() - self.last_gps_time), type='gauge')
        return t

    def get_position(self, reply=None, metrics=None):
        """Return the current position, GPS availability and numer of satellites used.

        Returns:
            (lat, lon, avail, n_sat) tuple:
                lat (float): latitude in fractional degrees
                lon (float): longitude in fractional degrees
                avail (bool): GPS availability (0=unavailable, 1=available)
                n_sat (int): number_of_satellites (0-12)
        """
        lat, ns, lon, ew, avail, n_sat = self.query('52', reply)

        return (
            (float(lat[:2]) + float(lat[2:]) / 60) * (-1 if ns == 'S' else 1),
            (float(lon[:3]) + float(lon[3:]) / 60) * (-1 if ew == 'W' else 1),
            bool(int(avail)),
            int(n_sat, 16))

    def get_altitude(self, reply=None, metrics=None):
        """Return the current altitude.

        Returns:
            float: signed altitude in meters
        """
        alt, units = self.query('53', reply)
        assert units=='M', 'Units are not in meters'
        return float(alt)

    def get_mask_angle(self, reply=None, metrics=None):
        """Return the current mask_angle.

        Returns:
            int: mask angle code: 0 (5 deg), 1 (15 deg) or 2 (20 deg)
        """
        angle_code, datum = self.query('55', reply)
        assert datum == '47', 'datum is not WGS84'
        mask_angle = int(angle_code)
        self.add_metric(metrics, 'gps_mask_angle', value=mask_angle, type='gauge')
        return mask_angle

    def get_user_time_bias(self, reply=None, metrics=None):
        """Return the current user time bias.

        Returns:
            int: time bias in ns
        """
        (bias, ) = self.query('56', reply)
        time_bias = int(bias)
        self.add_metric(metrics, 'gps_user_time_bias', value=time_bias)
        return time_bias

    def get_timing_mode(self, reply=None, metrics=None):
        """Return the current timing mode.

        Returns:
            int: timing mode:
                0: Dynamic Timing Mode
                1: Static Timing Mode
                3: Auto Survey Mode
        """
        (mode, ) = self.query('57', reply)
        timing_mode = int(mode)
        self.add_metric(metrics, 'gps_timing_mode', value=timing_mode)
        return timing_mode

    def get_geometric_quality_and_almanac_status(self, reply=None, metrics=None):
        """Return the geometric quality (GQ) and almanac status.

        Returns:
            (gq, almanac_status) tuple where:
                gq (int): geometric quality (0-9)
                almanac_status (int): 0: OK, 1: no almanac, 2: almanac is old
        """
        gq, almanac_status = self.query('59', reply)
        gq, almanac_status = int(gq), int(almanac_status)
        self.add_metric(metrics, 'gps_geometric_quality', value=gq)
        self.add_metric(metrics, 'gps_almanac_status', value=almanac_status)
        return gq, almanac_status

    def get_oscillator_tuning_mode(self, reply=None, metrics=None):
        """Return the oscillator tuning mode.

        Returns:
            osc_tuning_mode (int):
                1: oscillator warm-up
                2: course adjust
                3: course adjust standby
                4: fine adjust
                5: fine adjust hold)
        """
        (osc_tuning_mode, ) = self.query('64', reply)
        osc_tuning_mode = int(osc_tuning_mode)
        self.add_metric(metrics, 'gps_osc_tuning_mode', value=osc_tuning_mode)
        return osc_tuning_mode

    def get_alarm_status(self, reply=None, metrics=None):
        """Return the coast, antenna and 10 MHz alarm status.

        Returns:
            (coast_alarm, antenna_alarm, clk_alarm) tuple where:
                coast_alarm (bool): coast alarm
                antenna_alarm (bool): antenna alarm
                clk_alarm (bool): 10 MHz output alarm
        """
        coast_alarm, antenna_alarm, clk_alarm = self.query('65', reply)
        coast_alarm, antenna_alarm, clk_alarm = bool(int(coast_alarm)), bool(int(antenna_alarm)), bool(int(clk_alarm))
        self.add_metric(metrics, 'gps_coast_alarm', value=coast_alarm)
        self.add_metric(metrics, 'gps_antenna_alarm', value=antenna_alarm)
        self.add_metric(metrics, 'gps_10MHz_alarm', value=clk_alarm)
        return coast_alarm, antenna_alarm, clk_alarm

    def get_mux1_output_source(self, reply=None, metrics=None):
        """Return the mux output source.

        Returns:
                mux1 (int): Mux 1 source:
                    0 for 10 MHz output
                    1 for 5 MHz output
                    2 for 1 MHz output
                    3 for 100 kHz output
                    4 for 10 kHz output
                    5 for 1 kHz output
                    6 for IRIG output (if installed)
                    7 for PPS output
                    8 for OFF (newer TM-4's only)
        """
        time_port_baud_rate, mux1, unknown = self.query('60', reply) # undocumented 'unknown' parameter ('+00')
        mux1 = int(mux1)
        self.add_metric(metrics, 'gps_mux1_source', value=mux1)
        return mux1

    def get_timing_status(self, reply=None, metrics=None):
        """Return the timing status.

        Returns:
                status (int): 0: time not valid; 1: time valid
        """
        (status, ) = self.query('61', reply)
        status = int(status)
        self.gps_time_valid = status

        self.add_metric(metrics, 'gps_timing_status', value=status)
        return status


    def get_mux2_output_source(self, reply=None, metrics=None):
        """Return the mux output source.

        Returns:
            mux2 (int): Mux 2 source
                0 : 10 MHz output
                1 : Mux1 mirror
                2 : PPS
                3 : output option 1
                4 : output option 2
                5 : output option 3
                6 : baseband IRIG (if installed)
                7 : baseband NASA-36 (if installed)
                8 : OFF (newer TM-4's only)
        """
        (mux2, ) = self.query('68', reply)
        mux2 = int(mux2)
        self.add_metric(metrics, 'gps_mux2_source', value=mux2)
        return mux2

    def get_tracking_channel_status(self, reply=None, metrics=None):
        """Return the status of each satellite.

        Returns:
            (statellite_status_map ,  receiver_status) tuple where:

                satellite_status_map = {satellite_prn: {constellation_status: x, tracking_status: y, signal_quality:v, ephemeris_status: z},...}
                satellite_prn (int): satellite id
                constellation_status (int): constellation status ( 0/1 = not included/included in current constellation)
                tracking_status (str): tracking status (A = acquisition/reacquisition, S = searching, 0-9 = SQ)
                signal_quality (int): tracking status in numeric format: -2: searching, -1: acquisition/reaquisition, 0-9: signal quality
                ephemeris_status (int): 0/1 not collected/collected
                receiver_status (int):

                    2 = search the sky
                    3 = Almanac collect
                    4 = Ephemeris collect
                    5 = acquisition
                    6 = position
        """
        s = self.query('69', reply)
        satellite_status_map = NameSpace()
        while len(s) >= 4:
            prn, cs, ts, es = s[:4]
            satellite_status_map[int(prn)] = NameSpace(
                constellation_status = int(cs),
                tracking_status = ts,
                signal_quality = -2 if ts=='S' else -1 if ts=='A' else int(ts),
                ephemeris_status = int(es))
            s = s[4:]
        receiver_status = int(s[0])

        self.add_metric(metrics, 'gps_receiver_status', value=receiver_status)
        for sat_number, sat_info in satellite_status_map.items():
            self.add_metric(metrics, 'gps_constellation_status', satellite_number=sat_number, value=sat_info.constellation_status)
            self.add_metric(metrics, 'gps_signal_quality', satellite_number=sat_number, value=sat_info.signal_quality)
            self.add_metric(metrics, 'gps_ephemeris_status', satellite_number=sat_number, value=sat_info.ephemeris_status)

        return satellite_status_map, receiver_status


    def get_serial_time_code_format(self, reply=None, metrics=None):
        """Return the current serial time code format.

        Returns:
            int: timing mode:
                0: IRIG-B002/B122 (no BCD year)
                1: NASA-36
                3: IRIG-B007/B127 (with BCD year & SBS)
        """
        (mode, ) = self.query('71', reply)
        time_code_format = int(mode)
        self.add_metric(metrics, 'gps_serial_time_code_format', value=time_code_format)
        return time_code_format


    def get_speed_and_heading(self, reply=None, metrics=None):
        """Return the current speed and heading

        Returns:
            (speed, heading) tuple:
                speed (float): speed in m/s
                heading (float): heading in decimal degrees
        """
        speed, heading = self.query('75', reply)
        return (float(speed), float(heading))


    def get_nmea_info(self, reply=None, metrics=None):
        """Return higher precision position, speed and course.

        Returns:
            NameSpace containing:
                lat (float): latitude in fractional degrees
                lon (float): longitude in fractional degrees
                alt (float): altitude in meters
                fix (bool): GPS fix validity (0=not valid, 1=valid)
                n_sat (int): number_of_satellites (0-12)
                h_dilution: horizontal dilution (0 - 99.9)
                speed (float): speed over ground in knots,
                course (float): course in degrees
        """
        lat, ns, lon, ew, alt, alt_units, fix, n_sat, h_dil, speed, course = self.query('76', reply)

        info = NameSpace(
            lat = (float(lat[:2]) + float(lat[2:]) / 60) * (-1 if ns == 'S' else 1),
            lon = (float(lon[:3]) + float(lon[3:]) / 60) * (-1 if ew == 'W' else 1),
            alt = float(alt),
            fix = bool(fix),
            n_sat = int(n_sat),
            h_dilution = float(h_dil),
            speed = float(speed),
            course = float(course))

        self.add_metric(metrics, 'gps_latitude', value=info.lat)
        self.add_metric(metrics, 'gps_longitude', value=info.lon)
        self.add_metric(metrics, 'gps_altitude', value=info.alt)
        self.add_metric(metrics, 'gps_fix_valid', value=info.fix)
        self.add_metric(metrics, 'gps_number_of_satellites', value=info.n_sat)
        self.add_metric(metrics, 'gps_horiz_dilution', value=info.h_dilution)
        self.add_metric(metrics, 'gps_speed', value=info.speed)
        self.add_metric(metrics, 'gps_course', value=info.course)
        return info

    def get_user_options(self, reply=None, metrics=None):
        """Return the current antenna alarm elable status and the PPS source

        Returns:

            (antenna_alarm_enable, pps_source) tuple:

                antenna_alarm_enable (bool): antenna alarm is enabled
                pps_source (int): PPS source unitl Time valid/Initial phase lock/Beyond lock

                   0: LOW/GPSPPS/FILPPS
                   1: LOW/LOW/FILPPS
                   2: LOW/GPSPPS/GPSPPS
                   4: GPSPPS/GPSPPS/GPSPPS
        """
        r = self.query('78', reply)
        aa_enabled, pps_source = (bool(r[0]), int(r[1]))
        self.add_metric(metrics, 'gps_antenna_alarm_detection_enabled', value=aa_enabled)
        self.add_metric(metrics, 'gps_pps_source', value=pps_source)
        return aa_enabled, pps_source

    def get_coast_timer(self, reply=None, metrics=None):
        """Return the  Amount of time that the unit has been in Coast (Mode 3 or Mode 5)

        Returns:
            coast_time (float): in fractional hours
        """
        # actual reply is ['#79', '00000000', '05335027', '02902627', '00000000', '4']
        # _, time = self.query('79')
        # return float(time[:4]) + float(time[4:6])/60 + float(time[6:])/3600
        coast_timer_values = self.query('79', reply)
        coast_timer_values = [int(c) for c in coast_timer_values]
        # self.add_metric(metrics, 'gps_coast_time', value=coast_time)
        for i, c in enumerate(coast_timer_values):
            self.add_metric(metrics, 'gps_coast_time', field=i, value=c)
        return coast_timer_values

    def get_phase_lock_status(self, reply=None, metrics=None):
        """Return the current phase lock status.

        Returns:
            int: phase lock status:
                0: OCXO warm-up (OSC mode: 1, Phase lock state: NO)
                1: Coarse OCXO tuning (OSC mode:2, Phase lock state: NO)
                2: Entered Coast condition during Mode 2 tuning (OSC mode:3, Phase lock state: NO)
                3: Fine tuning OCXO, waiting for phase lock. (OSC mode:4, Phase lock state: NO)
                4: Fine tuning OCXO, approaching phase lock (OSC mode:4, Phase lock state: NO)
                5: Entered Coast condition during Mode 4 tuning (OSC mode:5, Phase lock state: NO)
                9: Phase Lock Achieved (OSC mode:4, Phase lock state: YES)
        """
        (status, ) = self.query('80', reply)
        phase_lock_status = int(status)
        self.add_metric(metrics, 'gps_phase_lock_status', value=phase_lock_status)
        return phase_lock_status

    def get_leap_seconds(self, reply=None, metrics=None):
        """
        Return the number of Leap Seconds that have been introduced to UTC Time since the beginning
        of GPS Time.

        The method also receives whether the unit is using GPS or UTC time,
        but the value is not returned (but is added to the metrics).

        Returns:
            valid (bool): leap seconds info is valid
            leap_seconds(int): number of leap seconds
        """
        is_utc_time, valid, leaps  = self.query('81', reply)
        is_utc_time = bool(int(is_utc_time))
        leap_seconds_valid = bool(int(valid))
        leap_seconds = int(leaps)
        self.gps_leap_seconds = None if not leap_seconds_valid else 0 if is_utc_time else leap_seconds
        self.add_metric(metrics, 'gps_is_utc_time', value=int(is_utc_time))
        self.add_metric(metrics, 'gps_leap_seconds_valid', value=leap_seconds_valid)
        self.add_metric(metrics, 'gps_leap_seconds', value=leap_seconds)
        return leap_seconds_valid, leap_seconds

    def poll_metrics(self):
        metrics = Metrics()
        with self.socket(flush=True):
            if self.polling_mode != 1:
                self.set_polling_mode(1)
            for command, get_method in self.get_methods.items():
                if get_method:
                    try:
                        get_method(metrics=metrics)
                    except IOError:
                        self.log.warning(f'{self!r}: Could not get reply for command {command}')
        return metrics

    def get_broadcast_metrics(self):
        metrics = Metrics()
        with self.socket():# don't flush, data is presumably constantly coming in
            if self.polling_mode != 0:
                self.set_polling_mode(0)
            self.send('\r\n')  # provoke an error if the connection is broken
            #print('Cumulative buffer before is: %r\n\n' % self.buffer)
            for i in range(10):
                # process whatever replies are in the buffer until all is left are partial commands
                # try to get new replies to complete partials command. If there are none,
                try:
                    reply = self.recv(timeout = 1.2) # must be <1 s because new data is coming every second and we'll never get out of here
                    self.buffer += reply
                    #print('got %i bytes: %r\n' % (len(reply), reply))
                    if len(reply) < 1000:
                         break
                except IOError: # there was no data, this must be the end
                    break
            #print('Cumulative buffer after is: %r\n' % self.buffer)
            while True:
                    # Remove anything up to '#' in case we got a partial buffer
                    if not self.buffer.startswith('#'):
                        pos = self.buffer.find('#')
                        if pos >=0:
                            self.buffer = self.buffer[pos:]
                            #print('chopped beginning of buffer to %r\n' % self.buffer[:10])
                    # find a string up to \r\n
                    pos = self.buffer.find('\r\n')
                    if pos <=0: # if there is not complete string, give up for now
                        break
                    reply = self.buffer[:pos+2]
                    self.buffer = self.buffer[pos+2:]
                    self.log.debug(f'Got broadcast string {reply!r}')
                    get_method = self.get_method_for(reply)
                    if get_method:
                        get_method(reply=reply, metrics=metrics)
                    #break
        #print('Parsed %i metrics' % len(metrics.metrics))
        return metrics

    def configure_gps(self, location='DRAO', lat=None, lon=None, alt=None):
        """
        Configure the GPS for standard CHIME operations.

        The GPS is put in 'static' mode, where it tries only to get time information and not the
        position infromation. This requires less satellites and presumably provides for a more
        stable time signal. In this mode a static position is given to the GPS so it will know what
        satellites to search for.

        The default locations can be passed by name in the `location` parameter, or if `location` is `None`,


        Valid location strings are:

            'DRAO': The default position is the center of the CHIME array at
                DRAO, Penticton, BC, Canada.

            'McGill': The location of the GPS antenna at the McGill Rutherford building, Montreal, Canada

        """
        if bool(location) == bool(lat is not None or lon is not None or alt is not None):
            raise ValueError("You must specify either the location name, or specify the 'lat', 'lon' and 'alt'")

        if location is None:
            if lat is None or lon is None or alt is None:
                raise ValueError(" You must specify `lat', 'lon' and 'alt'")
        elif location == 'DRAO':
            lat = 49.320683333333335
            lon = -119.62329666666666
            alt = 562.0
        elif location == 'McGill':
            lat = 45.507100
            lon = -73.579128
            alt = 92.5
        else:
            raise ValueError(f"Invalid location '{location}'. Valid locations are 'DRAO' or 'McGill'")

        with self.socket(flush=True):
            self.set_polling_mode()
            self.enable_ntp_output(False)  #Not needed, causes the unit to do extra processing and affects latency
            self.set_mask_angle(0)
            self.set_time_format(1) # UTC time
            self.set_timing_mode(1) # Static. Position is set below.
            self.set_serial_time_code_format(2) # IRIG-B007, including BCD year and SBS
            self.set_position(lat, lon, alt)
            self.set_pps_output_source(1) # FILPPS only when fully locked
            self.set_bnc_output_source(0,1,7) # OutA=Mux1, OutB=Mux2, OutC=FILPPS
            self.set_multiplexer_output_source(6, 0) # Mux1=IRIGB, Mux2= 10 MHz
            self.set_user_time_bias(0)
            self.set_antenna_alarm_enable(True)
            pass

class GPSAsyncRESTServer(AsyncRESTServer):
    """
    REST interface for receiver hut GPS.
    """

    DEFAULT_PORT = 54325

    def __init__(self,  address='', port=DEFAULT_PORT, logging_params={}):
        """ Create a GPS Metrics server.
        """
        self.gps = {}
        super().__init__(address=address, port=port, heartbeat_string='Gs')
        self.metrics_queue = queue.Queue(1000)
        self.metrics = Metrics(latest_only=True)
        self.add_periodic_callback(self._get_metrics, 1000, stop_on_errors=False)
        self.startup_time = datetime.datetime.utcnow()



    async def _get_metrics(self):
        """ get the metrics from the GPS units and put them in the queue
        """
        for gps_name, gps in self.gps.items():
            self.log.info(f'{self!r}: Getting metrics for GPS {gps_name}')
            try:
                m = gps.get_broadcast_metrics()
                self.metrics.add(m, gps_name=gps_name)
                self.log.info(f'Got {len(m.metrics)} metrics')
            except IOError as e:
                self.log.warning(f'{self!r}: Error while trying to access metric from {gps_name}\nThe error is:\n{e!r}')
                # raise
        self.log.info(f'Queue has {len(self.metrics)} metrics blocks')

    ##################
    # Server commands
    ##################


    @endpoint('start')
    async def start(self, **config):
        """ Start the GPS server with provided config
        """
        print('Starting GPS server')
        self.log.info(f'{self!r}: Received start command')
        if self.gps:
            raise RuntimeError(f'{self!r}: GPS server is already started')

        # Register config with comet broker
        try:
            enable_comet = config['comet_broker']['enabled']
        except KeyError:
            msg = "Missing config value 'comet_broker/enabled'."
            self.log.error(msg)
            return msg
        if enable_comet:
            if comet is None:
                msg = "Failure importing comet for configuration tracking.  Please install the " \
                      "comet package or set 'comet_broker/enabled' to False in config."
                self.log.error(msg)
                return msg
            try:
                comet_host = config['comet_broker']['host']
                comet_port = config['comet_broker']['port']
            except KeyError as exc:
                msg = f"Failure registering initial config with comet broker: 'comet_broker/{exc}' " \
                      f"not defined in config."
                self.log.error(msg)
                return msg
            comet_manager = comet.Manager(comet_host, comet_port)
            try:
                comet_manager.register_start(self.startup_time, __version__)
                comet_manager.register_config(config)
            except comet.CometError as exc:
                msg = f'Comet failed registering GPS server start and initial config: {exc}'
                self.log.error(msg)
                return msg
        else:
            self.log.warning("Config registration DISABLED. This is only OK for testing.")

        self.config = NameSpace(config)
        units = self.config.units or {}
        for name, params in units.items():
            self.log.debug(f'{self!r}: Creating GPS handler {name}')
            gps = SpectrumInstrumentsTM4D(**params)
            self.gps[name] = gps
            print(f'Creating gps {gps}')
        return 'GPS server started'

    @endpoint('stop')
    async def stop(self):
        if not self.gps:
            self.log.warning(f'{self!r}: GPS server is not started')
        else:
            self.gps = {}
        return 'GPS server stopped'

    @endpoint('test')
    async def test(self, x=1):
        y = x + 1
        return y

    @endpoint('list-names')
    async def listNames(self):
        self.log.info(f'{self!r}: Received list names request')
        return list(self.gps.keys())

    @endpoint('get-monitoring-data')
    async def monitoringMetrics(self):
        self.log.info(f'{self!r}: Received monitoring metrics request')
        self.log.info(f'{self!r}: sending {len(self.metrics.metrics)} metrics')
        return aiohttp.web.Response(text=str(self.metrics.pop()))


#########################################
# GPS REST client
#########################################

class GPSAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote RawAcq server.

    This client is used by ch_master to start, configure and operate all the RawAcq servers in the array.

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

        ps_names (list of str): list of GPS names on which this client will operate. Other
            supplies will not be affected.
    """
    DEFAULT_PORT = GPSAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname='localhost', port=DEFAULT_PORT):
        super().__init__(
            hostname=hostname, port=port,
            server_class=GPSAsyncRESTServer,
            heartbeat_string='Gc')



    async def start(self, **config):
        """ If the GPS remote server is not started, start it with the specified configuration

        Parameters:

            config (str or dict): If a string, the configuration is loaded from the specified
                configuration file and name. if a dict, it is passed directly to the server.

        """
        #print('start!')
        self.log.info(f'{self!r}: Starting remote GPS server at {self.hostname}:{self.port} with config: {config!r}')

        if isinstance(config, str):
            config = load_yaml_config(config)
        result = await self.post('start', **config)
        return 'GPS server started'

    async def stop(self):
        result = await self.get('stop')
        return result

    async def test(self, **args):
        result = await self.post('test', **args)
        return result

    async def list_names(self):
        result = await self.get('list-names')
        return result


def main():
    """ Command-line interface to launch and operate the GPS server.
    """
    # Setup logging
    log.setup_basic_logging('WARN')
    client, server = run_client(
        sys.argv[1:], GPSAsyncRESTServer, GPSAsyncRESTClient,
        object_name ='GPS', server_config_path='gps.servers')
    return client, server

if __name__ == '__main__':
    client, server = main()
