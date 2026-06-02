"""Backward-compatible import path for :class:`RawDataPacketProcessor`."""

from .raw_data_packet_processor import RawDataPacketProcessor

RawDataProcessor = RawDataPacketProcessor

__all__ = ["RawDataProcessor", "RawDataPacketProcessor"]
