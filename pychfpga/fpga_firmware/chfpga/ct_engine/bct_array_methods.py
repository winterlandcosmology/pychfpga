"""Array-wide methods for BRAM-based corner-turn (BCT) firmware."""

import asyncio
import datetime
import time

import matplotlib.pyplot as plt
import numpy as np

from pychfpga.common import Ccoll
from pychfpga.common.array_methods import ArrayMethods


class BCTArrayMethods(ArrayMethods):
    """Array-wide methods for BCT (Ice shuffle / corr16) arrays."""

    # ------------------------------------------------------------------
    # Actively used (fpga_master / FPGAArray operational path)
    # ------------------------------------------------------------------

    def get_frequency_map(self, format='l:cscb'):
        """
        Returns a map describing the content (crate, slot, channel, bin) of
        every packet at the output of the corner turn engine.

        This map is obtained by passing the channelizer identity map through
        the shuffle map.
        """

        return self.get_shuffle_output(self.get_chan_identity_map(format=format))

    def get_chan_identity_map(self, format='l:cscb'):
        """
        Return an identity map that describes the origin of each of the 1024
        samples contained in the channelizer output packets.


        Returns:

            The map as a dict:

                {(crate, slot, channel): [sample_id0, ... sample_id1023]}

            where:

                (crate, slot, lane) tuple is the key from get_channel_ids(),
                   which derives its (crate, slot) from mb.get_id(). ``crate`
                   and ``slot`` could therefore be non-numeric if there is no
                   backplane or if a crate number is not assigned to a crate.

                ``sample_id`` describes the channelizer output samples. It varies depending on `format`:

                    "l:cscb": sample_id[bin_number] =  (crate, slot, channel, bin_number), where
                       ``crate``, ``slot``, ``channel`` are the same as in the
                       key, and can be non-numeric. ``bin_number`` is an integer from 0 to 1023.

                    "l:cc": sample_id[:] = global_chan_number, which is an integer representing the global crate, slot
                        and channel. It is the same for all bins. crate, slot
                        and channel must be numeric (there must be a crate and
                        crate number).

                    "l:cb": sample_id[bin_number] = (global_chan_number, bin_number).
                        See abive for ``global_chan_number``. ``bin_number`` is an integer from 0 to 1023. This tuple
                        uniquely represent every sample of every output in the
                        array.

                    "l:cb": sample_id[bin_number] = bin_number.
                        ``bin_number`` is an integer from 0 to 1023. This list is the same for every channel.


        This map can be propagated through the shuffle map (see
        `apply_shuffle_map` method) to obtain the contents of the output of
        the corner-turn engine.
        """
        ch_out = {}
        for mb in self.ib:
            for (crate, slot, lane) in mb.get_channel_ids():

                if format == 'l:cscb':  # Unique (crate, slot, local_channel)
                    ch_out[(crate, slot, lane)] = [(crate, slot, lane, bin) for bin in range(1024)]
                elif format == 'l:cc':  # Non-unique global channel numbers (repeated for each bin)
                    if not all(isinstance(i, int) for i in (crate, slot, lane)):
                        raise RuntimeError('get_channel_identity_map requires numeric crate and slot numbers')
                    ch_out[(crate, slot, lane)] = [crate * 256 + slot * 16 + lane for bin in range(1024)]
                elif format == 'l:cb':  # Unique (global channel, lane) tuple
                    if not all(isinstance(i, int) for i in (crate, slot, lane)):
                        raise RuntimeError('get_channel_identity_map requires numeric crate and slot numbers')
                    ch_out[(crate, slot, lane)] = [(crate * 256 + slot * 16 + lane, bin) for bin in range(1024)]
                elif format == 'l:bb':  # Non unique bin_number (repeated for each channel)
                    ch_out[(crate, slot, lane)] = [bin for bin in range(1024)]
        return ch_out

    def get_shuffle_output(self, chan_map):
        """
        Takes the channelizer data map `chan_data` and propagates it through the
        shuffle stages as they are currently configured in the FPGA, and return the resulting data.

        ``chan_data`` is a dictionary of the format {(crate_number, slot, channel_number): [1024 elements],...}

        The elements describing the channel contents can by of any type (complex number, channel & bin tuple, etc.)

        """

        # Get first crossbar map
        # Channels are converted to (crate_number, slot, input_number)
        # Output lane id is converted to (crate_number, slot, lane)

        cb1_out = {}
        for mb in self.ib:
            # extract channels for this motherboard only
            cb1_in = {ch: chan_map[(crate, slot, ch)] for (crate, slot, ch) in mb.get_channel_ids()}
            for lane, data in mb.CROSSBAR.map(cb1_in).items():
                cb1_out[mb.get_id(lane)] = data

        # Apply pcb shuffling
        pcb_shuffle_out = {}
        for cr in self.ic:
            pcb_link_map = cr.get_pcb_link_map()
            for (rx_slot, rx_lane), (tx_slot, tx_lane) in pcb_link_map.items():
                (crate, ) = cr.get_id()
                pcb_shuffle_out[(crate, rx_slot - 1, rx_lane)] = (
                    cb1_out[(crate, tx_slot - 1, tx_lane)] if (crate, tx_slot - 1, tx_lane) in cb1_out
                    else dict(data=[None] * 1024))

        # Apply CROSSBAR2
        cb2_out = {}
        for mb in self.ib:
            (crate, slot) = mb.get_id()
            # extract channels for this motherboard only
            cb2_in = {lane: pcb_shuffle_out[(crate, slot, lane)]
                      for lane in range(mb.BP_SHUFFLE.NUMBER_OF_PCB_LANES)}
            for lane, data in mb.CROSSBAR2.map(cb2_in).items():
                cb2_out[(crate, slot, lane)] = data

        # Apply QSFP shuffling
        qsfp_shuffle_out = {}
        for mb in self.ib:
            (crate, slot) = mb.get_id()
            bypass = mb.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE
            number_of_qsfp_lanes = mb.BP_SHUFFLE.NUMBER_OF_QSFP_LANES
            for rx_lane in range(number_of_qsfp_lanes):
                if bypass:
                    qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out[(crate, slot, rx_lane)]
                else:
                    crate_offset = rx_lane * 2 // number_of_qsfp_lanes
                    qsfp_shuffle_out[(crate, slot, rx_lane)] = cb2_out.get(
                        (crate ^ crate_offset, slot, rx_lane), dict(data=[None] * 2048))

        # Apply CROSSBAR3
        cb3_out = {}
        for mb in self.ib:
            (crate, slot) = mb.get_id()
            # extract channels for this motherboard only
            cb_in = {lane: qsfp_shuffle_out[(crate, slot, lane)]
                     for lane in range(mb.BP_SHUFFLE.NUMBER_OF_QSFP_LANES)}
            for lane, data in mb.CROSSBAR3.map(cb_in).items():
                cb3_out[(crate, slot, lane)] = data

        return cb3_out

    # def get_map(self):
    #     cb1_map = self.CROSSBAR.get_map()
    #     shuffle_map
    #     cb2_map = self.CROSSBAR2.get_map()
    #     return cb1_map

    def set_tx_power(self, default_power=(5, 10), lane_group=None, exceptions=[], index=0):
        """ Set the power level of the corner-turn engine GTX transmitters.

        Parameters:

            default_power (int or tuple): power level to use for all gtx that
                are not exception list. An int is interpreted as a
                single-element tuple. In the case where multiple power values
                are to be specified, the value within the tuple is selected by
                `index`.

            lane_group (str): lane group for which the power is set ('pcb' or
                'qsfp').

            exceptions (list): list of the lane-specific power level exceptin, in the form:

                [((crate, slot, lane), power_tuple), ...}

            index (int): used to select which value within a power tuple, list
                or map will be used to set power.
        """
        if isinstance(default_power, int):
            default_power = (default_power, )

        exceptions = {tuple(node_id): power_tuple for node_id, power_tuple in (exceptions or [])}
        self.logger.info(f'{self.fpga_array!r}: Setting GTX power for lane group {lane_group} to power index {index}')
        self.logger.debug(f'{self.fpga_array!r}:    Default power is {default_power}')
        self.logger.debug(f'{self.fpga_array!r}:    Power exceptions are {exceptions}')

        for mb in self.ib:
            bp = mb.CT.BP_SHUFFLE
            power_tuples = [(lane, exceptions.get(mb.get_id(lane), default_power)[index])
                            for lane, gtx in enumerate(bp.get_gtx(lane_group=lane_group)) if gtx]
            self.logger.debug(f'{self.fpga_array!r}: setting Tx power for {mb} {lane_group} links')
            bp.set_tx_power(power_tuples, lane_group)


    def reset_crossbar_stats(self):
        """ Reset error statistics for the crossbar, crossbar2, and crossbar3.
        """

        for mb in self.ib:
            for cb in [mb.CROSSBAR, mb.CROSSBAR2, mb.CROSSBAR3]:
                if cb:  # make sure the crossbar exists in this firmware
                    cb.reset_stats()

    def reset_bp_shuffle_stats(self):
        """ Reset error statistics for the backplane shuffle.
        """

        for mb in self.ib:
            if mb.BP_SHUFFLE:  # makesure we have a shuffle block in this firmware
                mb.BP_SHUFFLE.reset_stats()

    # ------------------------------------------------------------------
    # Debugging / QC (notebooks, crate tests, BER/eye; not on CRS)
    # ------------------------------------------------------------------

    def detect_backplane_links(self, tx_power=7, print_=True):
        """ Setup all boards on the array to send test pattern over the
        backplane link and detect  from which slot/lane every board is
        receiving data.

        The test is performed only on IceBoards that are installed in crates
        and whose FPGA has been programmed and initialized. Other boards are
        ignored.

        Parameters:

            tx_power (int):

            print_ (bool):


        For now, this test works only if all boards are in a single crate.
        """
        # select only boards on crates and that are open
        array = Ccoll(mb for mb in self.ib if mb.is_open() and mb.crate and mb.slot)

        if not array:
            raise RuntimeError('There are no boards that are opened() AND connected in a crate')

        # Check if the board are all on a single crate
        if len(set(array.crate)) != 1:
            raise RuntimeError(f'Sorry, this method currently can work on only one crate. The currently active boards span multiple crates {list(set(array.crate))}')

        active_slots = [mb.slot for mb in array]
        if len(set(active_slots)) != len(active_slots):
            raise SystemError('Slot numbers are not unique!')

        print('Setting Transmitted ID')
        for mb in array:
            mb.BP_SHUFFLE.TX_DATA_MSB = 0xFF00 + mb.slot
            for gtx_number, g in enumerate(mb.BP_SHUFFLE.gtx):
                g.SOURCE_SEL = 1  # 0:Send TXDATA , 1: SEND 10G Ethernet test packet
                g.LOOPBACK = 0
                g.TXPRBSSEL = 0
                g.RXPRBSSEL = 0
                g.TXDIFFCTRL = tx_power
                g.TX_DATA_LSB = gtx_number + 1
                g.TXHEADER = 1
                g.CAPTURE_ENABLE = 1
                g.TXPRECURSOR = 0b00000  # DFE cannot compensate pre-cursor
                g.TXPOSTCURSOR = 0b00000
                g.RXLPMEN = 0  # Go to DFE mode instead of LPM
                # g.RXMONITORSEL = 1 # 1=AGC, 2=UL, 3=VP loop
                # g.RX_DEBUG_CFG = 0b1011 << 2
                # g.DMONITOR_CFG1 = 0
                # g.DMONITOR_CFG0 = (0b1<<15) | (0x0080 <<1) | 1
                # Configure DMONITOR to read the AGC gain
                # g.DMONITOR_SELECT = 1
                # g.PCS_RSVD_ATTR_BIT6 = 1
                # old=g.read_drp(0x6f)
                # g.write_drp(0x6f,old | 1<<6)
                # g.RXDFEOVRD=1
                # g.write_drp(0x1d, 0x00ea)

        print('Resetting the GTXes')
        for mb in array:
            for g in mb.BP_SHUFFLE.gtx:
                g.RXDFELPMRESET = 1
                g.RXDFELPMRESET = 0
            time.sleep(0.1)

        print('Checking received data')
        link_list = []
        link_matrix = [[None] * 17 for x in range(17)]
        serial_number = ['N/A'] * 17
        for mb in array:
            # print 'Slot %i' % (mb.slot)
            dest_slot = mb.slot
            serial_number[dest_slot - 1] = mb.serial
            # JM: Fixed this below bc was getting an error. JF please check
            for gtx_number, g in enumerate(mb.BP_SHUFFLE.gtx[0:mb.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]):
                dest_lane = gtx_number + 1
                dest = (dest_slot, dest_lane)
                expected_source = mb.crate.get_matching_tx(dest)
                source_present = expected_source[0] in active_slots
                for trial in range(3):
                    rxdata = g.get_rxdata()
                    # print '   Slot %i Lane %i received %08X' % (    mb.slot, lane+1,  rxdata)
                    source_slot = int((rxdata >> 8) & 0xFF)
                    source_lane = int((rxdata) & 0xFF)
                    source = (source_slot, source_lane)
                    source_valid = (rxdata >> 16) == 0xFFFF
                    maybe = (rxdata != 0x55555555) and (rxdata != 0xAAAAAAAA)
                    if source_valid:
                        break

                if source_valid:
                    print(f'Slot {int(dest_slot):2} Lane {int(dest_lane):2} is receiving data from Slot {int(source_slot):2} Lane {int(source_lane):2} (received word = 0x{rxdata:08X}, RXMONITOROUT= {g.RXMONITOR:x}, DMONITOROUT={g.DMONITOROUT:x})')
                    link_matrix[dest_slot][dest_lane] = f'S{int(source_slot):02}L{int(source_lane):02}'
                elif maybe:
                    print(f'Slot {int(dest_slot):2} Lane {int(dest_lane):2} is receiving some data but cannot determine source (received word = 0x{rxdata:08X}, RXMONITOROUT= {g.RXMONITOR:x}, DMONITOROUT={g.DMONITOROUT:x})')
                    link_matrix[dest_slot][dest_lane] = 'S??L??'
                else:
                    link_matrix[dest_slot][dest_lane] = '  ()  '

                if not source_valid or not source_present or source != expected_source:
                    if source_present:
                        link_matrix[dest_slot][dest_lane] += '/S{}L{}  '.format(*expected_source)
                    else:
                        link_matrix[dest_slot][dest_lane] += '/(S{}L{})'.format(*expected_source)

                link_list.append((source, dest))
        if print_:
            # Print a slot map
            print('Slot-> ' + ' '.join([f'{int(slot + 1):<15d}' for slot in range(16)]))
            print('S/N -> ' + ' '.join([f'{sn:15}' for sn in serial_number]))
            print('Lane   ' + ' '.join([f'{"-" * 15:15}' for _ in range(16)]))

            for dest_lane in range(1, 16):
                print(f'{int(dest_lane):6} ', end=' ')
                for dest_slot in range(1, 17):
                    print(f'{link_matrix[dest_slot][dest_lane]:15}', end=' ')
                print()

        return link_list

    def get_backplane_pcb_link_map(self):
        """ Return a dictionary that lists all the backplane PCB links and
        their corresponding GTXes for every boards in the array.

        The list covers every transmitter and receivers on the borads that are
        currently in the array. Each GTX therefore has two entries, one in
        which it is the transmitter, and one in which it is the reciever. The
        connectivity is resolved by relying on the connectivity information
        that is probiced by the IceCrate object, so there is no need for
        additional resolving.

        Parameters:

            None

        Returns:
            A dict, in the format:

            {('pcb', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}
        """
        link_map = {}
        for mb in self.ib:
            link_map.update(mb.BP_SHUFFLE.get_link_map('pcb'))
        return link_map

    def get_backplane_qsfp_links(self):
        """ Get the list of backplane QSFP links and resolve their
        connectivity to return a list of lane connectivity.

        We achieve this by getting the list that matches the QSFP-connected
        GTXes to a cable link ID by calling each crate's get_qsfp_links(),
        which returns a list in the format:

            [('BP_QSFP', (crate_id, iceboard_slot, gtx_index), None, cable_link_id), ...]


        Returns:

            A resolved, motherboard- and lane-oriented connectivity list in the format:

                [('BP_QSFP', (tx_crate, tx_iceboard_slot, tx_lane), (rx_crate, rx_iceboard_slot, rx_lane) ), ...]

            The list refers to logical lane numbers (including the internal
            bypass lanes). Note that the internal (bypass) links are not added
            to the result.


        """
        # Combine TX and RX link dicts from all crates
        raw_links = [link for cr in self.ic
                     for qsfp_link_list in cr.get_qsfp_links()
                     for link in qsfp_link_list]

        for cr in self.ic:
            raw_links += cr.get_qsfp_links()

        # Make a map of crates objects indexed by crate_number
        crate_map = {crate.get_id()[0]: crate for crate in self.ic}

        # Visit each link and find the attached nodes
        links = []
        for (link_type, tx_id, rx_id, link_id) in raw_links:
            # If the second node is not already known, search all the links
            # for a corresponding half-link with the same link_id
            if rx_id is None:
                matching_nodes = [nid1 for (lt, nid1, nid2, lid) in raw_links
                                  if lt == link_type and nid1 != tx_id and nid2 is None and lid == link_id]
                if len(matching_nodes) == 1:
                    rx_id = matching_nodes[0]
            if tx_id is None or rx_id is None:
                continue
            (source_crate, source_slot, source_lane) = tx_id
            (dest_crate, dest_slot, dest_lane) = rx_id
            ic0 = crate_map[source_crate]
            ic1 = crate_map[dest_crate]
            if (source_slot not in ic0.slot) or (dest_slot not in ic1.slot):
                continue
            bp0 = ic0.slot[source_slot].BP_SHUFFLE
            bp1 = ic1.slot[dest_slot].BP_SHUFFLE
            # Append entry. We convert from gtx index to logical lane #
            links.append((
                link_type,
                (source_crate, source_slot, source_lane + bp0.NUMBER_OF_QSFP_DIRECT_LANES),
                (dest_crate, dest_slot, dest_lane + bp1.NUMBER_OF_QSFP_DIRECT_LANES)))

        return links

    def get_backplane_qsfp_link_map(self, resolve=True):
        """ Return a dictionary that maps the backplane QSFP links to
        corresponding GTX transmitter and receiver instances.

        Returns:
           A dict, in the format:

            {('qsfp', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}

        """
        qsfp_link_map = {}
        for mb in self.ib:
            qsfp_link_map.update(mb.BP_SHUFFLE.get_link_map('qsfp'))

        if not resolve:
            return qsfp_link_map

        # Ask each board a map that describe how each logical link is connected to the backplane links
        bp_to_logical_link_map = {}
        for mb in self.ib:
            bp_to_logical_link_map.update(mb.BP_SHUFFLE.get_bp_to_logical_link_map('qsfp'))

        # Ask each crate the map that matches backplane links to cable ids
        # (this takes time: we need to read the cable identification)
        bp_to_cable_map = {}
        for cr in self.ic:
            bp_to_cable_map.update(cr.get_qsfp_cable_map())

        # Create a map that matches each cable id to a list of corresponding
        # logical link ids (there should be 2 for each link)
        cable_to_link_map = {}  # {cable_id: (crate, slot, logical_lane)}
        for (bp_id, cable_id) in bp_to_cable_map.items():
            cable_to_link_map.setdefault(cable_id, []).append(bp_to_logical_link_map[('qsfp', bp_id)])

        # Crate a map that matches each logical link id with another logical link id.
        link_map = {}
        for cable_id, link_ids in cable_to_link_map.items():
            if len(link_ids) == 1:
                self.logger.warning(f'Only one end of a QSFP cable is connected; Cable ID {cable_id} connects only to {link_ids[0]}. The link will be ignored.')
            elif len(link_ids) > 2:
                raise RuntimeError(f'A QSFP cable connects to more than 2 links. Something is wrong. Cable ID {cable_id} connects only to {link_ids}.')
            elif len(link_ids) == 2:
                link_map[link_ids[0][1]] = link_ids[1][1]
                link_map[link_ids[1][1]] = link_ids[0][1]
        print(link_map)

        # Resolve each unresolved link.
        resolved_qsfp_link_map = {}
        for (link_type, tx_id, rx_id), (tx_gtx, rx_gtx) in qsfp_link_map.items():
            if not tx_id and rx_id and rx_id in link_map:
                tx_id = link_map[rx_id]
                tx_gtx, _ = qsfp_link_map[('qsfp', tx_id, None)]
            elif not rx_id and tx_id and tx_id in link_map:
                rx_id = link_map[tx_id]
                _, rx_gtx = qsfp_link_map[('qsfp', None, rx_id)]
            resolved_qsfp_link_map[link_type, tx_id, rx_id] = (tx_gtx, rx_gtx)

        return resolved_qsfp_link_map

    def get_gpu_link_map(self):
        """
        Return a dictionary that matches the GPU links to the GTX Rx and Tx
        instances when connected with a loopback cable.

        Returns:
           A dict, in the format:

            {('GPU', (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}
        """
        link_map = {}
        for mb in self.ib:
            crate_id = mb.get_crate_id()
            slot = mb.slot
            n_lanes = len(mb.GPU.gtx)
            # For the 8-lane 10G links, lanes 0-4 are looped back to 5-8. For 4-lane 100G link, lanes are looped back on themselves.
            for tx_lane in range(n_lanes):
                rx_lane = mb.GPU.LOOPBACK_LANE_MAP[tx_lane]
                link = ('GPU', (crate_id, slot, tx_lane), (crate_id, slot, rx_lane))
                tx = mb.GPU.gtx[tx_lane]
                rx = mb.GPU.gtx[loopback_lanes[tx_lane]]
                link_map[link] = (tx, rx)
        return link_map

    def get_link_map(self, links=None, gtx_only=False):
        """
        Return a dictionary that describes the corner-turn and loopbacked GPU
        links and the corresponding Tx and RX GTXes.


        Is used by `get_ber()` to identify the links on which Bit Error Rate (BER) tests will be performed.

        Parameters:

            links (str or list of str): select only the link type specified as
                a string or the link types specified in a list of strings.

        Returns:

            A dict, in the format::

                {(link_type, (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)): (tx_gtx_instance, rx_gtx_instance)}

            `link_type` is either "BP", "BP_QSFP" or "GPU"
        """
        link_map = {}
        link_map.update(self.get_backplane_pcb_link_map())
        link_map.update(self.get_backplane_qsfp_link_map())
        link_map.update(self.get_gpu_link_map())  # loopback links

        if isinstance(links, str):
            link_map = {link: gtxes for link, gtxes in link_map.items() if link[0] == links}
        elif links is not None:
            link_map = {link: gtxes for link, gtxes in link_map.items() if link in links}

        # Remove links that do not exist or have no GTX (direct lanes)
        if gtx_only:
            link_map = {link: gtxes for link, gtxes in link_map.items() if (('int' not in gtxes) and (None not in gtxes))}

        # link_list.sort(key=lambda (lt, (sc, ss, sl), (dc, ds, dl)): ss * 16 + ds)
        return link_map


    async def get_ber_async(self, link_list=None, period=0.1, tx_power=12, print_=True, tx_init_power=7, tx_qsfp_power=15, minerrors_toprint=0):

        links = self.get_link_map(link_list, gtx_only=True)

        for link, (source_gtx, dest_gtx) in links.items():
            # First, make sure we can get errors by setting the wrong RX PRBS Sequence
            if source_gtx is None or dest_gtx is None or source_gtx == 'int' or dest_gtx == 'int':
                continue
            source_gtx.select()
            source_gtx.TXPRBSSEL = 4

            dest_gtx.select()
            dest_gtx.RXPRBSCNTRESET = 1
            dest_gtx.RXPRBSSEL = 3
            dest_gtx.RXPRBSCNTRESET = 0

            t0 = time.time()
            while True:
                if dest_gtx.get_prbs_error_ctr():
                    break
                if time.time() - t0 > 1:
                    raise SystemError('Cannot detect errors even with the wrong sequence! '
                                      'Are the links connected as expected?')

        # Perform BER test on a single list, to be run in parallel below
        async def one_link_ber_async(link):
            (link_type, (sc, ss, sl), (dc, ds, dl)), (source_gtx, dest_gtx) = link

            dest_gtx.select()
            dest_gtx.RXPRBSSEL = 4
            source_gtx.select()
            source_gtx.TXPRBSSEL = 4
            source_gtx.TXDIFFCTRL = tx_init_power  # Setting initial power level

            dest_gtx.select()
            dest_gtx.RXDFELPMRESET = 1  # Retting the Reciever DFE
            await asyncio.sleep(0.5)
            dest_gtx.RXDFELPMRESET = 0

            source_gtx.select()
            if (link_type == 'pcb'):
                source_gtx.TXDIFFCTRL = tx_power
            else:
                source_gtx.TXDIFFCTRL = tx_qsfp_power

            # if print_:
            #    print 'Measuring BER for link %s' % (link[0],),
            #    print source_gtx.TXDIFFCTRL
            dest_gtx.select()
            dest_gtx.RXPRBSCNTRESET = 1  # Resetting errors
            await asyncio.sleep(0.5)
            dest_gtx.RXPRBSCNTRESET = 0

            await asyncio.sleep(period)

            cnt = dest_gtx.ERR_CTR
            err = (float(cnt) * 16) / (period * 10e9)
            err_max = (float(cnt) * 16 + 1) / (period * 10e9)

            # source_gtx.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
            # dest_gtx.TXPRBSSEL = 0  #Setting PRBS to 0 (data path)
            if print_ and (cnt > minerrors_toprint):
                print(f'{link[0]!r} BER = {err:1.1e} ({cnt} errors, BER<{err_max:1.1e})')
            self.fpga_array.print_flush()
            return err

        # Run BER test on each link in parallel
        ber_table = await asyncio.gather(*[one_link_ber_async((link, gtxes)) for link, gtxes in links.items()])
        return dict(zip(links.keys(), ber_table))

    async def get_ber_vs_power_async(self, links, max_power, period=0.1):

        # links = self.get_link_map(links, gtx_only=True)
        # links = self.scan_links(array, tx_power = max_power)

        power = list(range(0, max_power + 1))
        data = {}
        for tx_power in power:
            e = await self.get_ber_async(links, period=period, tx_power=tx_power)
            for (link, ber) in e.items():
                if link in data:
                    data[link][0].append(tx_power)
                    data[link][1].append(ber)
                else:
                    data[link] = [[tx_power], [ber]]
        return data

    async def plot_ber_vs_power_async(self, data=None, period=0.1, **kwargs):

        if isinstance(data, str):
            data = await self.get_ber_vs_power_async(links=data, period=period, **kwargs)

        for link, (tx_power, ber) in data.items():
            print(f"{link:>20} {','.join([f'{b:6.1g}' for b in ber])}")
        plt.figure(1)
        plt.clf()

        for link, (tx_power, ber) in sorted(data.items(), key=str):
            plt.semilogy(tx_power, (np.array(ber) + 1e-12), label=f'Link {link}')
        plt.title(f'BER of links as a function of TX power (period={period:0.1f} s)')
        plt.xlabel('TX power (0-15)')
        plt.ylabel('BER')
        leg = plt.legend(loc='best', fontsize='small', markerscale=3, framealpha=0.6, shadow=True)
        plt.setp(leg.get_lines(), linewidth=2)  # make legend lines thicker so we can see the color better
        plt.grid(1)

    def get_eye_matrix(self, h_step=10, v_step=40):
        link_map = self.detect_backplane_links()
        eye_matrix = {}

        for link in link_map:
            ((from_slot, from_lane), (to_slot, to_lane)) = link
            print(f"###### running from slot {int(from_slot)} lane {int(from_lane)} to slot {int(to_slot)} lane {int(to_lane)} #######")
            gtx = self.ib.get(slot=to_slot).BP_SHUFFLE.gtx[to_lane - 1]
            e = gtx.get_eye_diagram(list(range(-32, 32, h_step)), list(range(-127, 128, v_step)))
            eye_matrix[link] = e
        return eye_matrix

    @staticmethod
    def plot_eye_matrix(eye_matrix):
        plt.figure(1)
        plt.clf()

        source_slots = [ss for ((ss, sl), (ds, dl)) in list(eye_matrix.keys())]
        dest_slots = [ds for ((ss, sl), (ds, dl)) in list(eye_matrix.keys())]
        slots = sorted(set(source_slots + dest_slots))

        # slot_map = {slot: ix for (ix, slot) in enumerate(slots)}
        slot_map = {x + 1: x for x in range(16)}

        (fig, ax) = plt.subplots(
            len(slot_map), len(slot_map),
            sharex=True, sharey=True, subplot_kw={'axis_bgcolor': 'black'})
        fig.subplots_adjust(wspace=0, hspace=0)
        fig.suptitle('Backplane 10Gbps mesh eye diagrams\n TX slot #: left, Rx slot #: bottom')

        for (slot, ix) in slot_map.items():
            # Bottom images
            a = ax[ix, 0]
            a.tick_params(labelsize=6)
            a.set_ylabel(f"TX S{int(slot):02}", fontsize=10)
            # Left images
            a = ax[len(slot_map) - 1, ix]
            a.tick_params(labelsize=6)
            a.set_xlabel(f"RX S{int(slot):02}")
            plt.setp(a.xaxis.get_majorticklabels(), rotation=70)
            # Diagonal images
            a = ax[ix, ix]
            a.patch.set_color('black')

        for (((ss, sl), (ds, dl)), eye) in eye_matrix.items():
            a = ax[slot_map[ss], slot_map[ds]]
            a.imshow(
                np.log10(eye.ber_map + 1e-12),
                origin='lower', extent=(-32, 32, -128, 128), aspect='auto', vmin=-12, vmax=1)
        plt.draw()

    async def print_crossbar2_frame_info(self, reset_stats=False, grid=True, width=180):
        slots = Ccoll(self.ib, self.ib.slot)  # Get iceboards indexed by slot number

        crate = set(slots.crate)
        if len(crate) == 1:
            crate = crate.pop()
        else:
            raise RuntimeError(f'Sorry, this method currently can work on one and only one crate. The currently active boards either have no crates or span multiple crates {list(set(slots.crate))}')

        slot_range = list(range(1, 17))
        lane_range = list(range(16))
        slot_labels = [slots[s].serial if s in list(slots.keys()) else 'N/A' for s in slot_range]

        captured_source = {}
        computed_source = {}
        valid_source = {}
        info = {}
        expected_bp_frame_size = 37
        for dest_slot in slot_range:
            if dest_slot in slots.keys():
                cb = slots[dest_slot].CROSSBAR2
                bp = slots[dest_slot].BP_SHUFFLE
                shuffle_to_bin_sel_lane_map = cb.get_reverse_lane_map()
                remapped_direct_lane = shuffle_to_bin_sel_lane_map[0]
                if reset_stats:
                    bp.reset_stats()
                # Header capture lives on the crossbar. The bin selector no
                # longer has these methods, and the reads are coroutines.
                stream_ids = cb.capture_stream_id()
                frame_number = await cb.capture_frame_number()
                frame_number = [f - frame_number[remapped_direct_lane] for f in frame_number]
                captured_source = {remapped_dest_lane: (((s >> 4) & 0xF) + 1, (s & 0xF))
                                   for remapped_dest_lane, s in enumerate(stream_ids)}
                computed_source = {shuffle_to_bin_sel_lane_map[dest_lane]: crate.get_matching_tx((dest_slot, dest_lane))
                                   for dest_lane in lane_range}
                # INPUT_FRAME_CTR is the "has this lane delivered a frame" flag.
                # There is no INPUT_DETECT monitor, and the RX overflow flag is
                # RX_FIFO_OVERFLOW.
                (input_frame_ctr, missing, align_ovf) = await cb.get_lane_monitor(
                    ['INPUT_FRAME_CTR', 'MISSING_FRAME', 'ALIGN_FIFO_OVERFLOW'])
                (rx_err, rx_ovf, rx_maxlen) = await bp.get_rx_lane_monitor(
                    ['ERROR_CTR', 'RX_FIFO_OVERFLOW', 'MAX_FRAME_LENGTH'])
                valid_source = {shuffle_to_bin_sel_lane_map[dest_lane]: bool(v)
                                for dest_lane, v in enumerate(input_frame_ctr)}
                missing_frame = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                 for dest_lane, v in enumerate(missing)}
                align_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                       for dest_lane, v in enumerate(align_ovf)}
                rx_errors = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                             for dest_lane, v in enumerate(rx_err)}
                rx_fifo_overflow = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                    for dest_lane, v in enumerate(rx_ovf)}
                rx_max_frame_length = {shuffle_to_bin_sel_lane_map[dest_lane]: v
                                       for dest_lane, v in enumerate(rx_maxlen)}

            else:
                captured_source = {dest_lane: None for dest_lane in lane_range}
                computed_source = {dest_lane: crate.get_matching_tx((dest_slot, dest_lane)) for dest_lane in lane_range}
                valid_source = {dest_lane: False for dest_lane in lane_range}
                missing_frame = {dest_lane: False for dest_lane in lane_range}
                align_fifo_overflow = {dest_lane: False for dest_lane in lane_range}
                rx_fifo_overflow = {dest_lane: False for dest_lane in lane_range}
                rx_errors = {dest_lane: 0 for dest_lane in lane_range}
                rx_max_frame_length = {dest_lane: expected_bp_frame_size for dest_lane in lane_range}
                frame_number = None

            info[dest_slot] = {}
            for remapped_dest_lane in lane_range:
                cap_src = captured_source[remapped_dest_lane]
                comp_src = computed_source[remapped_dest_lane]
                valid_src = valid_source[remapped_dest_lane]
                data_expected = not ((comp_src[0] not in slots.keys()) or (dest_slot not in slots.keys()))
                cell = ' ' if (cap_src and cap_src == comp_src and valid_src) or not data_expected else '!'
                cell += ('------' if not cap_src or not data_expected
                         else '--??--' if not valid_src else f'S{cap_src[0]:02d}L{cap_src[1]:02d}')
                cell += f'/S{comp_src[0]:02d}L{comp_src[1]:02d}'
                if frame_number is not None and data_expected and any(frame_number):
                    cell += f' F={frame_number[remapped_dest_lane]:02x}'
                if data_expected:
                    if missing_frame[remapped_dest_lane]:
                        cell += '\n  MISSING FRAMES'
                    if align_fifo_overflow[remapped_dest_lane]:
                        cell += '\n  ALIGN FIFO OVERFLOW'
                    if rx_fifo_overflow[remapped_dest_lane]:
                        cell += '\n  RX FIFO OVERFLOW'
                    if rx_errors[remapped_dest_lane]:
                        cell += f'\n  RX ERR = {int(rx_errors[remapped_dest_lane])}'
                    if rx_max_frame_length[remapped_dest_lane] != expected_bp_frame_size:
                        cell += f'\n  RX FRAME = {int(rx_max_frame_length[remapped_dest_lane])} Words'
                info[dest_slot][remapped_dest_lane] = cell

        print('Post-remap, crossbar2 bin selector input lane indentification')

        corner_label = 'Slot->\nS/N ->\n\\|/Lane'
        col_labels = [f'{int(slot_range[i])}\n{slot_labels[i]}' for i in range(len(slot_range))]
        row_labels = lane_range
        self.print_table(info,
                         row_labels=row_labels, col_labels=col_labels, corner_label=corner_label,
                         line_sep=grid, max_width=width)

    async def get_corner_turn_engine_status_async(self, reset_stats=False):
        """ Gather info on the status of the corner-turn engine for all boards of the array installed in crates.

        The information includes status info on every lane of  the following corner turn subsystems:

            - Every lane (16) of the backplane PCB shuffle (shuffle between boards in a crate)
            - Every lane (8)  of the backplane QSFP shuffle (shuffle between crates)
            - Every lane (16) of crossbar 2
            - Every lane (8) of crossbar 3

        Parameters:

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

        Returns:

            A list of dict containing the corner-turn engine status flags for each crate, each dist following the schema::

                "cr": <crate_object>: # Crate object
                "slots":
                    slot_number1: # Slot number as found in Motherboard.slot (int)
                        "mb": <mb_object> # Motherbpard object
                        "serial": <serial_number>  # Motherboard Serial number, as found in Motherboard.serial (str)
                        "subsystems":
                            subsystem1: # Subsystem name (str)
                                "status": <subsystem_status>  # Subsystem aggregate status for all lanes (bool)
                                "lanes":
                                    lane_number1:  # Lane number (int)
                                        "status": Lane status (True if there are any flags) (bool)
                                        "label": Full length label for the lane (subsystem & lane) (str)
                                        "fields":
                                            "Tx": # Tuple identifying the transmitter (tuple)
                                            "Rx": # Tuple identifying the receiver (tuple)
                                            field1_name: <field1_value> # field status. Field name is a str. Field value can be anything
                                            field2_name: <field2_value>
                                        ...
                                    lane_number2:
                                    ...
                            subsystem2:
                        ...
                    slot_number2:
                    ...

        Example:

            ::

                info[0]['slots'][2]['subsystems']['BP PCB']['status']
                info[0]['slots'][2]['subsystems']['BP PCB']['lanes'][3]['status']

            Print the summary of every lanes of every subsystem for every slot::

                info = await ca.get_corner_turn_engine_status_async(reset_stats=True)
                [f"Crate {c} Slot {slot_number} {sub_name} { [lane['status'] for lane in sub['lanes'].values()]}"
                   for c in info
                   for slot_number, slot in c['slots'].items()
                   for sub_name, sub in slot['subsystems'].items()]

            Get the status of the first crate as a dict ``{slot_number:{subsystem:status, ...}, ...}``::

                status =  {slot_number:{sub_name:sub['status'] for sub_name, sub in slot['subsystems'].items()}
                           for c in info[0] for slot_number, slot in c['slots'].items()}
        """
        info = []

        for crate_obj in self.ic:
            # initialize the dictionary the will contain the infor for each slot in the current crate
            crate = {}  # {crate_object: slot_dict}
            info.append(crate)
            slot_range = list(range(1, crate_obj.NUMBER_OF_SLOTS + 1)) or [None]

            crate['cr'] = crate_obj
            # gather info for each currently populated slot
            crate['slots'] = {}
            for slot_number in slot_range:
                crate['slots'][slot_number] = slot = {}
                slot['mb'] = None
                slot['serial'] = None
                slot['subsystems'] = {}
                if slot_number not in crate_obj.slot:
                    continue
                # initialize the dict for each lane
                mb = crate_obj.slot[slot_number]
                slot['mb'] = mb  # motherboard object in this slot
                slot['serial'] = mb.serial  # motherboard object in this slot
                # Gather status from the backplane PCB and QSFP links
                for lane_group in mb.BP_SHUFFLE.lane_group_names:
                    subsystem_name = f'BP {lane_group.upper()}'
                    slot['subsystems'][subsystem_name] = subsystem = {}  # {lane:status_dict, ...}
                    subsystem['lanes'] = {}
                    if reset_stats:
                        mb.BP_SHUFFLE.reset_stats()
                    # ee: list of N error status dicts  [lane0_errors, lane1 errors, ...]
                    status_dicts = await mb.BP_SHUFFLE.get_bp_rx_status(lane_group)
                    for lane_number, status_dict in enumerate(status_dicts):
                        subsystem['lanes'][lane_number] = lane = {}
                        lane['status'] = bool(status_dict) # True if there were any error flags returned for this lane
                        lane['label'] = f'{subsystem_name} L{lane_number:02d}'
                        if lane_group == 'pcb':  # if verbose, add the matching TX (slot,lane) for each rx lane on that slot
                            status_dict['Tx'] = f'{mb.crate.get_matching_tx((slot_number, lane_number))}'
                            status_dict['Rx'] = f'{(slot_number, lane_number)}'
                        lane['fields'] = status_dict
                        # print(f'added {subsystem_name} {lane_number} = {status_dict}')
                    # generate a subsystem status summary
                    subsystem['status'] = any(status_dict['status'] for status_dict in subsystem['lanes'].values())

                    # errs.append(ee)

                # Gather status from the crossbars
                for subsystem_prefix, cb in (('CB2', mb.CROSSBAR2), ('CB3', mb.CROSSBAR3)):
                    if reset_stats:
                        cb.reset_stats()

                    # Get the status flags. These are returned as a list of dict, one dict per lane.
                    align_status_dicts = await cb.get_align_status()
                    frame_status_dicts = await cb.get_frame_alignment_status()
                    bin_sel_status_dicts = await cb.get_bin_sel_status()
                    for subsystem_suffix, status_dicts in (('ALIGN', align_status_dicts),
                                                           ('FRAME #', frame_status_dicts),
                                                           ('BIN_SEL', bin_sel_status_dicts)):
                        subsystem_name = f'{subsystem_prefix} {subsystem_suffix}'
                        slot['subsystems'][subsystem_name] = subsystem = {}  # {lane:status_dict, ...}
                        subsystem['lanes'] = {}
                        for lane_number, status_dict in enumerate(status_dicts):
                            subsystem['lanes'][lane_number] = lane = {}
                            lane['status'] = bool(status_dict) # True if there were any error flags returned for this lane
                            lane['label'] = f'{subsystem_name} L{lane_number:02d}'
                            lane['fields'] = status_dict
                        # generate a subsystem status summary
                        subsystem['status'] = any(status_dict['status'] for status_dict in subsystem['lanes'].values())
        return info

    def _print_shuffle_status(self, info, verbose=1, grid=False) -> None:
        """ Formats and prints the status information of the corner-turn engine


        Parameters:

            info (list[dict]): output of get_corner_turn_engine_status_async that we wish to print

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

            verbose (int): Determine how much status information is returned

                verbose=0: Will provide the '-' or 'ERR' for each subsystem  depending on whether there are any errors on any lane in the subsystem

                verbose=1: Will provide '-' or 'ERR' for each lane of the sybsystem depending on whether there are any errors on for each lane in the subsystem

                verbose=2: Will provide detailed status info on each lane of the subsystem in the form of a dict. Will also add the coordinates of the Tx/Rx pairs involved in backplane links.

            grid (bool): If true, line separators will be used

        """
        for crate in info:
            # Print the table
            corner_label = 'Slot->\nS/N ->\n\\|/Lane'
            # slot_labels = ['SN%s' % slot_dict['mb'].serial if slot_dict['mb'] else 'N/A' for slot_dicts in info[crate].values()]
            col_labels = [f"SN{slot['serial']}\n{slot_number}" for slot_number, slot in crate['slots'].items()]
            # row_labels = ['BP PCB Rx\nBP QSFP Rx\nCB2 FIFO\nCB2 ALIGN\nCB2 FRAMEnCB3 FIFO\nCB3 ALIGN\nCB3 FRAME\n']
            row_labels = []
            data = []

            slots = list(crate['slots'].values())
            first_slot = [slot for slot in slots if slot['mb']][0]
            if not verbose:
                # verbose = 0. We print summary status info only for each subsystem
                row_labels = list(first_slot['subsystems'].keys())
                data = [[('-', 'ERR')[subsystem['status']] for subsystem in slot['subsystems'].values()] for slot in slots]
            else:
                # verbose != 0 : We print info on each lane
                row_labels = [lane['label'] for subsystem in first_slot['subsystems'].values() for lane in subsystem['lanes'].values()]
                # create an 2-dimensional array of lane dicts
                all_lanes = [[lane for subsystem in slot['subsystems'].values()
                                   for lane in subsystem['lanes'].values()
                            ] for slot in slots]
                if verbose == 1:
                    # verbose = 1: we print summary info for each lane
                    data = [[('-', 'ERR')[lane['status']] for lane in lanes]
                            for lanes in all_lanes]
                elif verbose > 1:
                    # verbose > 1: we print all status info for each lane
                    data = [['\n'.join(f'{key}={value}' for key, value in lane['fields'].items())
                             for lane in lanes] for lanes in all_lanes]

            # for label, lanes in [('BP PCB Rx', 16), ('BP QSFP Rx', 8), ('CB2 ALIGN', 16),
            #                      ('CB2 FRAME #', 16), ('CB2 BIN_SELs', 2), ('CB3 ALIGN', 8),
            #                      ('CB3 FRAME #', 8), ('CB3 BIN SELs', 8)]:
            #     if verbose and lanes:
            #         row_labels += ['%s L%02i' % (label, lane) for lane in range(lanes)]
            #     else:
            #         row_labels += [label]
            # return info
            # print 'row_labels=', row_labels
            # print 'col_labels=', col_labels
            # print 'data=', info
            self.print_table(
                data, row_labels=row_labels, col_labels=col_labels,
                corner_label=corner_label, line_sep=grid)

    async def print_shuffle_status_async(self, reset_stats=False, verbose=1, grid=False):
        """ Prints status information of the corner-turn engine


        Parameters:

            reset_stats (bool): if True, the statistics on the corner turn subsystems will be reset before being measured.

            verbose (int): Determine how much status information is returned

                verbose=0: Will provide the '-' or 'ERR' for each subsystem  depending on whether there are any errors on any lane in the subsystem

                verbose=1: Will provide '-' or 'ERR' for each lane of the sybsystem depending on whether there are any errors on for each lane in the subsystem

                verbose=2: Will provide detailed status info on each lane of the subsystem in the form of a dict. Will also add the coordinates of the Tx/Rx pairs involved in backplane links.

            grid (bool): If true, line separators will be used

        """

        info = await self.get_corner_turn_engine_status_async(reset_stats=reset_stats)
        self._print_shuffle_status(info, verbose=verbose, grid=grid)

    async def print_rx_err_map(
            self,
            icecrates=None,
            reset_stats=0,
            delay=-5,
            tx_power=None,
            tx_precursor=None,
            tx_postcursor=None,
            lpm=None,
            dfe_reset=False,
            stop_on_errors=2,
            verbose=1):
        """
        Test the backplane PCB and QSFP link operation. Allows various
        parameters of the GTX transceivers to be changed to measure their
        effect.

        Parameters:

            icecrates (IceCrate Ccoll): List of icecrates on which the test is
                done. If `None`, all icecrates are initialized/tested.

            reset_stats (bool): Reset the statistics counters. Default is false.

            delay (int):

            tx_power (int): transmit power to be set on the GTX. Ranges from
                0-15. If ``None``, TX power levels ar enot changed.

            tx_precursor: None,

            tx_postcursor: None

            lpm (bool): If true, enables Low Power Mode in the GTX. Requires a dfe_reset.

            dfe_reset (bool): If true, resets the Dynamix Feedback Equalizer
                to allow it to find a new equalization solution. Is needed
                when power levels or operaiton mode are changed

            stop_on_errors : =2,

            verbose=1
        """
        if icecrates is None:
            icecrates = self.ic

        ibs = Ccoll.chain(*icecrates.slot.item_values())
        gtx = Ccoll.chain(*ibs.BP_SHUFFLE.gtx)

        time_without_error = [None] * len(icecrates)
        has_errors = [False] * len(icecrates)

        if lpm is not None:
            gtx.RXLPMEN = lpm

        if tx_power is not None:
            ibs.BP_SHUFFLE.set_tx_power(tx_power)

        if tx_precursor is not None:
            gtx.TXPRECURSOR = tx_precursor

        if tx_postcursor is not None:
            gtx.TXPOSTCURSOR = tx_postcursor

        if dfe_reset:
            gtx.reset_rx_equalizer()
            await asyncio.sleep(0.1)

        if reset_stats:
            self.ib.BP_SHUFFLE.reset_stats()

        t0 = time.time()

        finished = False
        try:
            while not finished:
                await asyncio.sleep(abs(delay))
                dt = time.time() - t0
                print('At', time.asctime(), f'({datetime.timedelta(seconds=dt)} seconds since the method call)')
                for i, cr in enumerate(icecrates):
                    err_map = [[None] * cr.NUMBER_OF_SLOTS for _ in range(cr.NUMBER_OF_SLOTS)]
                    # print  cr.slot.values()
                    worst_err = 0
                    worst_det = 1
                    for slot, mb in cr.slot.items():
                        errs, det = await mb.BP_SHUFFLE.get_rx_lane_monitor(
                            ['ERROR_CTR', 'FRAME_DETECT'], lane_group='pcb')
                        worst_err = max(worst_err, max(errs))
                        worst_det = min(worst_det, min(det))

                        if not has_errors[i]:
                            time_without_error[i] = dt

                        if (any(errs) or not all(det)):
                            has_errors[i] = True

                        for lane in range(16):
                            rx_lane_id = (slot, lane)
                            tx_lane = mb.BP_SHUFFLE.get_matching_tx_node_id(rx_lane_id)
                            err_map[rx_lane_id[0] - 1][tx_lane[0] - 1] = (
                                errs[lane] if errs[lane] else '!DET' if not det[lane] else '-')
                            # print '%s -> %s = %i' % (tx_lane, rx_lane_id, errs[lane])
                    row_labels = [f'Tx S{int(mb.slot):02} SN{mb.serial}' for mb in cr.slot.values()]
                    col_labels = [f'Rx S{int(mb.slot):02}\nSN{mb.serial}' for mb in cr.slot.values()]
                    corner_label = f'{cr.get_string_id()}\nCrate #{cr.crate_number}'
                    err_or_frames = (
                        'No Frames!' if not worst_det else f'{worst_err} errors'
                    )
                    print(
                        f'    {cr.get_string_id()}: {err_or_frames:<10} '
                        f'{datetime.timedelta(seconds=int(time_without_error[i]))} without errors '
                        f"{'so far' if not has_errors[i] else ''}"
                    )
                    if verbose:
                        self.print_table(
                            err_map, row_labels=row_labels, col_labels=col_labels,
                            corner_label=corner_label)
                if (stop_on_errors == 1 and any(has_errors)) or (stop_on_errors > 1 and all(has_errors)):
                    break
        except KeyboardInterrupt:
            pass
