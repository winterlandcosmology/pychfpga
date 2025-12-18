#!/usr/bin/python

"""
untract.py module
Interface for the FPGA UltraRAM-based crossbar/packet generator.
"""

import logging
import asyncio
import socket

from wtl.metrics import Metrics
from ..mmi import MMI, BitField, CONTROL, STATUS
import numpy as np


class UCorn(MMI):
    """ Class to operate the UltraRAM-based corner-turn module"""

    ADDRESS_WIDTH = 16

    RAM_SEL             = BitField(CONTROL, 0, 5, doc='When 0, RAM writes are made to the UltraRAM data memory. When 1, RAM writes are made to the playlist memory.')
    RAM_BANK            = BitField(CONTROL, 0, 4, doc='Determines in which bank of the data buffer is written by RAM writes')
    RAM_FRAME           = BitField(CONTROL, 0, 0, width=4, doc='Determines for which frame of the data buffer is written by RAM writes')

    RX_WRITE_EN         = BitField(CONTROL, 1, 7, doc='1: Allows channelizer data to be written in the data buffer')
    TX_TRIG_SEL         = BitField(CONTROL, 1, 6, doc='   0: Data transmission starts when an incoming frame set is completed; 1: data transmission starts every `TX_PERIOD` clocks. ')
    TX_ENABLE           = BitField(CONTROL, 1, 5, doc="'1' to enable data transmission. '0' data transmission is muted.  ")
    TX_RESET            = BitField(CONTROL, 1, 4, doc="'1' resets the data transmitter  ")
    TX_TOGGLE_BANK      = BitField(CONTROL, 1, 3, doc="When 0, only bank 0 is sent. When 1, the bank not being currently written into is sent.")
    TX_INSERT_TIMESTAMP = BitField(CONTROL, 1, 2, doc="When 1, a timestamp is inserted in the header words .")
    RX_MASK_LOW_BINS    = BitField(CONTROL, 1, 1, doc="When 1, a incoming data for bins 0 to 127 is not written into the buffer so these bins can be used for packet headers.")
    TX_OVERRIDE_DATA    = BitField(CONTROL, 1, 0, doc="When 1, the output data is oferriden by a fixed pattern.")

    TX_PERIOD           = BitField(CONTROL, 5, 0, width=32, doc="Number of clocks between playlist transmission")
    TX_LENGTH           = BitField(CONTROL, 6, 0, width=8, doc="maximum number of words to transmit in a period")

    TIMESTAMP_INCR      = BitField(CONTROL, 7, 0, width=5,  doc="Timestamp increments between transmission sets")

    OVERRUN             = BitField(STATUS, 0, 0, doc='1 when data transmission request was performed before the previous transmission was completed. Sticky flag.')
    IN_FRAME_CTR        = BitField(STATUS, 1, 0, width=8, doc='Counts the number of frames coming in.')
    OUT_FRAME_CTR       = BitField(STATUS, 2, 0, width=8, doc='Counts the number of frames coming out.')

    def __init__(self, *, router, router_port, verbose=0):
        """ Creates a UCorn corner-turn engine instance.

        ``__init__`` initializes the instnce but does not yet start communicating with. This allows
        the communication link to be set-up before further initializations. Actual communications
        are performed by :meth:`init`.

        Parameters:

            fpga_instance (FPGAFirmware): instance of the FPGAFirmware that represents the firmware
                running on the board. Used to access memory-mapped registers of the UCorn module.

            base_address (int): Base address of the registers for this module instance

            address_increment (int): Address offset between multiple UCorn instances. Is not used
                since there is usually only one UCorn instance per design.

            verbose (int): When non-zero, debugging messages will be printed.

        """
        super().__init__(router=router, router_port=router_port)
        # self.fpga = fpga_instance
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        # self.crossbar_level = 1
        # self.NUMBER_OF_CROSSBAR_OUTPUTS = 1 # 1 physicala link, although we have up to 128 logical links

        # define the default source parameters
        self.source_mac_addr = "00:5D:03:01:02:03"
        self.source_ip_addr = "10.70.0.1"
        self.source_ip_port = 41000
        self.targets = {
            0: dict(dest_mac_addr="FF:FF:FF:FF:FF:FF", dest_ip_addr="10.88.0.1", dest_ip_port=41001),
            1: dict(dest_mac_addr="01:23:45:67:89:AB", dest_ip_addr="10.88.0.2", dest_ip_port=41002),
        }
        self.sacrificial_bins = list(range(128))  # bins locations used to store packet headers

    def init(self):
        """ Initializes the UCorn module

        """

        self.RX_WRITE_EN = 0  # Do not allow F-Engine data wo be written - we will write test patterns manually into the data buffers.
        self.RX_MASK_LOW_BINS = 1 # prevent F-ENgine data to be written in the first bins, as these are used for storing packet headers.

        self.TX_TRIG_SEL = 1  # enable period-based transmission trigger
        self.TX_ENABLE = 1 # enable packet transmission
        self.TX_TOGGLE_BANK = 0 # always send data from the same bank
        self.TX_INSERT_TIMESTAMP = 1  # insert the timestamp at the appropriate place in the payload header
        self.TX_OVERRIDE_DATA = 0 # Do not override the output data (for low level debugging)
        self.TX_RESET = 1 # put the module in reset state

        # program data for one bin
        self.set_bin_data(bin=1, frame=0, data=np.arange(8*16, dtype=np.uint8))

        first_bin = 300*8192//1600 # 1536
        last_bin= 1500*8192//1600  # Last bin is excluded
        number_of_bins = last_bin - first_bin
        number_of_packets = 128
        bins_per_packet = number_of_bins // number_of_packets

        self.playlist = [
            (dict(dest_mac_addr=f"01:23:34:67:89:{i+1:02X}",
                  dest_ip_addr=f"10.88.0.{i+1}",
                  dest_ip_port=41001),
             np.arange(bins_per_packet) + first_bin + bins_per_packet*i)
            for i in range(number_of_packets)]
        # Set the playlist to send data from that bin
        self.set_playlist(playlist=self.playlist)

        self.TX_RESET = 0 # take the module out of reset. Packet transmisison starts.

    def set_bin_data(self, frame=0, bin=0, data=b'', bank=(0,1)):
        """ Set the bin data for one or more consecutive frames.

        Parameters:

            frame (int): Starting frame number into which the data will be written

            bin (int): Bin number into which the data will be written

            data (bytes): Data to write. One frame for one bin contains 8 bytes (4+4i complex
                values). If more than 8 bytes are specified, each block of 8 bytes is written in
                consecutive frames starting at `frame`.

            bank (int, tuple or list): Bank into which the data will be written

        """
        if isinstance(bank, int):
            bank = (bank,)
        for i in range(0, len(data), 8):
            for b in bank:
                self.write_data_buffer(bank=b, frame=frame, bin=bin, data=data[i:i+8])
            frame += 1

    def set_ethernet_header(self, target, dest_bin, length, stream_id=None, source_id=None, dest_mac_addr=None, dest_ip_addr=None, dest_ip_port=None):
        """ Created and stores an Ethernet/IP/UDP/payload header in the bin corresponding to specified target.

        Parameters:

            target (dict or key): dict containing the target information, or key used to lookup ``self.targets`` for that information.

            dest_bin (int): Index of bin whose memory space will be used to store the packet header

            length (int): number of bytes in the payload. Used to fill the IP & UDP header length fields

            stream_id (int): 16-bit integer describing the stream ID. Placed directly in the payload header.

        The header is static; it does not change from packet to packet, as it depends only on fixed
        parameters such as source/destination addresses and ports, packet length and Stream ID. A
        new header must be computed if any of these parameters are changed. The IP checkum covers
        only the static IP header bytes, and the UDP checksum covers the whole payload, but is
        optional and is not used. There is one exception to the static header principle: the payload
        timestamp is updated on-the-fly by the firmware. The timestamp does not affect the header
        checksums as it is in the UDP payload.


        The Ethernet/IP/UDP packet header format is:

        - Bytes 0-5: Ethernet MAC destination address. Specified in the ``targets`` dict.
        - Bytes 6-11: Ethernet MAC source address. Always value in ``self.source_mac_addr``
        - Bytes 12-13: Ethernet Ethtype: Always 0x0800.
        - Bytes 14-15: IP Version/Header Length/Flags: Always 0x4500
        - Bytes 16-17: IP Total packet length. Computed. Big endian.
        - Bytes 18-19: IP Identification: Always 0x1234 (arbitrary)
        - Bytes 20-21: IP Flags/Fragments/offsets: Always 0x0000
        - Byte  22: IP TTL (Time to live); Always 0x7F
        - Byte  23: IP Protocol: UDP (0x11)
        - Bytes 24-25: IP Header Checksum. Computed
        - Bytes 26-29: IP Source address
        - Bytes 30-33: IP Destination address
        - Bytes 34-35: UDP source port. From  ``self.source_ip_port``. Big endian.
        - Bytes 36-37: UDP destination port. From target dict. Big endian.
        - Bytes 38-39: UDP Length. Computed. Big Endian.
        - Bytes 40-41: UDP Checksum (optional). Not used. Always 0x0000.

        The paylod packet format is:

        - Byte  (42+) 0: Cookie (0xCF)
        - Byte  (42+) 1: Header info (0x14)
        - Byte  (42+) 2-3: 16-bit, Source ID, little endian. Identifies which board (crate/slot) is sending the data
        - Bytes (42+) 4-5: 16-bit Stream ID, little endian. Key to a system-defined map that identifies the content of the payload (i.e which frequency bins are present
        - Bytes (42+) 6-7: 16-bit, Zeros (0x0000). Reserved for future timestamp changes.
        - Bytes (42+) 8-15: 64-bit timestamp (little endian). This value is updated on-the-fly by the FPGA firmware.
        - Bytes (42+) 16-21: Reserved (all 0's)

        .. Future agreed-upon timestamp position change to align it to a 8-byte address boundary
        .. - Bytes (42+) 6-13: 64-bit timestamp (little endian). This value is updated on-the-fly by the FPGA firmware.
        .. - Bytes (42+) 14-21: Reserved (all 0's)

        """
        def h(s):
            # print(s)
            return bytes.fromhex(s.translate({ord(c):None for c in "_ :"}))
        src_mac_addr = h(self.source_mac_addr)
        src_ip_addr = socket.inet_aton(self.source_ip_addr)
        src_port = self.source_ip_port.to_bytes(2, 'big')

        if isinstance(target, dict):
            tgt = target
        elif target is not None:
            tgt = self.targets[target]
        else:
            tgt = {}
        dest_mac_addr = h(dest_mac_addr or tgt['dest_mac_addr'])
        dest_ip_addr = socket.inet_aton(dest_ip_addr or tgt['dest_ip_addr'])
        dest_port = (dest_ip_port or tgt['dest_ip_port']).to_bytes(2, 'big')
        # dest_bin = tgt['bin']

        udp_len = 8 + 22 + length
        ip_len = 20 + udp_len

        # ethernet header
        ethertype = h('0800') # IP protocol
        # eth_header = f"{dest_mac_addr} {src_mac_addr} 0800"  # -- dest MAC addr, src MAC addr, ethertype (0x0800=IPv4)
        eth_bytes = dest_mac_addr + src_mac_addr + ethertype  # -- dest MAC addr, src MAC addr, ethertype (0x0800=IPv4)

        # IP header
        # ip_header = ; #-- Version/HdrLen/DSCP/ECP flags, IP Len, ID, Flags/Frag offset, TTL, Protocol, IP header checksum, src IP addr, Dest IP Addr
        ip_bytes = bytearray(h('4500') + ip_len.to_bytes(2,'big') + h('1234 0000 7F 11_0000') + src_ip_addr + dest_ip_addr)
        ip_checksum = (sum(ip_bytes[::2]) << 8) + sum(ip_bytes[1::2])
        ip_checksum = (~ (ip_checksum + (ip_checksum >> 16))) & 0xFFFF
        ip_bytes[10:12] = ip_checksum.to_bytes(2,'big')

        # UDP Header
        udp_bytes = src_port + dest_port + udp_len.to_bytes(2, 'big') + h("0000"); #-- Src port, dest port, UDP len,  UDP checksum (0=disable)
        # udp_header = f"{src_port:04X} {dest_port:04X} {udp_len:04X} 0000"; #-- Src port, dest port, UDP len,  UDP checksum (0=disable)

        # user header
        user_bytes = h("CF14") + source_id.to_bytes(2, 'little') + stream_id.to_bytes(2, 'little') + h('0000 0000000000000000 000000000000')

        # total header
        header = eth_bytes + ip_bytes + udp_bytes + user_bytes
        self.logger.debug(f"{self!r}: Target {target} (bin {dest_bin}) Eth Header is {eth_bytes.hex()} ({len(eth_bytes)} bytes)")
        self.logger.debug(f"{self!r}: Target {target} (bin {dest_bin}) IP Header is {ip_bytes.hex()} ({len(ip_bytes)} bytes)")
        self.logger.debug(f"{self!r}: Target {target} (bin {dest_bin}) UDP Header is {udp_bytes.hex()} ({len(udp_bytes)} bytes)")
        self.logger.debug(f"{self!r}: Target {target} (bin {dest_bin}) User Header is {user_bytes.hex()} ({len(user_bytes)} bytes)")

        # print(f"Header is {header.hex()} ({len(header)} bytes)")
        self.set_bin_data(bin=dest_bin, data=header)


    def set_playlist(self, playlist=((0,[1]),), sacrificial_bins = list(range(128)), verbose=0):
        """ Configure the playlist buffer to send the selected bins

        Parameters:

            bins (list): List of ``(target, bin_list)`` tuples describing the bin numbers to send to each
                target, where:

                - ``target`` (dick or key): Dict containing the information on the destination for
                  the packet, or key allowing to lookup self.targets to get that information..

                - ``bin_list`` (list): list of integers describing the bin numbers (0 - 8191) to be sent.

            sacrificial_bins (list): list of bins that wil lbe used to store Ethernet headers and
                not actual bin data. The bin used to store the header of the first target is in
                sacrifcial_bin[0], the header of the next target is in sacrifcial_bin[1] etc.

        The playlist buffer is an array of 16-bit words describing which bins to send:

        - bit 15: End of transmission: Is '1' when this on the last word of the playlist
        - bit 14: End of packet. Is '1' when this is the last bin of a packet. Must be set when ``end_of_transmission`` is set.
        - bit 13: Number of frames: '0'= 2 framegroup (2*4=8 frames = 64 bytes), '1'= 4 framegroups (4*4=16 frames = 128 bytes).
              Set to '0' for transmitting an ethernet header, and sent to '1' to send actual data.
        - bit 12-0: Bin number. Bin to send, from 0 to 8191.

        The first bin of each packet described in the playlist shall point to a bin that has been
        repurposed to contains a Ethernet/IP/UDP/payload header, which containing the destination
        address etc. That bin  has bit 13 set in the playlist, so only 8-frame worth if data is
        sent, that is, 8 bytes/frame * 8 = 64 bytes, are transmitted. The Ethernet/IP/UDP headers need only 42 bytes,
        leaving 22 bytes are sufficient for our own packet headers (cookie, stream ID, flags,
        timestamp).

        The following bins in the playlist have bit 13 set to zero, so all 16 frames, or 128 bytes,
        of data is sent.

        Any number of different headers stored (with different destination IP addresses) can be
        stored in bins that would typically not be used in the experiment (e.g the lowest bins
        outside the feed response, the high bins subject to aliasing, known bins contaminated with
        RFI etc.).

        """

        # Compute the masks indicating which bins can be written with live data
        # We set the write-enable bit to '1' on every bin except the sactificial bins (those used for storing ethernet headers)
        self.sacrificial_bins = sacrificial_bins
        write_mask = np.full(8192/8, 0xFF, dtype=np.uint8) # 1 bit per bin, 8 bits per byte x 1024 bytes = 8192 bins
        for b in self.sacrificial_bins:
            write_mask[b//8] &=  ~ (1 << (b % 8))
        write_mask_buffer(0, write_mask)

        bins_total = sum(len(b) for t,b in playlist)
        self.logger.info(f'{self!r}: Total data rate: {1*(bins_total*16*8+64)*8*3200e6/16384/16/1e9} Gbps')
        if verbose:
            for (t,b) in playlist:
                l = len(b)*8*16+8*8
                self.logger.info(f"Target {t.get('dest_ip_addr', t) if isinstance(t,dict) else t}: Eth: {l} bytes, UDP: {l-42} bytes, {len(b)*4+2} words")
        addr = 0
        for i, (target, b) in enumerate(playlist):
            # COmpute the number of bytes to send excluding the ethernet/IP/UDP/payload header. Used to compute the headers.
            #  length =  bytes_per_bin * frames_per_bin * number_of_bins
            length = 8 * 16 * len(b)
            dest_bin = self.sacrificial_bins[i]
            crate,slot = self.fpga.get_id(default_crate=0, default_slot=0)
            source_id = crate <<8 | slot << 4
            self.set_ethernet_header(target=target, dest_bin=dest_bin, length=length, stream_id=i, source_id= source_id)  # Program the ethernet header
            b = np.concatenate(([dest_bin], b)).astype('>u2')
            if any(b >= 8192):
                raise('Invalid bin number in bin list')
            b[-1] |= 1<<14 # end of packet on last bin
            b[1:] |= 1<<13 # Send 4 frames except for 1st bin
            if i == len(playlist) - 1:
                b[-1] |= (1<<15) # end of transmission on last bin of last packet
            self.logger.debug(f"Writing Playlist RAM[{addr}]={b.tobytes().hex(':')}")
            self.write_playlist_buffer(addr, b.tobytes())
            addr += 2 * len(b)

        # self.RAM_SEL = 0  # select playlist buffer
        # b = np.array([i + (1<<13) for i in range(10*10)], dtype=np.uint8)

    def write_data_buffer(self,  bank=0, frame=0, bin=0, data = b''):
        """ Write data into the data buffer.

        Parameters:


            bank (int): Bank number into which the data is to be written (0 or 1).

            frame (int): Frame number into which the data is to be written (0 to 15)

            bin (int): Bin number into which the data is to be written (0-8191)

            data (bytes): Data to be written. Typically 8 bytes long.
        """

        self.RAM_SEL = 0  # select data buffer (not playlist buffer)
        if not isinstance(bank, (list, tuple)):
            bank = (bank,)
        if not isinstance(frame, (list, tuple)):
            frame = (frame,)
        if isinstance(data, np.ndarray):
            data = data.tobytes()
        for b in bank:
            self.RAM_BANK = b
            for f in frame:
                self.RAM_FRAME = f
                self.write_ram(8*bin, data)

    def write_playlist_buffer(self, addr, data):
        """ Write data to the playlist buffer

        Parameters:

            addr (int): byte address into the playlist buffer

            data (bytes): data to write

        """
        self.RAM_SEL = 1  # select the playlist buffer
        self.RAM_BANK = 0  # select the playlist buffer
        self.write_ram(addr, data)

    def write_mask_buffer(self, addr, data):
        """ Write data to the mask buffer

        Parameters:

            addr (int): byte address into the mask buffer

            data (bytes): data to write

        """
        self.RAM_SEL = 1  # select the playlist buffer
        self.RAM_BANK = 1  # select the playlist buffer
        self.write_ram(addr, data)

    def set_data_width(self, width):
    #     """
    #     Sets the number of bits expected at the input of the crossbar.
    #     All crossbars are set to the new setting.
    #         width=4: data is 4 bits Real + 4 bits Imaginary
    #         width=8: data is 8 bits Real + 8 bits Imaginary
    #     """
        self.logger.warn('{self!r}: Cannot set data width on UCorn. Width is hardware fixed at 4+4i bits.Command is ignored. ')

    def get_data_width(self):
        """
        Returns number of bits used by the crossbar.
        If all the crossbar sub-units  are not set in the same mode, an error is raised.
        """
        return 4

    def set_frames_per_packet(self, group_size):
        """
        Set the number of frame per packets.
        """
        self.logger.warn(f'{self!r}: set_frames_per_packet() is not implemented on {__name__}. Packet size is set through the playlist. Command is ignored. ')


