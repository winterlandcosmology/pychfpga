"""Dual-source raw packet processor with ADC and FFT sink dispatch."""

import numpy as np

from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor
from pychfpga.common.state import State

from .adc_data_processor import ADCDataProcessor
from .fft_data_processor import FFTDataProcessor


def _merge_fft_decoder_into_raw(raw_config, fft_config):
    """Apply the FFT/scaler decoder profile to the shared raw assembly config.
    
    *** TODO: This method should not be necessary if the config file was strucutred properly.
    """
    raw = State(raw_config).deepcopy() if not isinstance(raw_config, State) else raw_config.deepcopy()
    fft = State(fft_config) if not isinstance(fft_config, State) else fft_config
    fft_caps = fft.get('capture_format')
    if fft_caps:
        fft_caps = list(fft_caps)
        if len(fft_caps) > 1 and fft_caps[1] is not None:
            caps = list(raw.get('capture_format') or [None, None])
            while len(caps) < 2:
                caps.append(None)
            cf1 = fft_caps[1]
            caps[1] = cf1 if isinstance(cf1, State) else State(cf1)
            raw.capture_format = caps
    for key in (
        'packets_per_logical_frame',
        'logical_frames_per_burst',
        'fft_offset_encoding',
        'offset_encoding',
        'fft_bin_map',
        'packet_cookie',
        'source',
    ):
        if key in fft:
            raw[key] = fft[key]
    return raw


class RawDataProcessor(RawDataPacketProcessor):
    """Assemble raw UDP bursts and dispatch formatted rows to ADC/FFT sinks."""

    def __init__(
        self,
        packet_receiver,
        config=None,
        adc_config=None,
        fft_config=None,
        stream_ids=(),
        metrics_refresh_time=10,
        adc_rms_refresh_count=100,
        **kwargs,
    ):
        if config is not None and fft_config is not None:
            config = _merge_fft_decoder_into_raw(config, fft_config)
        super().__init__(packet_receiver, config=config, **kwargs)
        self.recv = packet_receiver
        self.adc = ADCDataProcessor(
            packet_receiver,
            self,
            config=adc_config,
            stream_ids=stream_ids,
            metrics_refresh_time=metrics_refresh_time,
            adc_rms_refresh_count=adc_rms_refresh_count,
        )
        self.fft = FFTDataProcessor(
            packet_receiver,
            self,
            config=fft_config,
            stream_ids=stream_ids,
        )

    def update_config(self, config=None, adc_config=None, fft_config=None):
        if fft_config is not None:
            self.fft.update_config(fft_config)
        if config is not None and fft_config is not None:
            config = _merge_fft_decoder_into_raw(config, fft_config)
        elif fft_config is not None:
            config = _merge_fft_decoder_into_raw(self.config.as_dict(), fft_config)
        if config is not None:
            super().update_config(config)
        if adc_config is not None:
            self.adc.update_config(adc_config)

    def process_udp_packets(self, config=None, verbose=None):
        super().process_udp_packets(config=config, verbose=verbose)
        if not self.n_out:
            return
        indices = np.arange(self.n_out)
        rows = slice(0, self.n_out)
        for source, processor in enumerate((self.adc, self.fft)):
            ts, data, counts = self.get_formatted_data(rows, source=source)
            processor.consume((ts, data, counts, indices))

    async def get_metrics_async(self, metrics):
        for processor in (self.adc, self.fft):
            await processor.get_metrics_async(metrics)

    def flush(self, flush_timeout=None):
        super().flush(flush_timeout=flush_timeout)
        for processor in (self.adc, self.fft):
            processor.flush(flush_timeout=flush_timeout)

    def stop(self):
        for processor in (self.adc, self.fft):
            processor.stop()
