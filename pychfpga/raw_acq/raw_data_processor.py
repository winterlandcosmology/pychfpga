"""Dual-source raw packet processor with ADC and FFT sink dispatch."""

import numpy as np

from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor

from .adc_data_processor import ADCDataProcessor
from .fft_data_processor import FFTDataProcessor


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
        if config is not None:
            super().update_config(config)
        if adc_config is not None:
            self.adc.update_config(adc_config)
        if fft_config is not None:
            self.fft.update_config(fft_config)

    def process_udp_packets(self, config=None, verbose=None):
        super().process_udp_packets(config=config, verbose=verbose)
        if not self.n_out:
            return
        indices = np.arange(self.n_out)
        (ts0, data0, counts0), (ts1, data1, counts1) = self.get_formatted_data()
        self.adc.consume((ts0, data0, counts0, indices))
        self.fft.consume((ts1, data1, counts1, indices))

    async def get_metrics_async(self, metrics):
        await self.adc.get_metrics_async(metrics)
        await self.fft.get_metrics_async(metrics)

    def flush(self, flush_timeout=None):
        super().flush(flush_timeout=flush_timeout)
        self.adc.flush(flush_timeout=flush_timeout)
        self.fft.flush(flush_timeout=flush_timeout)

    def stop(self):
        self.adc.stop()
        self.fft.stop()
