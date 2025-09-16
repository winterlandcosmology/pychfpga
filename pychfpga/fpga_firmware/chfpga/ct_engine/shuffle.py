"""
shuffle.py module
    Implements interface to the backplane or intercrate shuffle module

History:
    2013-10-29 : JFC : Created
"""
from . import xglink


class Shuffle(xglink.XGLinkArray):
    """ Object that represents the GTX data links of the FPGA-based
    corner-turn engine. Both the PCB links (between boards in the same crate)
    and the backplane QSFP links (between boards in two different crates) are
    included.

    As opposed to the base class :class:`XGLinkArray`, this class is aware of the
    crate in which the board is located, and how the links are connected
    through PBC lanes or QSFP cables.
    """

    def __init__(self, *, parent_module, router_port, verbose=1):

        self.NUMBER_OF_PCB_DIRECT_LANES = 1
        self.NUMBER_OF_QSFP_DIRECT_LANES = 4
        self.NUMBER_OF_PCB_LINKS = 15
        self.NUMBER_OF_QSFP_LINKS = 4

        self.lane_groups = (('pcb', self.NUMBER_OF_PCB_DIRECT_LANES, self.NUMBER_OF_PCB_LINKS),
                            ('qsfp', self.NUMBER_OF_QSFP_DIRECT_LANES, self.NUMBER_OF_QSFP_LINKS))

        self.lane_group_names = [name for (name, _, _) in self.lane_groups]

        super().__init__(parent_module=parent_module, router_port=router_port, lane_groups=self.lane_groups, verbose=verbose)

        self.NUMBER_OF_PCB_LANES = 16
        self.NUMBER_OF_QSFP_LANES = 8
        self.NUMBER_OF_LANES = self.NUMBER_OF_LINKS + 5  # 1 bypass link for BP PCB shuffle, 4 for Bp QSFP shuffle

    def get_matching_tx_node_id(self, rx_node_id):
        """ Return the PCB link transmitter node id ( a (slot, lane) tuple)
        that corresponds to the specified receiver node id.

        Parameters:

            rx_node_id (tuple): A (slot, lane) tuple that identifies the PCB link receiver

        Returns:

            A (slot, lane) tuple that corresponds to the specified receiver node id
        """
        return self.fpga.crate.get_matching_tx(rx_node_id)

    def get_matching_rx_node_id(self, tx_node_id):
        """ Return the PCB link receiver node id ( a (slot, lane) tuple)
        that corresponds to the specified transmitter node id.

        Parameters:

            tx_node_id (tuple): A (slot, lane) tuple that identifies the PCB link transmitter

        Returns:

            A (slot, lane) tuple that corresponds to the specified receiver node id
        """
        return self.fpga.crate.get_matching_rx(tx_node_id)

    def get_rx_net_length(self, rx_node_id):
        """ Return the length of the PCB link connected to the specified receiver node.

        This method can be useful if we need to optimize the transmit power as
        a function of estimated losses in the link.

        Parameters:

            rx_node_id (tuple): A (slot, lane) tuple that identifies the PCB link receiver

        Returns:

            float that indicates the trace length in mils.
        """
        return self.fpga.crate.get_rx_net_length(rx_node_id)

    def get_link_map(self, lane_group='pcb'):
        """ Return a dictionary that lists all the corner-turn engine data
        lanes, and associated GTX Tx and Rx instances when possible.

        Parameters:

            lane_group (str): type of backplane links to include in the list
                ('pcb' or 'qsfp'). If None, both types will be included.

        Returns:
            A dictionary in the format:

                { (link_type, tx_id, rx_id) : (tx_gtx, rx_gtx)}


            Where link_type is either 'BP' or 'BP_QSFP', and where `tx_id`
            and `rx_id` are tuples in the format: ``(crate_id, slot_id,
            lane_id)``. All indices in the id tuples are zero-based.

        A link is resolved when *both* the `tx_id` and `rx_id` are
        known. This does not necessarily mean that the link is connectd to
        hardware, but only that it could potentially be there.

        A link is actually connected only if both `tx_gtx` and `rx_gtx` are
        defined as an instance of the correcponding GTX object if the link
        goes through a actual GTX link, or as the 'int' string if the link is
        established through the internal bypass link. :

        Examples:

            Resolved and connected link, through wither GTXes or internal links

                ('BP', tx_id, rx_id) : (tx_gtx, rx_gtx)
                ('BP', tx_id, rx_id) : ('int', 'int')

            Resolved link that is not connected (i.e. missing board on a backplane PCB link):

                ('BP', tx_id, rx_id) : (tx_gtx, None)

            The two ends of a link that yet unresolved (i.e. the cabling will have to be checked):

                ('BP_QSFP', tx_id, None) : (tx_gtx, None)
                ('BP_QSFP', None, rx_id) : (None, rx_id)

        'pcb' links are always resolved, since the method has access to the
        icecrate object, which is aware of which node should be at the other
        end. All possible PCB links are listed, even if they are not connected
        at one or both end becaus eof missing boards. Lane 0 is always a
        connected internal link that connect to the same lane of the same
        board.

        With the exception of the internal direct links, 'qsfp' links are
        always unresolved, since this method has no knowledge of other crates.
        There is an unresolved entry for each receiver and transmitter, which
        will have to be resolved based on the cable id.

        The link map takes into account whether the transmitters are are
        currently in bypass mode and are sending the data to itself instead of
        an other board.

        """
        links = {}
        (crate, slot) = self.fpga.get_id()

        lane_groups = ['pcb', 'qsfp']
        if lane_group is None:
            groups = self.lane_group_names
        elif lane_group in self.lane_group_names:
            groups = [lane_group]
        elif all(group in self.lane_group_names for group in lane_group):
            groups = lane_group
        else:
            raise ValueError('Invalid lane group. Can be one of %r' % lane_groups)

        # cache the bypass flags obtained from the FPGA
        bypass_pcb_shuffle = self.BYPASS_PCB_SHUFFLE
        bypass_qsfp_shuffle = self.BYPASS_QSFP_SHUFFLE

        # Create the pcb link map. We include all the possible PCB links that
        # are offered by the backplane, even if there
        for group in groups:
            for lane, gtx in enumerate(self.get_gtx(lane_group=group)):
                if group == 'pcb':
                    if self.is_gtx(gtx) and not bypass_pcb_shuffle:  # do we have a real external link?
                        # Add the link connected to the receiver side of the GTX
                        (rx_slot, rx_lane) = (slot, lane)
                        rx_id = (crate, rx_slot, rx_lane)
                        rx_gtx = gtx
                        (tx_slot, tx_lane) = self.get_matching_tx_node_id((rx_slot + 1, rx_lane))
                        tx_id = (crate, tx_slot - 1, tx_lane)
                        tx_ib = self.fpga.crate.slot.get(tx_slot, None)
                        tx_gtx = tx_ib.BP_SHUFFLE.get_gtx(tx_lane, group) if tx_ib else None
                        links[(group, tx_id, rx_id)] = (tx_gtx, rx_gtx)  # No transmitter

                        # Add the link connected to the transmitter side of the GTX
                        (tx_slot, tx_lane) = (slot, lane)
                        tx_id = (crate, tx_slot, tx_lane)
                        tx_gtx = gtx
                        (rx_slot, rx_lane) = self.get_matching_rx_node_id((tx_slot + 1, tx_lane))
                        rx_id = (crate, rx_slot - 1, rx_lane)
                        rx_ib = self.fpga.crate.slot.get(rx_slot, None)
                        rx_gtx = rx_ib.BP_SHUFFLE.get_gtx(rx_lane, group) if rx_ib else None
                        links[(group, tx_id, rx_id)] = (tx_gtx, rx_gtx)

                    else:  # if a direct internal link or a software bypass
                        tx_id = rx_id = (crate, slot, lane)
                        tx_gtx = rx_gtx = 'int'  # internal link
                        links[(group, tx_id, rx_id)] = (tx_gtx, rx_gtx)
                elif group == 'qsfp':
                    tx_id = rx_id = (crate, slot, lane)
                    if self.is_gtx(gtx) and not bypass_qsfp_shuffle:
                        links[(group, None, rx_id)] = (None, gtx)
                        links[(group, rx_id, None)] = (gtx, None)
                    else:  # if a direct internal link or a software bypass
                        links[(group, rx_id, tx_id)] = ('int', 'int')  # already resolved
            return links

    def get_bp_to_logical_link_map(self, lane_group=None):
        """ Return a map that matches the backplane link id (i.e. relative to
        the backplane connector pinout) to logical links (i.e related to the
        lane groups).

        The map is built to match the routing of the IceBoard and the firmware
        assignments of the GTX and direct lanes in various lane groups. The
        map include both the PCB and QSFP mappings.

        This map can be used to relate the connectivity provided by the
        backplane to connectivity from the point of view of the firmware.


        Returns:

            dict: A dictionary in the format::

                    {backplane_link_id: logical_link_id, ...}

                where

                - ``backplane_link_id`` is a ``('qsfp', (crate, slot, bp_lane))`` tuple that
                refers to the backplane link
                - ``logical_link_id`` is a ``('qsfp', (crate, slot, logical_lane))`` that refer to a logical link.

        """

        (crate, slot) = self.fpga.get_id()

        lane_groups = ['pcb', 'qsfp']
        if lane_group is None:
            groups = self.lane_group_names
        elif lane_group in self.lane_group_names:
            groups = [lane_group]
        elif all(group in self.lane_group_names for group in lane_group):
            groups = lane_group
        else:
            raise ValueError('Invalid lane group. Can be one of %r' % lane_groups)

        # cache the bypass flags obtained from the FPGA
        bypass_pcb_shuffle = self.BYPASS_PCB_SHUFFLE
        bypass_qsfp_shuffle = self.BYPASS_QSFP_SHUFFLE

        bp_to_logical_link_map = {}
        for group in groups:
            for lane, gtx in enumerate(self.get_gtx(lane_group=group)):
                logical_id = (group, (crate, slot, lane))
                if group == 'pcb' and self.is_gtx(gtx) and not bypass_pcb_shuffle:  # do we have a real external link?
                    bp_id = (group, (crate, slot + 0, lane - self.NUMBER_OF_PCB_DIRECT_LANES))
                    bp_to_logical_link_map[bp_id] = logical_id
                elif group == 'qsfp' and self.is_gtx(gtx) and not bypass_qsfp_shuffle:
                    bp_id = (group, (crate, slot + 0, lane - self.NUMBER_OF_QSFP_DIRECT_LANES))
                    bp_to_logical_link_map[bp_id] = logical_id
            return bp_to_logical_link_map

    def get_links(self):
        """
        Return a list of all the backplane links in the format
        (link_type, (source_crate, source_slot, source_lane), (dest_crate, dest_slot, dest_lane)).

        Takes into account the BYPASS mode.
        """
        return list(self.get_link_map().keys())
