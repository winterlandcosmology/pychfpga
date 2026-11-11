#!/usr/bin/python

"""
ct_prober.py module
 Implements the interface to the corner-turn output capture probe (ct_prober)

 History:
 2026-08-10 VB: Created
"""

# Standard library packages
import logging

from pychfpga.common import State

from ..mmi import BitField, CONTROL, STATUS
from ..f_engine.prober import PROBER


class CTProber(PROBER):
    """ Implements the interface to the corner-turn output capture probe.

    The ct_prober firmware module (hdl/ct/ct_prober.vhd, cookie 0xA2, BSB top
    router port 6) occasionally snapshots one complete corner-turn output
    packet from ONE software-selectable lane (0-7) and streams it to the host
    over the existing slow capture link, wrapped in the standard 10-byte
    capture header. It exists only in ICE corner-turn firmware builds
    (IMPLEMENT_CT_PROBER=TRUE, i.e. the CT_LEVEL=3 'ct' bitstreams); it is
    never present in corr16 builds or on CRS/UCAP platforms, and the
    constructor refuses to instantiate on such firmware.

    The register map deliberately mirrors the channelizer prober
    (hdl/chan/prober.vhd) so this class can subclass PROBER; only CONTROL
    byte 0 and the STATUS bytes differ. Because MMI.__setattr__'s lock only
    accepts attribute names found in this class's own __dict__ (it does not
    walk the MRO), every writable BitField is re-declared below even when it
    is identical to the parent's; conversely, the parent fields that do NOT
    exist in this hardware (SOURCE_SEL, FIFO_RESET, PROBER_USER_FLAGS,
    PERIOD1 and the chan-specific status bits) are intentionally not
    declared, so the lock blocks accidental writes to them.

    Clock-domain-crossing contract (same as the channelizer prober): the
    control bytes cross into the 244 MHz corner-turn clock domain without
    synchronization, so OFFSET, PERIOD0 and SUB_PERIOD must only be changed
    while the module is held in RESET (or be followed by a RESET pulse).
    CAPTURE_EN and LANE_SEL are safe to change live: CAPTURE_EN only gates
    new burst triggers, and LANE_SEL is latched between bursts.

    All periods/offsets are counted in corner-turn PACKETS (one CT packet
    carries GROUP_FRAMES FPGA frames of 2.56 us), not in FPGA frames.
    """

    # Memory-mapped control registers
    RESET = BitField(CONTROL, 0, 7, doc="Resets the module (including the FIFO, counters and sticky error bits)")
    CAPTURE_EN = BitField(CONTROL, 0, 6, doc="When '1', enables new capture bursts. '0' (default) parks the probe; an in-flight burst still completes")
    LANE_SEL = BitField(CONTROL, 0, 3, width=3, doc="Corner-turn output lane (0-7) to capture. Latched between bursts, safe to change live")
    BURST_LENGTH = BitField(CONTROL, 0, 0, width=2, doc="Sets the number of consecutive CT packets to capture and transmit, minus one")

    OFFSET = BitField(CONTROL, 3, 0, width=24, doc="Number of CT packets to wait before starting the capture")

    PERIOD0 = BitField(CONTROL, 6, 0, width=24, doc="Number of CT packets between capture triggers")

    STREAM_ID = BitField(CONTROL, 8, 0, width=16, doc="Stream id of the capture packets. Bits 15:12 (burst packet index) and 3:0 (lane number) are overwritten by hardware")
    SEND_DELAY = BitField(CONTROL, 10, 0, width=16, doc="Amount of time to wait before sending captured data once the data fifo has been emptied. The actual delay is (send_delay*65536)/125 MHz.")

    # CONTROL bytes 11-13 (the channelizer prober's PERIOD1) are reserved
    SUB_PERIOD = BitField(CONTROL, 14, 0, width=5, doc="Capture additional bursts every 2**(N+1) CT packets. 23 disables sub-period captures")

    # Memory-mapped status registers
    _DATA_FIFO_EMPTY = BitField(STATUS, 0x00, 4, doc="State of the DATA_FIFO_EMPTY signal (for debugging)")
    _DATA_FIFO_OVERFLOW = BitField(STATUS, 0x00, 3, doc="State of the DATA_FIFO_OVERFLOW signal (for debugging)")
    _DATA_FIFO_OVERFLOW_STICKY = BitField(STATUS, 0x00, 2, doc="Sticks to '1' when the burst overflowed the data FIFO. Cleared by RESET only")
    OVERRUN = BitField(STATUS, 0x00, 1, doc="Sticks to '1' when a capture trigger arrived while the previous burst was still being read out (that burst was skipped). Cleared by RESET only")

    CURRENT_LANE = BitField(STATUS, 0x01, 0, width=3, doc="Lane currently latched by the capture mux")

    # The data FIFO holds 2048 33-bit words; a whole burst must fit in it
    # (flags-off shuffle16: 3 x 517-word packets max at 1 frame/packet, one
    # 1029-word packet at 2 frames/packet). Unlike the channelizer prober
    # there is no DATA_BUFFER_CAPACITY/send-delay constraint: a trigger that
    # arrives while the FIFO is still being read out is skipped and flagged
    # in OVERRUN instead of overflowing.
    DATA_FIFO_DEPTH = 2048

    def __init__(self, *, router, router_port, instance_number, config):
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port, instance_number=instance_number, config=config)
        # The parent __init__ has locked the instance already; only existing
        # attributes can be touched from here on, which is all we need.
        if not getattr(self.fpga, 'HAS_CT_CAPTURE', False):
            raise RuntimeError(
                f"{self!r}: ct_prober is not implemented in this firmware "
                f"(platform id={getattr(self.fpga, 'PLATFORM_ID', None)!r}, "
                f"CT_LEVEL={getattr(self.fpga, 'CT_LEVEL', None)!r})")

    # Specialized functions

    def reset_fifo(self):
        raise RuntimeError("ct_prober has no separate FIFO reset; use reset(), which also clears the FIFO")

    def set_data_source(self, source):
        raise RuntimeError(
            "The CT prober always captures the corner-turn output; select a lane instead "
            "with LANE_SEL or start_ct_capture(lane=n)")

    def set_burst_period(self, burst_period0=390625, burst_period1=None):
        """ Sets the interval between data capture bursts. The period is specified in number of CT packets.

        `burst_period1` is accepted for signature compatibility with the
        channelizer prober but must be None: the ct_prober has a single
        period register (CONTROL bytes 11-13 are reserved).
        """
        if burst_period1 is not None:
            raise ValueError("ct_prober has a single burst period; burst_period1 must be None")
        self.PERIOD0 = burst_period0 - 1

    def get_burst_period(self):
        """ Returns the interval between data capture bursts, in CT packets."""
        return [self.PERIOD0]

    def get_packet_geometry(self, lane=None):
        """ Return the corner-turn packet geometry for `lane`, read back from the crossbar registers.

        Parameters:

            lane (int or None): corner-turn output lane. If None, the lane
                currently programmed in LANE_SEL is used.

        Returns:

            dict: frames_per_packet, bins_per_frame, channels_per_bin,
                words_per_bin, data_words, flags_words, words_per_packet,
                four_bits, use_offset_binary, send_flags, geometry_source.

        Notes:

            In single-stage modes (shuffle16, chan*) the later crossbars are
            bypassed and the geometry comes from the FIRST corner-turn stage
            (CB1). In modes where CROSSBAR3 actively re-slices the packets
            (shuffle256/512, chord16) the output geometry differs from CB1's
            in every respect INCLUDING total size (e.g. shuffle256: 326-word
            CB1 packets become 597-word output packets at one frame per
            packet with flags), so the geometry is derived from the CB3
            bin-selector registers instead. The data encoding fields
            (four_bits, use_offset_binary) and send_flags always come from
            CB1: the equivalent CB3 registers only populate the outgoing
            header and are never programmed by init_crossbars. The 4-word CT
            header carried inside every captured packet remains readable
            with read_raw_frames(..., format='32'), but its bins/channels
            fields are likewise unprogrammed (zero) at CB3.
        """
        if lane is None:
            lane = int(self.LANE_SEL)
        # Encoding and flag enablement are properties of the first stage in
        # every mode (CB1 packs the nibbles and appends the flags blocks).
        cb1_bs = self.fpga.CT.CROSSBAR.BIN_SEL[int(lane)]
        four_bits = bool(cb1_bs.FOUR_BITS)
        use_offset_binary = bool(cb1_bs.USE_OFFSET_BINARY)
        send_flags = bool(cb1_bs.SEND_FLAGS) and not bool(cb1_bs.BYPASS)

        cb3 = getattr(self.fpga.CT, 'CROSSBAR3', None)
        cb3_active = cb3 is not None and not bool(cb3.BIN_SEL[0].BYPASS)
        if cb3_active:
            # Active shuffle mode: the packet leaving the corner turn is the
            # one re-sliced by the CB3 bin selector of this output lane.
            bs = cb3.BIN_SEL[int(lane)]
            frames_per_packet = int(bs.NUMBER_OF_FRAMES_PER_PACKET)
            bins_per_frame = len(bs.get_selected_bins())
            # Each output bin concatenates the words of all input lanes the
            # selector is configured to forward.
            lanes = int(bs.LAST_LANE) - int(bs.FIRST_LANE) + 1
            words_per_bin = int(bs.NUMBER_OF_WORDS_PER_BIN) * lanes
            data_words = frames_per_packet * bins_per_frame * words_per_bin
            if send_flags:
                data_flag_words = bins_per_frame * lanes * int(bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN)
                frame_flag_words = int(bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME) * lanes
                flags_words = frames_per_packet * (data_flag_words + frame_flag_words)
            else:
                flags_words = 0
            geometry_source = 'CB3'
        else:
            bs = cb1_bs
            frames_per_packet = int(bs.GROUP_FRAMES)
            bins_per_frame = int(bs.NUMBER_OF_SELECTED_WORDS)
            channels_per_bin = (int(bs.LAST_FIFO_NUMBER) - int(bs.FIRST_FIFO_NUMBER) + 1) * 4
            words_per_bin = channels_per_bin // (4 if four_bits else 2)
            data_words = frames_per_packet * bins_per_frame * words_per_bin
            if send_flags:
                # Data flags block per frame, plus one frame-flags word per frame
                # (same formulas as BCTEngine.init_crossbars/print_packet_size)
                data_flag_words = bins_per_frame * words_per_bin * 4 // (32 if bs.COMBINE_DATA_FLAGS else 16)
                flags_words = frames_per_packet * (data_flag_words + 1)
            else:
                flags_words = 0
            geometry_source = 'CB1'
        channels_per_bin = words_per_bin * (4 if four_bits else 2)
        # 4-word CT header + data + optional flags + 1 packet-flags trailer word
        words_per_packet = 4 + data_words + flags_words + 1
        return dict(
            frames_per_packet=frames_per_packet,
            bins_per_frame=bins_per_frame,
            channels_per_bin=channels_per_bin,
            words_per_bin=words_per_bin,
            data_words=data_words,
            flags_words=flags_words,
            words_per_packet=words_per_packet,
            four_bits=four_bits,
            use_offset_binary=use_offset_binary,
            send_flags=send_flags,
            geometry_source=geometry_source)

    def stop_capture(self):
        """Park the probe.

        ``GLOBAL_TRIG`` does not gate this probe, so capture is stopped by
        clearing ``CAPTURE_EN`` and holding the probe in reset.
        """
        self.CAPTURE_EN = 0
        self.RESET = 1

    def start_capture(
            self,
            period=None,
            frames_per_burst=1,
            lane=0,
            verbose=1,
            burst_period_in_seconds=None,
            burst_period_in_frames=None,
            burst_period_frame_rounding=4,
            offset=0,
            send_delay=0,
            read_rate=None):
        """Capture one corner-turn output lane and send it to the host.

        The period is counted in CT packets: one packet spans
        ``frames_per_packet`` FPGA frames. The channelizer reset is not
        toggled. On a crate board that restart shifts the frame phase and
        breaks corner-turn alignment until the next array sync.
        Stop with :meth:`stop_capture`.

        Parameters:

            period (float): time in seconds between captured bursts.

            burst_period_in_seconds (float): same as ``period``.

            burst_period_in_frames (int): FPGA frames between bursts. Converted
                to CT packets using the selected lane's packet geometry.

            burst_period_frame_rounding (int): round the frame period down to
                a multiple of this value before converting to CT packets.

            frames_per_burst (int): consecutive CT packets in one burst (1-4).

            lane (int): corner-turn output lane, 0-7.

            offset (int): CT packets skipped after the probe reset before the
                first burst.

            send_delay (int): probe ``SEND_DELAY`` units (524.288 us each).

            read_rate (int): host frame read rate written to GPIO.HOST_FRAME_READ_RATE if not None. Affects all capture engines (data and correlator)
        """
        fpga = self.fpga
        if burst_period_in_seconds is not None:
            period = burst_period_in_seconds
        if ((burst_period_in_frames is None) and (period is None)) \
           or ((burst_period_in_frames is not None) and (period is not None)):
            raise ValueError("You must specify either 'period' or 'burst_period_in_frames'")
        if period is not None:
            burst_period_in_frames = max(float(period) / fpga.FRAME_PERIOD, 1)
        burst_period_in_frames = (
            int(burst_period_in_frames) // burst_period_frame_rounding
        ) * burst_period_frame_rounding

        lane = int(lane)
        if not 0 <= lane <= 7:
            raise ValueError(f"lane must be between zero and seven, got {lane}")

        geometry = self.get_packet_geometry(lane)
        frames_per_packet = int(geometry["frames_per_packet"])
        if frames_per_packet < 1 or int(geometry["data_words"]) == 0:
            raise RuntimeError(
                "The corner-turn crossbars do not appear to be initialized "
                "(the selected lane reports an empty packet); run init_corner_turn "
                "before capturing from the corner turn")
        burst_period_in_packets = max(
            int(burst_period_in_frames) // frames_per_packet,
            int(frames_per_burst))

        if verbose:
            packet_bytes = 4 * int(geometry["words_per_packet"]) + 10 + 42
            packets_per_second = (
                frames_per_burst / fpga.FRAME_PERIOD
                / frames_per_packet
                / burst_period_in_packets)
            self.logger.debug(
                f"{fpga!r}: CT capture on lane {lane}: "
                f"{packets_per_second * packet_bytes * 8 / 1e6:.3f} Mbits/s "
                f"({packet_bytes - 52}-byte CT packets)")

        if read_rate is not None:
            fpga.GPIO.HOST_FRAME_READ_RATE = read_rate
        self.RESET = 1
        self.LANE_SEL = lane
        self.SUB_PERIOD = 23
        self.set_stream_id(fpga.get_id(lane))
        self.config_capture(
            frames_per_burst=frames_per_burst,
            burst_period=burst_period_in_packets,
            offset=offset,
            send_delay=send_delay,
            words_per_packet=geometry["words_per_packet"])
        self.RESET = 0
        self.CAPTURE_EN = 1
        self.update_config()
        fpga.set_trig(1)
        self.logger.debug(f"{fpga!r}: Enabling corner-turn capture on lane {lane}")

    def config_capture(self, frames_per_burst=1, burst_period=100, offset=0, send_delay=0, words_per_packet=None):
        """
        Configure the capture of corner-turn packets for transmission over the ethernet link.

        The module must be held in RESET while this is called (or RESET must
        be pulsed afterwards): the period/offset registers cross into the
        corner-turn clock domain unsynchronized.

        Parameters:

            frames_per_burst (int): number of consecutive CT packets to send
                in a burst (default=1). The whole burst must fit in the
                2048-word data FIFO.

            burst_period (int): delay between bursts, in CT packets.

            offset (int): number of CT packets to skip after reset before
                the capture starts.

            send_delay (int): Sets the delay to start sending a block of data
                after it has been captured. The delay is a 16 bit value that
                corresponds to increments of 65536/125 MHz = 524.288 us.

            words_per_packet (int or None): expected 32-bit words per CT
                packet, used to enforce the FIFO ceiling. If None, it is
                read back from the CB1 bin-sel registers of the currently
                selected lane (see get_packet_geometry()).
        """
        if not 1 <= int(frames_per_burst) <= 4:
            raise ValueError(
                f"frames_per_burst must be in range [1, 4] for ct_prober hardware (2-bit BURST_LENGTH), "
                f"got {frames_per_burst}."
            )

        if words_per_packet is None:
            words_per_packet = self.get_packet_geometry()['words_per_packet']
        max_frames_per_burst = self.DATA_FIFO_DEPTH // int(words_per_packet)
        if int(frames_per_burst) * int(words_per_packet) > self.DATA_FIFO_DEPTH:
            raise ValueError(
                f"A burst of {int(frames_per_burst)} CT packets of {int(words_per_packet)} words does not fit "
                f"in the {self.DATA_FIFO_DEPTH}-word capture FIFO. Maximum is {max_frames_per_burst} "
                f"packet(s) per burst for the current corner-turn configuration.")

        if burst_period < frames_per_burst:
            burst_period = frames_per_burst
        if burst_period >= 2**24:
            raise RuntimeError(f'Capture period of {int(burst_period)} CT packets is too long. Maximum value is {2 ** 24 - 1}')

        self.BURST_LENGTH = frames_per_burst - 1  # BURST_LENGTH actually specifies the number of additional packets in the firmware
        self.set_burst_period(burst_period)
        self.OFFSET = offset
        self.SEND_DELAY = send_delay

    def update_config(self):
        """Publish the capture format for the lane currently selected in ``LANE_SEL``.

        The word counts come from the corner-turn bin selectors. The receiver
        uses ``fmt_data_words`` to size and unpack the payload.
        """
        geometry = self.get_packet_geometry()
        if not self.config.has_attr("capture_format"):
            self.config.capture_format = State()
        self.config.capture_format.update(
            fmt_is_ct_data=True,
            fmt_header_words=4,
            fmt_data_words=int(geometry["data_words"]),
            fmt_four_bits=bool(geometry["four_bits"]),
            use_offset_encoding=bool(geometry["use_offset_binary"]),
        )
        return self.config

    def init(self, **kwargs):
        """ Initialize the capture probe (parked, held in reset).

        Not called during board init: the power-on defaults already park the
        probe, and touching BSB port 6 on a firmware without the probe would
        stall the MMI. Call this (or start_ct_capture) only on
        a corner-turn bitstream.
        """
        self.logger.debug(f'{self!r}: Initializing corner-turn capture probe')
        self.set_stream_id(self.fpga.get_id(0))
        self.RESET = 1  # Make sure no data is being transmitted at reset

    def status(self):
        """ Displays the status of the corner-turn capture probe"""
        print('-------------- CT_PROBER corner-turn data capture --------------')
        print(f' Capture enabled: {int(self.CAPTURE_EN)}, lane {int(self.LANE_SEL)} (latched: {int(self.CURRENT_LANE)})')
        print(f' Capture {int(self.BURST_LENGTH) + 1} CT packet(s) every {int(self.PERIOD0) + 1} CT packets')
        print(f' Overrun: {int(self.OVERRUN)}, FIFO overflow (sticky): {int(self._DATA_FIFO_OVERFLOW_STICKY)}')
