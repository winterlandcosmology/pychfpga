#!/usr/bin/env python
"""
Control module for CHIME receiver hut power supplies, including REST interface.

"""

import logging
import numpy
import os
import sys
import argparse
import requests
import time
import json

import tornado
import tornado.tcpclient
import tornado.web

from pychfpga.Agilent_N5764A import AgilentN5764AHandler
from pychfpga import Metric, NameSpace, load_yaml_config
from rest import AsyncRESTClient, AsyncRESTServer, endpoint, coroutine, coroutine_return, sleep, IOLoop  # generic REST servers and clients


class PowerSupplyRESTServer(AsyncRESTServer):
    """
    REST interface for receiver hut power supplies. Work in progress
    """

    # TODO: Look into appropriate default port / address
    DEFAULT_PORT = 33221

    POWER_SUPPLY_CLASSES = {
        'AgilentN5764': AgilentN5764AHandler,
        'AgilentN8731': AgilentN5764AHandler
        }

    def __init__(self, power_supplies, address='', port=DEFAULT_PORT, ps_address='', ps_port=5025, logging_params={}):
        """ power_supplies list of dict with entries 'type', 'name', and 'hostname'
        """
        self.log = logging.getLogger(__name__).getChild(self.__class__.__name__)
        self.power_supplies = {}
        for ps in power_supplies:
            type_ = ps.pop('type')
            name = ps.pop('name')
            cls = self.POWER_SUPPLY_CLASSES[type_]
            self.power_supplies[name] = cls(**ps)
            try:
                self.power_supplies[name].open()
            except Exception as e:
                self.log.error("Could not connect to power supply {}:\n{}".format(name, repr(e)))
                self.power_supplies.pop(name)

        super(PowerSupplyRESTServer, self).__init__(address=address, port=port)

    def _parse_names(self, ps_names):
        if ps_names is None:
            ps_names = self.power_supplies.keys()
        else:
            ps_names = [ pn.strip() for pn in ps_names.split(',') ]
            for pn in ps_names:
                if not pn in self.power_supplies.keys():
                    raise RuntimeError("Unknown power supply {}".format(pn))
        return ps_names

    @coroutine
    @endpoint
    def listNames(self, handler):
        self.log.info('%.32r: Received list names request' % self)
        coroutine_return(self.power_supplies.keys())

    @coroutine
    @endpoint
    def powerOn(self, handler, ps_names=None):
        ps_names = self._parse_names(ps_names)
        self.log.info('%.32r: Received power on command for %r' % (self, ps_names))
        for pn in ps_names:
            ps = self.power_supplies[pn]
            if ps.ps.output()['PowerEnabled']:
                self.log.warning("Power ouput already enabled for {}".format(pn))
            else:
                ps.unlock()
                ps.power_on()
                ps.lock()

    @coroutine
    @endpoint
    def powerOff(self, handler, ps_names=None):
        ps_names = self._parse_names(ps_names)
        self.log.info('%.32r: Received power off command for %r' % (self, ps_names))
        for pn in ps_names:
            ps = self.power_supplies[pn]
            if not ps.ps.output()['PowerEnabled']:
                self.log.warning("Power ouput already disabled for {}".format(pn))
            else:
                ps.unlock()
                ps.power_off()
                ps.lock()

    @coroutine
    @endpoint
    def status(self, handler, ps_names=None):
        ps_names = self._parse_names(ps_names)
        self.log.info('%.32r: Received status request for %r' % (self, ps_names))
        stati = { }
        for pn in ps_names:
            stati[pn] = self.power_supplies[pn].status()['status']
            self.log.info('%.32r:     Status of %s %s' % (self, pn, stati[pn]))
        coroutine_return(stati)

    @coroutine
    @endpoint
    def monitoringMetrics(self, handler):
        self.log.info('%.32r: Received monitoring metrics request' % self)
        metrics = [ ]
        for ps_name, ps in self.power_supplies.items():
            status = ps.status()
            metrics.append(Metric('power_supply_voltage', name=ps_name, value=status['voltage'], type='gauge'))
            metrics.append(Metric('power_supply_current', name=ps_name, value=status['current'], type='gauge'))
            metrics.append(Metric('power_supply_power', name=ps_name, value=status['power'], type='gauge'))
            metrics.append(Metric('power_supply_status', name=ps_name, value=int(status['status']=='OK'), type='gauge'))
        coroutine_return([ str(m) for m in metrics ])


