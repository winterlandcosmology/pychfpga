"""Raw acquisition REST server."""

import datetime

import time
import aiohttp.web

from wtl import log
from wtl.metrics import Metrics
from wtl.rest import AsyncRESTServer, endpoint

try:
    import comet
except ImportError:
    comet = None

from pychfpga import __version__

from .receiver import RawAcqReceiver

class RawAcqAsyncRESTServer(AsyncRESTServer):
    """
    Asynchronous RawAcq REST server that operates Python-based multi-threaded UDP data receivers.

    Todo:
        - Setup logging.
    """

    DEFAULT_PORT = 54322

    def __init__(self, address='', port=DEFAULT_PORT, logging_params={}):
        self.receiver = RawAcqReceiver()
        super(RawAcqAsyncRESTServer, self).__init__(address=address, port=port, heartbeat_string='Rs')
        # self.add_periodic_callback(self.receiver.print_stats, 3000)

        # Add a callback to ping the raw_acq data sources periodically to ensure the switches
        # routing tables always know how to route the packets to their destination
        self.add_periodic_callback(self.receiver.ping_sources_async, 20000)

        # Start a periodic callback to check the ioloop response time as its health indicator
        self.add_periodic_callback(self.receiver.check_ioloop_response_time_async, 3000)
        self.startup_time = datetime.datetime.utcnow()

    async def shutdown(self):
        self.receiver.stop()

    @endpoint('start')
    async def start(self, **config):
        self.log.info(f'{self!r}: Received start command with {config}')
        if self.receiver.is_running():
            self.log.info(f'{self!r}: Receiver is already running. Stopping it and restarting a new one')
            self.receiver.stop()
            # raise RuntimeError('Server is already started')

        # Register config with comet broker
        comet_config = config.pop('comet_broker', {})
        enable_comet = comet_config.get('enabled', None)
        print(f'comet_broker={comet_config}, enabled={enable_comet}')
        if enable_comet is None:  # if the comet_broker.enable parameter is not specified
            msg = "Missing config value 'comet_broker.enabled'."
            self.log.error(msg)
            raise RuntimeError(f'Cannot start comet broker: {msg}')
        if enable_comet:  # if comet parameters are present and comet is is enabled
            if comet is None:
                msg = "Failure importing comet for configuration tracking.  Please install the " \
                      "comet package or set 'comet_broker/enabled' to False in config."
                self.log.error(msg)
                return msg
            try:
                comet_host = comet_config['host']
                comet_port = comet_config['port']
            except KeyError as exc:
                msg = f"Failure registering initial config with comet broker: 'comet_broker/{exc}' " \
                      "not defined in config."
                self.log.error(msg)
                raise RuntimeError(f'Cannot start comet broker: {msg}')
            comet_manager = comet.Manager(comet_host, comet_port)
            try:
                comet_manager.register_start(self.startup_time, __version__)
                comet_manager.register_config(config.copy())
            except comet.CometError as exc:
                msg = "Comet failed registering raw_acq start and initial config. " \
                      f"The Comet client returned the following error: {exc}"
                self.log.error(msg)
                raise RuntimeError(f'Cannot start comet broker: {msg}')
        else:
            self.log.warning("Config registration DISABLED. This is only OK for testing.")
        # config.pop('hostname', None)
        # config.pop('port', None)
        result = await self.receiver.start_async(**config)
        self.log.info(f'{self!r}: UDP receiver started. Returned {result!r}')
        return result

    @endpoint('stop')
    async def stop(self):
        if not self.receiver.is_running():
            self.log.warning(f'{self!r}: Server is not running')
        self.receiver.stop()
        return "stopped receiver"

    @endpoint('update-config')
    async def update_config(self, **config):
        self.log.info(f'{self!r}: received config update')
        self.receiver.update_config(config)
        return dict(
            adc_configured=self.receiver.processors.get("adc") is not None,
            fft_configured=self.receiver.processors.get("fft") is not None,
            corr_configured=self.receiver.processors.get("corr") is not None,
        )

    @endpoint('start-raw-hdf5')
    async def start_raw_hdf5(
            self,
            base_dir='./',
            base_filename='RawAcq',
            capture_duration=0,
            capture_refresh_time=0,
            elements_per_file=2048 * 64,
            capture_aux_enable=False,
            aux_base_dir='./',
            aux_base_filename='RawAcq_aux',
            capture_aux_refresh_time=0,
            capture_aux_stream_ids=[6],
            aux_elements_per_file=2048 * 64):
        self.receiver.processors["raw"].adc.start_adc_hdf5(
            base_dir=base_dir,
            base_filename=base_filename,
            capture_duration=capture_duration,
            capture_refresh_time=capture_refresh_time,
            elements_per_file=elements_per_file,
            capture_aux_enable=capture_aux_enable,
            aux_base_dir=aux_base_dir,
            aux_base_filename=aux_base_filename,
            capture_aux_refresh_time=capture_aux_refresh_time,
            capture_aux_stream_ids=capture_aux_stream_ids,
            aux_elements_per_file=aux_elements_per_file)
        return "started hdf5 writing to disk."

    @endpoint('stop-raw-hdf5')
    async def stop_raw_hdf5(self):
        self.receiver.processors["raw"].adc.stop_adc_hdf5()
        return "stopped hdf5 writing to disk."

    @endpoint('status')
    async def status(self):
        self.log.info(f'{self!r}: getting status request')
        return dict(started=self.receiver.is_running() if self.receiver else False)

    @endpoint('get-packets')
    async def get_packets(self):
        self.log.info(f'{self!r}: received get_packets command')
        await self.receiver.processors["raw"].adc.get_data_async()

    @endpoint('start-fft-rms')
    async def start_fft_rms(self, stream_ids=[], target_gain_bank=0, number_of_frames=100):
        self.log.info(f'{self!r}: received start_fft_rms command')
        await self.receiver.processors["raw"].fft.start_fft_rms_async(
            stream_ids=stream_ids,
            target_gain_bank=target_gain_bank,
            number_of_frames=number_of_frames)
        return

    @endpoint('get-fft-rms')
    async def get_fft_rms(self):
        self.log.info(f'{self!r}: received get_fft_rms command')
        ix, rms = await self.receiver.processors["raw"].fft.get_fft_rms_async()
        return (ix.tolist(), rms.tolist())

    @endpoint('start-corr-hdf5')
    async def start_corr_hdf5(self,
                              base_dir=None,
                              base_filename=None,
                              capture_duration=0,
                              capture_n_inputs=4,
                              autocorr_only=False,
                              elements_per_file=256,
                              frame0_irigb_time=0,
                              number_of_correlators=None,  # has to be provided
                              number_of_correlated_inputs= None,  # has to be provided
                              number_of_bins_per_frame=None,  # has to be provided
                              ):
        self.log.debug(f'{self!r}: Starting correlator data capture (calling receiver.processors["corr"].start_corr_hdf5)')
        self.receiver.processors["corr"].start_corr_hdf5(
            base_dir=base_dir,
            base_filename=base_filename,
            capture_duration=capture_duration,
            capture_n_inputs=capture_n_inputs,
            autocorr_only=autocorr_only,
            elements_per_file=elements_per_file,
            frame0_irigb_time=frame0_irigb_time,
            number_of_correlators=number_of_correlators,  # has to be provided
            number_of_correlated_inputs=number_of_correlated_inputs,  # has to be provided
            number_of_bins_per_frame=number_of_bins_per_frame  # has to be provided
            )
        corr = self.receiver.processors["corr"]
        return (
            f"started correlator hdf5 writing to disk with "
            f"firmware_integ_period={corr.firmware_integ_period}."
        )

    @endpoint('stop-corr-hdf5')
    async def stop_corr_hdf5(self):
        self.receiver.processors["corr"].stop_adc_hdf5()
        return "stopped corrlelator hdf5 writing to disk."

    @endpoint('get-rms')
    async def get_rms(self):
        adc = self.receiver.processors.get("adc") if self.receiver else None
        if self.receiver and self.receiver.is_running() and adc is not None:
            rms = await adc.get_adc_rms_async()
        else:
            rms = []  # if the receiver is not ready, return an empty list. This won't cause the caller to crash.
            # return raw packet processor not yet running")
        return dict(rms=rms)

    @endpoint('get-monitoring-data')
    async def get_monitoring_data(self):
        t0 = time.time()
        self.log.info(f'{self!r}: Received monitoring metrics request')
        if self.receiver:
            metrics = await self.receiver.get_metrics_async()
        else:
            metrics = Metrics()
        t1 = time.time()
        # handler.set_header('Content-Type', 'text/plain')
        # handler.set_header('Content-Encoding', 'gzip')
        # handler.write(metrics.get_gzip())

        # Return gzip-compressed response
        response = aiohttp.web.Response(
            body=metrics.get_gzip(),
            headers={'Content-Encoding': 'gzip'})
        t2 = time.time()
        self.log.info(
            f'{self!r}: Returning raw_acq {len(metrics)} metrics. '
            f'The request took {t2 - t0:.3f} seconds '
            f'({t1 - t0:.3f}s to format metrics, {t2 - t1:.3f}s to encode them)')
        return response

