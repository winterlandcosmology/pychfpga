"""Dual-source raw packet processor with ADC and FFT sink dispatch."""

import numpy as np

from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor
from pychfpga.common.state import State

from .adc_data_processor import ADCDataProcessor
from .fft_data_processor import FFTDataProcessor


class RawDataProcessor(RawDataPacketProcessor):
    """Assemble raw UDP bursts and dispatch formatted rows to ADC/FFT sinks."""

    def __init__(
        self,
        packet_receiver,
        config_getter,
        stream_ids=(),
        metrics_refresh_time=10,
        adc_rms_refresh_count=100,
        **kwargs,
    ):
        if config_getter is None:
            raise TypeError("config_getter is required")
        super().__init__(packet_receiver, config_getter=config_getter, **kwargs)
        self.recv = packet_receiver
        getter = config_getter
        self.adc = ADCDataProcessor(
            packet_receiver,
            self,
            config_getter=getter,
            stream_ids=stream_ids,
            metrics_refresh_time=metrics_refresh_time,
            adc_rms_refresh_count=adc_rms_refresh_count,
        )
        self.fft = FFTDataProcessor(
            packet_receiver,
            self,
            config_getter=getter,
            stream_ids=stream_ids,
        )
        self.update_config()

    def update_config(self):
        super().update_config()
        self.adc.update_config()
        self.fft.update_config()

    def process_udp_packets(self, verbose=None):
        super().process_udp_packets(verbose=verbose)
        if not self.n_out:
            return
        rows = self.out_rows
        indices = np.arange(rows.start, rows.stop)
        for source, processor in enumerate((self.adc, self.fft)):
            ts, data, counts = self.get_formatted_data(rows, source=source)
            processor.consume((ts, data, counts, indices))

    async def get_metrics_async(self, metrics):
        for processor in (self.adc, self.fft):
            await processor.get_metrics_async(metrics)

    def flush(self, flush_timeout=None):
        super().flush(flush_timeout=flush_timeout)
        self.skip_first_incomplete = True
        for processor in (self.adc, self.fft):
            processor.flush(flush_timeout=flush_timeout)

    def stop(self):
        for processor in (self.adc, self.fft):
            processor.stop()
