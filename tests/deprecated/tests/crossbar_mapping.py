import time
import numpy as np
import matplotlib.pyplot as plt

BP_RX_TO_TX_MAP = {
    (1,1):(12,1), (1,2):(10,11), (1,3): (9,15),  (1,4): (6,8),   (1,5): (5,2),  (1,6): (3,8),  (1,7): (8,6), (1,8):(2,10),
    (1,9):(15,3),  (1,10):(16,11), (1,11):(11,13), (1,12):(14,5), (1,13):(13,1), (1,14):(4,7), (1,15):(7,13),

    (2,1):(11,11), (2,2) :(16,7),  (2,3) :(12,13),  (2,4) :(6,14),  (2,5) :(5,4),  (2,6) :(15,5), (2,7) :(8,2), (2,8):(3,10),
    (2,9):(14,7),  (2,10):(13,2),  (2,11):(10,13),  (2,12):(9,13),  (2,13):(1,6),  (2,14):(4,8),  (2,15):(7,15),

    (3,1):(9,12),
    (3,2):(12,14),
    (3,3):(14,11),
    (3,4):(11,12),
    (3,5):(5,8),
    (3,6):(2,6),
    (3,7):(8,4),
    (3,8):(4,10),
    (3,9):(6,4),
    (3,10):(1,4),
    (3,11):(15,11),
    (3,12):(16,12),
    (3,13):(10,15),
    (3,14):(13,4),
    (3,15):(7,14),
    (4,1):(15,2),
    (4,2):(16,2),
    (4,3):(11,1),
    (4,4):(6,2),
    (4,5):(12,2),
    (4,6):(3,6),
    (4,7):(8,12),
    (4,8):(5,10),
    (4,9):(2,2),
    (4,10):(1,2),
    (4,11):(13,11),
    (4,12):(9,14),
    (4,13):(10,14),
    (4,14):(14,2),
    (4,15):(7,4),
    (5,1):(9,2),
    (5,2):(16,15),
    (5,3):(13,15),
    (5,4):(15,15),
    (5,5):(14,15),
    (5,6):(3,4),
    (5,7):(8,8),
    (5,8):(6,10),
    (5,9):(2,11),
    (5,10):(1,1),
    (5,11):(10,12),
    (5,12):(11,14),
    (5,13):(4,6),
    (5,14):(12,15),
    (5,15):(7,8),
    (6,1):(16,5),
    (6,2):(15,13),
    (6,3):(9,4),
    (6,4):(14,14),
    (6,5):(13,5),
    (6,6):(3,2),
    (6,7):(8,9),
    (6,8):(7,10),
    (6,9):(2,12),
    (6,10):(1,11),
    (6,11):(10,2),
    (6,12):(12,4),
    (6,13):(5,6),
    (6,14):(4,4),
    (6,15):(11,4),
    (7,1):(16,9),
    (7,2):(15,9),
    (7,3):(13,7),
    (7,4):(14,9),
    (7,5):(5,11),
    (7,6):(3,1),
    (7,7):(6,6),
    (7,8):(8,10),
    (7,9):(2,13),
    (7,10):(1,12),
    (7,11):(9,8),
    (7,12):(12,10),
    (7,13):(10,4),
    (7,14):(4,2),
    (7,15):(11,15),
    (8,1):(6,11),
    (8,2):(16,8),
    (8,3):(14,8),
    (8,4):(15,8),
    (8,5):(5,12),
    (8,6):(3,11),
    (8,7):(7,6),
    (8,8):(9,10),
    (8,9):(2,14),
    (8,10):(1,13),
    (8,11):(10,8),
    (8,12):(13,8),
    (8,13):(12,8),
    (8,14):(4,1),
    (8,15):(11,8),
    (9,1):(6,12),
    (9,2):(14,6),
    (9,3):(15,6),
    (9,4):(16,6),
    (9,5):(5,13),
    (9,6):(3,12),
    (9,7):(8,13),
    (9,8):(10,10),
    (9,9):(2,15),
    (9,10):(1,14),
    (9,11):(12,6),
    (9,12):(13,6),
    (9,13):(11,6),
    (9,14):(4,11),
    (9,15):(7,12),
    (10,1):(6,13),
    (10,2):(4,12),
    (10,3):(7,2),
    (10,4):(8,14),
    (10,5):(5,15),
    (10,6):(3,13),
    (10,7):(9,6),
    (10,8):(11,10),
    (10,9):(2,1),
    (10,10):(1,15),
    (10,11):(16,14),
    (10,12):(15,14),
    (10,13):(14,13),
    (10,14):(13,14),
    (10,15):(12,11),
    (11,1):(5,1),
    (11,2):(7,11),
    (11,3):(8,11),
    (11,4):(9,11),
    (11,5):(6,15),
    (11,6):(3,14),
    (11,7):(10,6),
    (11,8):(12,7),
    (11,9):(2,5),
    (11,10):(1,3),
    (11,11):(4,13),
    (11,12):(16,4),
    (11,13):(15,4),
    (11,14):(14,4),
    (11,15):(13,13),
    (12,1):(7,1),
    (12,2):(8,1),
    (12,3):(9,1),
    (12,4):(10,1),
    (12,5):(5,14),
    (12,6):(3,15),
    (12,7):(11,2),
    (12,8):(13,10),
    (12,9):(2,7),
    (12,10):(1,5),
    (12,11):(15,1),
    (12,12):(16,1),
    (12,13):(14,1),
    (12,14):(4,14),
    (12,15):(6,1),
    (13,1):(7,7),
    (13,2):(8,7),
    (13,3):(9,7),
    (13,4):(10,7),
    (13,5):(11,7),
    (13,6):(3,5),
    (13,7):(12,12),
    (13,8):(14,10),
    (13,9):(2,9),
    (13,10):(1,7),
    (13,11):(16,3),
    (13,12):(5,7),
    (13,13):(15,7),
    (13,14):(4,15),
    (13,15):(6,7),
    (14,1):(7,5),
    (14,2):(8,5),
    (14,3):(9,5),
    (14,4):(10,5),
    (14,5):(11,5),
    (14,6):(12,5),
    (14,7):(13,12),
    (14,8):(15,10),
    (14,9):(2,8),
    (14,10):(1,9),
    (14,11):(5,5),
    (14,12):(6,5),
    (14,13):(4,5),
    (14,14):(3,7),
    (14,15):(16,13),
    (15,1):(8,15),
    (15,2):(9,9),
    (15,3):(10,9),
    (15,4):(11,9),
    (15,5):(12,9),
    (15,6):(13,9),
    (15,7):(14,12),
    (15,8):(16,10),
    (15,9):(2,4),
    (15,10):(1,10),
    (15,11):(6,9),
    (15,12):(7,9),
    (15,13):(5,9),
    (15,14):(4,9),
    (15,15):(3,9),
    (16,1):(8,3),
    (16,2):(9,3),
    (16,3):(10,3),
    (16,4):(11,3),
    (16,5):(12,3),
    (16,6):(13,3),
    (16,7):(15,12),
    (16,8):(14,3),
    (16,9):(7,3),
    (16,10):(1,8),
    (16,11):(5,3),
    (16,12):(6,3),
    (16,13):(4,3),
    (16,14):(3,3),
    (16,15):(2,3),
}