class PowerSupplyEasyRESTClient(object):
    def __init__(self, hostname='localhost', port=PowerSupplyRESTServer.DEFAULT_PORT):
        self.port = port
        self.host = hostname
        self.url = "http://{}:{:d}/".format(self.host, self.port)
        print "Connected to server at {}".format(self.url)

    def check_code(self, code):
        if not code == 200:
            raise RuntimeError("Got code {:d} from server at {}:{:d}".format(code, self.host, self.port))

    def listNames(self):
        print "Requesting list of power supply names..."
        response = requests.get(self.url + "listNames")
        self.check_code(response.status_code)
        return response.json()

    def powerOn(self, ps_names=None):
        print "Sending power on command..."
        response = requests.post(self.url + "powerOn", data={'ps_names': ps_names})
        self.check_code(response.status_code)
        print "Successfully sent power on!"

    def powerOff(self, ps_names=None):
        print "Sending power off command..."
        response = requests.post(self.url + "powerOff", data={'ps_names': ps_names})
        self.check_code(response.status_code)
        print "Successfully sent power off!"

    def status(self, ps_names=None):
        print "Requesting status..."
        response = requests.post(self.url + "status", data={'ps_names': ps_names})
        self.check_code(response.status_code)
        print "Status: {}".format(response.json())
        return response.json()

    def monitoringMetrics(self):
        print "Requesting monitoring metrics..."
        response = requests.get(self.url + "monitoringMetrics")
        self.check_code(response.status_code)
        return response.json()


def parse_cmdline_args(argv):
    parser = argparse.ArgumentParser(description="ps: Receiver hut power supply control server", epilog="""
        """)
    parser.add_argument('args', type=str, choices=['client', 'server'], default='',  help='"server" or "client" ')
    parser.add_argument('-p', '--port', default=33221, type=int, help="Server port")
    parser.add_argument('-n', '--host', default='localhost', type=str, help="Server hostname")
    parser.add_argument('--config', default=None, type=str, help="Config in YAML filename:subconfig form. Will load the ERH FLA supply by default.")
    parser.add_argument('--logfile', default=None, type=str, help="Record logs in this file")
    return parser.parse_args(argv)

if __name__ == '__main__':
    """
    Stolen from raw_acq!

    Command-line interface to start power supply REST server
        ps.py server --port 33221 # starts the server on localhost.
        ps.py client --port 33221 --host localhost # starts a client in variable 'rc' to operate the server at localhost:33221

    Default port is 33221 if not specified.
    """
    args = parse_cmdline_args(sys.argv[1:])

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG) # pass all messages to the handlers
    logger.handlers = []  # clear all existing handlers
    # formatter = logging.Formatter(self.LOG_FORMAT, self.LOG_DATE_FORMAT)

    def add_handler(h, log_level):
        # h.setFormatter(formatter)
        level = log_level if isinstance(log_level, int) else log_level.upper()
        h.setLevel(level)
        logger.addHandler(h)

    add_handler(logging.StreamHandler(sys.stderr), 'warning')
    add_handler(logging.handlers.SysLogHandler(), 'debug')
    if args.logfile:
        add_handler(logging.FileHandler(args.logfile), 'debug')


    ioloop = IOLoop()
    ioloop.make_current()

    power_supplies = None
    if args.config is not None:
        config = load_yaml_config(args.config)
        if 'power_supplies' in config.keys():
            power_supplies = config['power_supplies']
    if power_supplies is None:
            power_supplies = [{'type': 'AgilentN8731', 'name': 'FLA_east', 'hostname': '10.0.0.40'}]# 'A-N8731A-1553P'}]

    first_arg = args.args.lower()
    if first_arg == 'server':
        rs = PowerSupplyRESTServer(power_supplies=power_supplies, port=args.port, address=args.host)
        print("Power supply REST Server started. Waiting for REST commands.")
        ioloop.start()
        print("\nI'm done. Bye!")
    elif first_arg == 'client':
        rc = PowerSupplyEasyRESTClient(hostname=args.host, port=args.port)
