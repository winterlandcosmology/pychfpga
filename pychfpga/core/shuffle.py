"""
shuffle.py module
    Implements interface to the backplane or intercrate shuffle module

History:
    2013-10-29 : JFC : Created
"""
import xglink

from Module import BitField

# Types of memory-mapped registers
CONTROL = BitField.CONTROL
STATUS = BitField.STATUS
DRP = BitField.DRP


class Shuffle(xglink.XGLinkArray):
    """ Instantiates an object that represents the backplane shuffle

    A backplane shuffle object is a pure xglink_array object, so nothing is added to the class.
    """
    pass

    def get_matching_tx_node_id(self, rx_node_id):
        return self.fpga.crate.get_matching_tx(rx_node_id)

    def get_rx_net_length(self, rx_node_id):
        return self.fpga.crate.get_rx_net_length(rx_node_id)

    def get_link_map(self):
        links = {}
        ds = self.fpga.slot
        sc = self.fpga.get_crate_id()

        for dl in range(self.NUMBER_OF_PCB_LANES):
            if self.BYPASS_PCB_SHUFFLE:
                (ss, sl) = (ds, dl)
                rx = None
                tx = None
            else:
                (ss, sl) = self.fpga.crate.get_matching_tx((ds, dl))
                rx = self.gtx[dl-self.NUMBER_OF_PCB_DIRECT_LANES] if dl >= self.NUMBER_OF_PCB_DIRECT_LANES else None
                source_ib = self.fpga.crate.slot.get(ss, None)
                tx = source_ib.BP_SHUFFLE.gtx[sl-self.NUMBER_OF_PCB_DIRECT_LANES] if source_ib else None
            link = ('BP', (sc, ss, sl), (sc, ds, dl))
            links[link] = (tx, rx)
        return links

    def get_links(self):
        """
        Return a list of all the backplane links in the format
        (link_type, (source_crate, source_slot, source_lane), (dest_crate, dest_slot, dest_lane)).

        Takes into account the BYPASS mode.
        """
        return self.get_link_map().keys()

