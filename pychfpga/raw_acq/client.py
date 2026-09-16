"""Raw acquisition REST client."""

from wtl import log
from wtl.rest import AsyncRESTClient

from .server import RawAcqAsyncRESTServer

class RawAcqAsyncRESTClient(AsyncRESTClient):
    """
    Implements an asynchronous client that exposes the functions of the specified remote RawAcq server.

    This client is used by ch_master to start, configue and operate all the RawAcq servers in the array.

    The client is implemented using a Tornado AsyncHTTPClient. It exposes the RawAcq server methods
    (i.e REST endpoints) as local methods. The local methods are Tornados so requests to
    multiple clients can be made in parallel. This is especially beneficial since the data requests
    from the server are slow IO operations which benefit the mist from casync o-execution.

    The client will operate only if the IOloop in which is was created is running.

    Parameters:

        name (str): Name of the client, to be used in logging etc.

        hostname (str): The hostname of the RawAcq REST server. If `host` is None, an (experimental,
             Python-based) RawAcq REST server will be created locally.

        port (int): The port number to which the RawAcq REST server is listening. Default is port 80.

        kwargs: All remaining aruments will be stored as configuration data.
    """

    def __init__(self,
                 name='RawAcq',
                 hostname='localhost',
                 port=RawAcqAsyncRESTServer.DEFAULT_PORT,
                 base_dir='~/data',
                 base_filename=None,
                 aux_base_dir='~/data',
                 aux_base_filename=None,
                 create_server=True,
                 **config):
        super().__init__(
            hostname=hostname,
            port=port,
            server_class=RawAcqAsyncRESTServer if create_server else None,
            heartbeat_string=None)

        self.name = name
        self.hdf5_base_dir = base_dir
        self.hdf5_aux_base_dir = aux_base_dir
        self.base_filename = base_filename or name
        self.aux_base_filename = aux_base_filename
        self.config = config

    async def ping(self):
        try:
            await self.get('status')
            self.log.info(f"Successfully pinged raw_acq server at {self.hostname}:{self.port}")
        except Exception as e:
            self.log.debug(repr(e))
            self.log.error("Can't ping raw_acq server at {self.hostname}:{self.port}")
            return False
        return True  # return raises an exception: we don't want it in the try block

    async def status(self):
        result = await self.get('status')
        return result

    async def start(self, **config):
        """ Start the RaqAcq remote server with the keyword argument as configuration data"""
        self.log.info(f'{self!r}: Starting remote RawAcq server at {self.hostname}:{self.port}')
        self.log.debug(
            f'{self!r}: Starting remote RawAcq server '
            f'at {self.hostname}:{self.port} with config: {config!r}')
        result = await self.post('start', **config)
        return result

    async def stop(self):
        result = await self.get('stop')
        return result

    async def update_config(self, config):
        return await self.post('update-config', **config)

    async def get_packets(self):
        data = await self.get('get-packets')
        return data

    async def start_fft_rms(self, stream_ids=[], target_gain_bank=0, number_of_frames=100):
        await self.post(
            'start-fft-rms',
            stream_ids=stream_ids,
            target_gain_bank=target_gain_bank,
            number_of_frames=number_of_frames)

    async def get_fft_rms(self):
        ix, rms = await self.get('get-fft-rms')
        return (ix, rms)

    async def start_raw_hdf5(self,
                             base_dir=None,
                             base_filename=None,
                             capture_duration=0,
                             capture_refresh_time=0,
                             elements_per_file=2048 * 64,
                             capture_aux_enable=None,
                             aux_base_dir=None,
                             aux_base_filename=None,
                             capture_aux_refresh_time=0,
                             capture_aux_stream_ids=[6],
                             aux_elements_per_file=2048 * 64):
        result = await self.post(
            'start-raw-hdf5',
            base_dir=base_dir or self.hdf5_base_dir,
            base_filename=base_filename or self.base_filename,
            capture_duration=capture_duration,
            capture_refresh_time=capture_refresh_time,
            elements_per_file=elements_per_file,
            capture_aux_enable = capture_aux_enable,
            aux_base_dir=aux_base_dir or self.hdf5_aux_base_dir,
            aux_base_filename=aux_base_filename or self.aux_base_filename,
            capture_aux_refresh_time=capture_aux_refresh_time,
            capture_aux_stream_ids = capture_aux_stream_ids,
            aux_elements_per_file=aux_elements_per_file
            )
        return result

    async def stop_raw_hdf5(self):
        result = await self.get('stop-raw-hdf5')
        return result

    async def start_corr_hdf5(
            self,
            base_dir=None,
            base_filename=None,
            capture_duration=0,
            elements_per_file=2048 * 64,
            capture_n_inputs=4,
            autocorr_only=False,
            software_integration_period=1,
            frame0_irigb_time=0,
            **kwargs):
        """Start correlator HDF5 capture on the remote raw_acq server."""
        self.log.debug(f'{self!r}: Triggered start_corr_hdf5, posting start-corr-hdf5 endpoint')
        result = await self.post(
            'start-corr-hdf5',
            base_dir=base_dir or self.hdf5_base_dir,
            base_filename=base_filename or self.base_filename,
            capture_duration=capture_duration,
            capture_n_inputs=capture_n_inputs,
            autocorr_only=autocorr_only,
            elements_per_file=elements_per_file,
            frame0_irigb_time=frame0_irigb_time,
            software_integration_period=software_integration_period,
        )
        return result

    async def stop_corr_hdf5(self):
        result = await self.get('stop-corr-hdf5')
        return result