tx_slot_tx_lane_to_rx_slot_rx_lane = {(ts,tl):(rs,rl) for (rs,rl),(ts,tl) in BP_RX_TO_TX_MAP.items()}

bypass_lanes = {(ts,0):(ts,0) for ts in range(1,17)}

tx_slot_tx_lane_to_rx_slot_rx_lane.update(bypass_lanes)

def chan_configure(self, number_of_bins_per_crossbar_output= None, populated_slots = None, missing_slots = None):
    """
    Configure the channel selection.
    This should be done once the data width has been selected.
    """
    # Will need lots of checks to be sure configuration goes properly. 
    # right now just assume everything is happy!
    # Restricted to odd/even bin selection
    # First go, step by 16 bins for each lane.  
    # slot 1 gets freq. 0,16,32..
    # slot 2 gets freq 1,17,33...
    # Will need to move/bypass this if want something more intelligent
    # Higher up 
    self.fpga.slot_number = tx_slot

    number_of_bins_per_frame = self.fpga.FRAME_LENGTH / 2 # Frame length is number of adc samples.
    
    

    if number_of_bins_per_crossbar_output is None:
        bin_step = 16 #backplane crossbar.
        number_of_bins_per_crossbar_output = int (number_of_bins_per_frame / bin_step)
    else:
        bin_step = int(number_of_bins_per_frame/number_of_bins_per_crossbar_output)
    #I'm assuming here lane 0 is what is mapped back to yourself. 
    # the 'bypass_lanes'
    for (i, xbar) in enumerate(self.CROSSBAR):
        receive_slot = tx_slot_tx_lane_to_rx_slot_rx_lane[(tx_slot,i)][0]
        if missing_slots:
            if receive_slots in missing_slots:
                continue
            else:
                if populated_slots:
                    pass
                else:
                    all_slots = range(1,17)
                    populated_slots = [ slot for slot in all_slots if slot not in missing_slots]
                bin_list = np.arange(number_of_bins_per_crossbar_output) * bin_step + populated_slots.index(receive_slot)
        else:
            bin_list = np.arange(number_of_bins_per_crossbar_output) * bin_step + receive_slot-1        
        xbar.CH_DIST.select_bins(bin_list) 
        # if missing_slots:
        #     n_missing = len(missing_slots)
        #     if receive_slot in missing_slots:
        #         pass
        #     else:


def crate_configure(crate_array):
    number_of_boards = len(self.serial_number)
    all_slots = range(1,17)
    populated_slots = crate_array.slot_number
    missing_slots = [ slot for slot in all_slots if slot not in populated_slots]
    number_of_bins_per_frame = (self.fpga.FRAME_LENGTH / 2)[0]
    if number_of_boards > 8:
        print 'Configuring to still get all frequencies from all boards'
        number_of_bins_per_crossbar_output = int(number_of_bins_per_frame/number_of_boards)
        #frequency priority... create map of what board receives what set of frequencies.  right now just send lowest step to lowest integer.
        # frequency shift based on slot.  
        for board in self:
            chan_configure(board, number_of_bins_per_crossbar_output=number_of_bins_per_crossbar_output, populated_slots = populated_slots, missing_slots=missing_slots)


