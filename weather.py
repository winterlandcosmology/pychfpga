#!/usr/bin/env python
"""
REST Server and clients for allowing the Prometheus to access the DRAO weather data gathered by ``wview``.

"""

import sys
import sqlite3
import numpy as np

import log  # logging helper functions
from pychfpga import Metrics, NameSpace
from rest import AsyncRESTClient, AsyncRESTServer, endpoint
from rest import coroutine, coroutine_return, sleep, IOLoop
from rest import RunSyncWrapper, SocketContext, run_client  # generic REST servers and clients

dataset = {    "barometer": {"type": "pressure", "units": "hPa"},
                "pressure": {"type": "pressure", "units": "hPa"},
               "altimeter": {"type": "pressure", "units": "hPa"},
                  "inTemp": {"type": "temperature", "units": "deg C"},
                 "outTemp": {"type": "temperature", "units": "deg C"},
              "inHumidity": {"type": "percent", "units": "%"},
             "outHumidity": {"type": "percent", "units": "%"},
               "windSpeed": {"type": "speed", "units": "km/h"},
                 "windDir": {"type": "direction", "units": "deg"},
                "windGust": {"type": "speed", "units": "km/h"},
             "windGustDir": {"type": "direction", "units": "deg"},
                "rainRate": {"type": "rate", "units": "mm/h"},
                    "rain": {"type": "amount", "units": "mm"},
                "dewpoint": {"type": "temperature", "units": "deg C"},
               "windchill": {"type": "temperature", "units": "deg C"},
               "heatindex": {"type": "temperature", "units": "deg C"}}

def get_wview_metrics(db_path='/var/lib/wview/archive/wview-archive.sdb'):
    """ Return a Metrics object containing the most recent entry of the wview sqlite database specified in `db_path`.

    Parameters:

        db_path (str): full path and filename where the ``wview`` SQLLite database can be found.

    Returns:

        Metrics object.

    Notes:
        - The Metrics is empty if the data does not have a new timestamp compared to the last call.
        - The metrics contains only the latest entry. We could envision returning all the entries
          since the last call, so we could access the database less often.
    """

    # Figure out the starting UNIX time.
    # t_start = int(datetime.datetime.strptime(arg.date, "%Y%m%d").strftime("%s"))
    # t_start = time.time()
    # t_end = t_start + 600
    # Get the data.
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    # cur.execute("SELECT dateTime FROM archive ORDER BY dateTime LIMIT 1;")
    # t_first = cur.fetchone()[0]
    col_names = dataset.keys()
    col_string = ",".join(["dateTime", "usUnits"] + col_names)
    # cur.execute("SELECT %s FROM archive WHERE dateTime BETWEEN %d AND %d " \
    #             "ORDER BY dateTime;" % (col, t_start, t_end))
    cur.execute("SELECT %s FROM archive ORDER BY dateTime DESC LIMIT 1;" % (col_string))
    data = np.asarray(cur.fetchall(), dtype=float)
    db.close()

    metrics = Metrics(type='GAUGE')
    # Check if "usUnits" is true; if so, convert from Imperial to metric units.
    for i in range(data.shape[0]):
        time_ = data[i, 0]
        are_us_units = data[i, 1]
        for j in range(2, data.shape[1]):
            value = data[i, j]
            if not value:
              continue
            metric_name = col_names[j-2]
            type_ = dataset[metric_name]["type"]
            units = dataset[metric_name]["units"]
            if are_us_units: # if US units, convert to metric
                if type_ == "pressure":
                    value = value * 33.86389 # inHg to hPa
                elif type_ == "temperature":
                    value = (value - 32.0) * 5.0 / 9.0 # F to C
                elif type_ == "speed":
                    value = value * 1.60934 # mi/h to km/h
                elif type_ == "amount" or type_ == "rate":
                    value = value * 25.4 # inch to mm
            metrics.add('weather_%s' % metric_name, value=value, units=units, time=time_ * 1000)
    return metrics


class WeatherAsyncRESTServer(AsyncRESTServer):
    """
    REST server that publishes ``wview`` weather server data as Prometheus-compatible metrics.
    """

    DEFAULT_PORT = 54325

    def __init__(self,  address='', port=DEFAULT_PORT, logging_params={}):
        """
        """
        super(WeatherAsyncRESTServer, self).__init__(address=address, port=port, heartbeat_string='Gs')
        self.last_time = None
        self.config = None

    ##################
    # Server commands
    ##################

    @coroutine
    @endpoint('start')
    def start(self, handler, **config):
        """ Start the Weather server with provided config
        """
        self.log.info('%r: Received start command' % self)
        self.config = NameSpace(config)
        coroutine_return('Weather server started')

    @coroutine
    @endpoint('stop')
    def stop(self, handler):
        self.config = None
        coroutine_return('Wheather server server stopped')

    @coroutine
    @endpoint('get-monitoring-data')
    def monitoringMetrics(self, handler):
        self.log.info('%.32r: Received monitoring metrics request' % self)
        metrics = Metrics()
        if self.config:
            for unit_name, unit_config in self.config.units.items():
                m = get_wview_metrics(unit_config.db_path)
                #print(m.metrics.items()[0][1]['entries'][0])
                if m:
                    new_time = m.metrics.items()[0][1]['entries'][0]['time']
                    if new_time != self.last_time:
                        metrics.add(m, name=unit_name)
                    self.last_time = new_time
        else:
            self.log.warning('%.32r: Weather server is not started!' % self)

        handler.set_header('Content-Type', 'text/plain')
        handler.write(str(metrics))

#########################################
# Weather REST client
#########################################

class WeatherAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified weather server.
    """
    DEFAULT_PORT = WeatherAsyncRESTServer.DEFAULT_PORT

    def __init__(self, hostname='localhost', port=DEFAULT_PORT):
        super(WeatherAsyncRESTClient, self).__init__(
            hostname=hostname, port=port,
            server_class= WeatherAsyncRESTServer,
            heartbeat_string='Gc')


    @coroutine
    def start(self, config):
        """ If the remote server is not started, start it with the specified configuration

        Parameters:

            config (str or dict): If a string, the configuration is loaded from the specified
                configuration file and name. if a dict, it is passed directly to the server.

        """
        #print('start!')
        self.log.info('%s: Starting remote Weather server at %s:%i with config: %r' % (self, self.hostname, self.port, config))

        if isinstance(config, str):
            config = load_yaml_config(config)
        result = self.post('start', **config)
        coroutine_return('Weather server started')

    @coroutine
    def stop(self):
        result = yield self.get('stop')
        coroutine_return(result)

def main():
    """ Command-line interface to launch and operate the Weather server.
    """
    # Setup logging
    log.setup_basic_logging('INFO')
    client, server = run_client(sys.argv[1:], WeatherAsyncRESTServer, WeatherAsyncRESTClient, object_name ='Weather', server_config_path='weather.servers')
    return client, server

if __name__ == '__main__':
    #pass
    client, server = main()
