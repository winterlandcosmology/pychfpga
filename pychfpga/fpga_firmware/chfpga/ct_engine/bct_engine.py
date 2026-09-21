"""
UCT.py module
    Implements the Ultrascale Corner-Turn Engine
"""
import logging
import time

import numpy as np

from ..mmi import MMI, MMIRouter, BitField, CONTROL, STATUS

from . import chan_crossbar
from . import shuffle_crossbar
from . import shuffle
from .bct_array_methods import BCTArrayMethods

class WiredCrossbar(MMI):
    """ Trivial hardwired crossbar, typically used with the internal correlator engine. Does not have any MMI register.

    Outputs 2 lanes (2 bins)  of 16 channels each. Both lanes/bins channels are in linear order.

    """

    def __init__(self, *, router, router_port, config):
        super().__init__(router=router, router_port=router_port)
        self.config = config

    def init(self):
        pass

    def set_data_width(self, width):
        if width != 4:
            raise RuntimeError(f'CT engine only supports a data width of 4+4 bits. {width}+{width} bits is not supported')
    def get_data_width(self):
        return 4

    def set_frames_per_packet(self, frames):
        if not 0 < frames < 5:
            self.logger.warning(f'{self!r}: CT engine only supports packaging from 1 to 4 frame per packet. {frames} frames are not supported')

    def get_frames_per_packet(self):
        return 1


class BCTEngine(MMIRouter):
    """ BRAM-Based corner-turn (CT) engine.

    Implements a 1st crossbar, intra-crate (16x16) data shuffle, 2nd crossbar, inter-crate (2x2) data shiffle, and a 3rd crossbar.

    """

    ROUTER_PORT_NUMBER_WIDTH = 2
    ROUTER_PORT_MAP = {
        'CB1': 0,
        'CB2': 1,
        'CB3': 2,
        'SHUFFLE': 3
        }

    def __init__(self, *, router, router_port, config, verbose=1):
        self.logger = logging.getLogger(__name__)
        self.verbose = verbose

        super().__init__(router=router, router_port=router_port)
        self.config = config
        self.fpga.register_array_methods(BCTArrayMethods, key='CT')

        self.BP_SHUFFLE = None
        self.CROSSBAR = None
        self.CROSSBAR2 = None
        self.CROSSBAR3 = None

        CT_LEVEL = self.CT_LEVEL = self.fpga.CT_LEVEL
        self.config.CT_LEVEL = CT_LEVEL

        self.logger.debug(f'{self!r}: === Instantiating Corner-turn engine Type BCT Level {CT_LEVEL}')

        if CT_LEVEL == 1:

            self.logger.debug(f'{self!r}: ===     Instantiating 1st CROSSBAR (Wired)')
            self.CROSSBAR = WiredCrossbar(router=self, router_port='CB1', config=self.config.add("crossbar"))

        elif CT_LEVEL == 3:
            self.logger.debug(f'{self!r}: ===     Instantiating 1st CROSSBAR (Programmable)')
            self.CROSSBAR = chan_crossbar.ChanCrossbar(router=self, router_port='CB1', config=self.config.add("crossbar"))

            self.logger.debug(f'{self!r}: ===     Instantiating Backplane shuffle subsystem')
            self.BP_SHUFFLE = shuffle.Shuffle(router=self, router_port='SHUFFLE', config=self.config.add("bp_shuffle"))

            self.logger.debug(f'{self!r}: ===     Instantiating 2nd CROSSBAR')
            self.CROSSBAR2 = shuffle_crossbar.ShuffleCrossbar(
                router=self,
                router_port='CB2',
                crossbar_level=2,
                config=self.config.add("crossbar2"),
                number_of_bin_sel=2)  # CROSSBAR block

            self.logger.debug(f'{self!r}: ===     Instantiating 3rd CROSSBAR')
            self.CROSSBAR3 = shuffle_crossbar.ShuffleCrossbar(
                router = self,
                router_port='CB3',
                crossbar_level=3,
                config=self.config.add("crossbar3"),
                number_of_bin_sel=8)  # CROSSBAR block
        else:
            raise RuntimeError("BCT-type Corner turn engine only supports CT_LEVEL of 1 or 3. Received CT_LEVEL={CT_LEVEL}")

    def init(self):
        self.logger.debug(f'{self!r}: === Initializing 1st Crossbar')

        # if self.NUMBER_OF_CROSSBAR1_OUTPUTS > 0:
        if self.CROSSBAR:
            self.logger.debug(f'{self!r}:  - BCT 1st CROSSBAR')
            self.CROSSBAR.init()
            # self.CROSSBAR.status()
        else:
            self.logger.warning(f"{self!r}: There is no 1st CROSSBAR module in this firmware build. "
                                 "The Channelizer outputs will be discarded.")

        if self.BP_SHUFFLE:
            self.logger.debug(f'{self!r}: === Initializing Backplane Shuffle')
            self.BP_SHUFFLE.init()

        self.logger.debug(f'{self!r}: === Initializing BCT 2nd Crossbar')
        if self.CROSSBAR2:
            # await asyncio.sleep(0)
            self.CROSSBAR2.init()
        # else:
        #     self.logger.warning(f"{self!r}: There is no 2nd CROSSBAR module in this firmware build")

        self.logger.debug(f'{self!r}: === Initializing BCT 3rd Crossbar')
        if self.CROSSBAR3:
            # await asyncio.sleep(0)
            self.CROSSBAR3.init()
        # else:
        #     self.logger.warning(f"{self!r}: There is no BCT 3rd CROSSBAR module in this firmware build")

    def update_config(self):
        """Refresh CT config subtree; returns ``self.config``."""
        return self.config

    def set_data_width(self, width):
        if self.CROSSBAR:
            self.CROSSBAR.set_data_width(width)

    def get_data_width(self):
        return self.CROSSBAR.get_data_width()

    def set_frames_per_packet(self, frames):
        if self.CROSSBAR:
            self.CROSSBAR.set_frames_per_packet(frames)
            self.logger.debug(f'{self!r}: The 1st crossbar will pack {frames} frames per packet')

    # def capture_bytes(self, N=64, fmt='hex'):
    #     """ Captures N bytes
    #     """
    #     b = bytearray(N)
    #     for i in range(N):
    #         self.CAPTURE_BYTE_NUMBER = i
    #         b[i] = self.CAPTURE_BYTE

    #     if fmt == 'hex':
    #         print('\n'.join(f'{b[i: i+16].hex()} {b[i+16: i+32].hex()}' for i in range(0,len(b),32)))
    #         return None

    #     return b

    # def set_enable(self, enable):
    #     """
    #     Enable link.
    #     """
    #     self.logger.warn('{self!r}: set_enable()  is not implemented on UltraCT. Command is ignored. ')

    # # def reset(self):
    # #     """ Resets the UDP/MAC stack, the SGMII interface and the GTX """
    # #     self.RESET = 1
    # #     self.RESET = 0

    # def get_lane_numbers(self):
    #     """ Returns a list of logical lane numbers for the GPU links.

    #     There is no distinction between the two QSFP connectors.

    #     Returns:

    #         List of integers.
    #     """
    #     return list(range(1))

    # def get_lane_ids(self):
    #     """ Return a list of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...].

    #     Returns:
    #         List of all lane IDs in the form of [(crate_number, slot_number, lane_number), ...]
    #     """

    #     return [self.fpga.get_id(lane) for lane in self.get_lane_numbers()]

    # # def status(self):
    # #     """ Displays the status of the GPU GTX hardware"""
    # #     # for gtx in self.GTX_COMMON:
    # #     #     gtx.status()



    @classmethod
    def compute_bin_routing_map(cls, mode, boards, bad_links=None, bin_priority=None, remap_level=0, verbose=0):
        """
        Compute a bin selection map for every board of the array in order to
        route the bins toward the  desired output links

        Parameters:

            mode (str): opertional mode. 'shuffle256' , 'shuffle512', 'shuffle16' and 'chord16'
                implement frequency-remaping. Other modes return de default
                map.

            boards: Sequence of motherboards whose IDs are included in the
                returned map.

            bad_links: List of corner_turn outputs [(crate, slot, link) , ...]
                that can't process data. low-priotity frequencies will be
                assigned to those bins.

            bin_priority: list of all frequency bins (from 0 to 1023) in order
                of priority, with the useful bins at the beginning of the
                list.

            remap_level (int): Sets the aggressivness of the remapping by
                selecting how many crossbar levels are involved. 0:no
                remapping, 1: 3rd crossbar only, 2: crossbars 2 and 3, 3: all
                crossbars.

        Returns:

            A dictionary that describes the corner-turn bin selection map for
            every board, in the format

                { (crate, slot):{'cb1':cb1_bins, 'cb2':cb2_bins, 'cb3':cb3_bins}, ... }

            where

                ``crate``: crate id as returned by ``mb.get_id()``. It is
                typically the crate number, but could be a string with the
                crate model/serial.

                ``slot``: slot id as returned by ``mb.get_id()``.

                ``cb1_bins``: list of 16 lists describing the bins indices
                    that are selected by each bin selector of the 1st crossbar

                ``cb2_bins``: list of 2 lists describing the bins indices that
                    are selected by each bin selector of the 2nd crossbar

                ``cb3_bins``: list of 8 lists describing the bins indices that
                    are selected by each bin selector of the 3rd crossbar
        """

        # Process bad link
        bad_links = bad_links or []
        bad_links = [tuple(id) for id in bad_links]
        if len(set(bad_links)) != len(bad_links):
            raise RuntimeError('bad links tuples are not unique!')
        bad_bad_links = [(crate, slot, lane) for (crate, slot, lane) in bad_links
                         if (not (0 <= crate <= 1)
                             or not (0 <= slot <= 15)
                             or not (0 <= lane <= 7))]
        if bad_bad_links:
            raise RuntimeError(f'The following bad links tuples are invalid: {bad_bad_links}')

        # Process bin priority
        bin_priority = bin_priority or list(range(1024))
        if set(bin_priority) != set(range(1024)):
            raise RuntimeError('Bin priority must contain every bin from 0 to 1023 exactly once')
        bin_priority = np.argsort(bin_priority)  # priority for each bin from 0 to 1023

        if mode == 'shuffle512':
            # start with the default bin map
            bin_map = {(crate, slot): dict(
                cb1=np.arange(1024).reshape((16, 64), order='F'),
                cb2=np.arange(64).reshape((2, 32), order='F'),
                cb3=np.arange(32).reshape((8, 4), order='F'))
                for crate in range(2) for slot in range(16)}
            if remap_level >= 3:
                cls.compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 2:
                cls.compute_cb2_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 1:
                cls.compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
                # cls.shuffle512_cb3_freq_remap(bin_map, bad_links, bin_priority)
            # Apply crates 0 & 1 map to all pair of crates
            bin_map = {(crate, slot): bin_map[((crate or 0) & 1, slot)]
                       for (crate, slot) in [mb.get_id() for mb in boards]}

            # cb3_map = cls.shuffle512_cb3_remap(mode=mode,
            #                                     bad_links=bad_links,
            #                                     freq_bins=bin_priority,
            #                                     output_cb3_bins=True)
            # # cb3_map describes only odd and even crate numbers. We expand the list to cover all crates explicitely.
            # for mb in boards:
            #     cb3_bins = [cb3_map.get((crate & 1, slot, lane), None)
            #         for crate, slot, lane in mb.GPU.get_lane_ids()]
            #     bin_map[mb.get_id()] = {
            #         'cb1':None,
            #         'cb2':None,
            #         'cb3':cb3_bins}
        elif mode == 'shuffle256':
            # start with the default bin map for a single crate
            bin_map = {(crate, slot): dict(
                cb1=np.arange(1024).reshape((16, 64), order='F'),
                cb2=np.arange(64).repeat(2).reshape(2, 64, order='F'),
                cb3=np.arange(64).reshape((8, 8), order='F'))
                for crate in range(1) for slot in range(16)}
            if remap_level >= 3:
                cls.compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 1:
                cls.compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            # Apply crates 0 map to all other crates
            bin_map = {(crate, slot): bin_map[(0, slot)]
                       for (crate, slot) in [mb.get_id() for mb in boards]}
        # elif mode == 'shuffle16':
        #     # use only the default bin map: we have no flexibility. All we can do is to remap the outputs.
        #     bin_map = dict(
        #             cb1=np.arange(1024).reshape((16, 64), order='F'),
        #             cb2=np.arange(64).repeat(2).reshape(2, 64, order='F'), # bypassed
        #             cb3=np.arange(64).reshape((8, 8), order='F')) # bypassed
        #     # Assign the same bin map to all boards in the array
        #     bin_map = {(crate, slot):bin_map
        #                 for (crate, slot) in [mb.get_id() for mb in boards]}
        else:  # other modes. Use defaults.
            bin_map = {mb.get_id(): {'cb1': None, 'cb2': None, 'cb3': None}
                       for mb in boards}
        return bin_map

    @staticmethod
    def compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=1):
        """ Compute a first crossbar bin map that assings bins to
        each slot in order to maximize the allocation of low-priority (rfi) bins to bad gpu links.

        Parameters:

          bin_map (dict): Dict that contains the bin maps for all crossbars. In the format

            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}

            `bin_map` is modified in place with the new optimized map.

          bad_links (list of tuple): List of (crate,slot,lane) GPU links that are inoperative

          bin_priority (ndarray): 1024-element vector indicating the priority of each bin.
              Element 0 is the priority for bin 0. A lower value has a higher priority.

        Returns:

            Nothing. bin_map['cb1'] is modified in place with the new optimized map.

        Description:

        In the 'shuffle512' and 'shuffle256' mode of the corner-turn engine,
        each of the 16 bin selector (BIN_SEL) of the first crossbar (crossbar 1) selects
        64 bins. These bins are then sent to specific slots. This is the only
        crossbar that can assign bins to slots; other subsequent crossbars
        operate within the same slot.

        The crossbar 1 bin selector has a constraint: bins that are selected
        by a bin selector must be separated by at least 8 bins. This limits
        the bin assignment possibilities. We cannot simply assign the N worst
        bins to the M worst GPU links, so we have to find the best compromise
        in a deterministic way.

        The algorithms is as follows:

          - We create 8 patterns of 128 bins separated by 8 bins. A pattern
            will be used by exactly two slots (each slot using 64 bins)
          - We process each slot one by one, starting with the slots that has
            the worst number of unprocessable bins
          - For each slot, we find the pattern that offers the worst N bins,
            where N is the number of unprocessable bins for that slot
          - We select assign the worst N bins from that pattern to the slot
          - we select the remaining 64-N best bins from that pattern to the slot
          - The resulting 64 selected bins are masked so they cannot be used anymore in that pattern
          - We repeat for each slot

        The crossbar 1 bin map does not attempt to route bad bins to specific
        crates or to specific GPU links. It just assigns the optimal number of
        bad bins that matches the total number of bad GPUs on that slot. It is
        the job of the following crossbars to route the bad bins to the
        correct crate and GPU. The downstream crossbars can do this routing
        perfectly since there are no restrictions on the bin assignments (no
        minimum bin spacing).

        This algorithm is somewhat restrictive and forgoes some of the
        flexibility we have in selecting bins.  Indeed, the bins do not have
        to be on a grid of 8: they can have *any* spacing greater or equal to 8.
        But all the algorithms we tried that took advantage of this flexibility would
        generally make excellent bin choices for the first slots, and the
        bins that remained for the last couple of slots wound no longer meets
        the spacing requirements; the algorithm would then fail.

        The algorithm we present here is restricted to 8 fixed grids, but it
        is deterministic and cannot fail. If we have a bad combination of bad
        links and bid priority, it will only provide less optimal solutions.
        Empirical data shows it is more than enough to salvage otherwise lost
        bins if the number of bad links is reasonable.

        """
        # Compute the number of unprocessable bins for each slot (both crates combined)
        Nbad = np.zeros(16, dtype=int)
        for (crate, slot, lane) in bad_links:
            Nbad[slot] += 4

        # Create a 8x128 array of bin numbers that represents 8 patterns of 128 bins that are spaced
        # by 8 bins. All patterns are exclusive, i.e. each bin is represented only once in the whole array
        bins = np.arange(1024).reshape((8, 128), order='F')  # 8 crossbar-compatible bin pattern of 128 bins

        # Create an equivalent matrix, now listing the priority level of each of the bins in the pattern array.
        # This is a masked array so we can mark which bins have been used.
        pri = np.ma.array(bin_priority[bins], mask=bin_priority[bins] * 0)  # priority level of bins in the pattern

        # bin_map = np.empty((16,64), dtype=int) # final bin assignments
        s = set()
        # Process each slot, starting with the one that has the worst number of unprocessable bins
        for j, slot in enumerate(np.argsort(Nbad)[::-1]):
            # We start by assigning the worst bins. We'll fill up the best bins after.
            nbad = Nbad[slot]
            if verbose:
                print(f'**** Interation #{int(j)}, Slot {int(slot)} (has {int(nbad)} unprocessable bins)')

            # Compute the number of remaining bins for each pattern (count() ignores masked entries)
            # It should normally be 128, 64 or 0
            Nbins = pri.count(axis=-1)
            if verbose:
                print('Number of remaining bins=', Nbins)

            # Find the index of the available bins for each pattern, in order of bin priority.
            # ix[i] is the index of elements of pri[i], with the highest priority (lower value) appearing first.
            # Masked bins end up at the end of the sorted list, so
            # only the first Nbins[i] bins are valid for pattern i.
            ix = pri.argsort(axis=-1)

            # Find the  best priority level we we end up with if we have to
            # take the `nbad` worst bins for each pattern. That corresponds to
            # taking the `nbad`-th element before last. We will start eating
            # into good (or higher priority) bins if we are running out of bad
            # (lower priority) bins in that pattern. Since we are selecting
            # bins that well end up on bad GPUs, we want these `nbad` bins to
            # contain as little good bins as possible.
            worst_pri = [(pri[s, x[np.clip(Nbins[s] - nbad, a_min=0, a_max=Nbins[s] - 1)]] if Nbins[s] else 0)
                         for s, x in enumerate(ix)]
            if verbose:
                print('Worst bin priority for all patterns are', worst_pri)

            # Find the pattern number that contained the worst nbad bins
            wo = np.argmax(worst_pri)  # 2 lost bins in typical data
            # We tried alternate pattern selection methods below, not of which were as good:
            # wo = np.argmax(Nbins) # Select the pattern with the most remaining bins: 14 lost bins with typical data
            # wo = slot//2 # Select the pattern based on slot number: 28 lost bins with typical data

            if verbose:
                print(f'slot {int(slot)} GPUs cannot process {int(Nbad[slot])} bins, using pattern #{int(wo)}')

            # Algorithmic check: check that there are bins available in this pattern.
            # This should always be true with this deterministic algorithm.
            if not Nbins[wo]:
                print('***** There are not enough frequencies left in the selected offset')
                raise RuntimeError('***** There are no frequencies left in the selected pattern')

            # Create a list indices containing the worst nbad bins and best 64-nbad bins for the selected pattern
            # pri[wo, bix] is the priority level of the selected bins in the chosen pattern
            # bins[wo,bix] is the number of the selected bins in the chosen pattern
            bix = ix[wo][list(range(64 - nbad)) + list(range(Nbins[wo] - nbad, Nbins[wo]))]
            # get the corresponding bin numbers. Sort them.
            b = sorted(bins[wo, bix])

            # Another algorithmic check: should never happen with our algorithm
            if any(pri[wo, bix].mask):  # sanity check, cannot happen in theory
                raise RuntimeError('Assigned a bin that was already assigned in another slot!')

            # Mask used pattern entries so they won't be used in the next slot
            pri[wo, bix] = np.ma.masked

            # Store the map for each bin_selector. Here, we assume that  bin
            # selector 0 selects bins for slot 0 etc. The init_crossbar()
            # method will reorder those to take into account the backplane
            # connectivity. We store this map for board (0,0), but it will be
            # the same for all boards.
            bin_map[(0, 0)]['cb1'][slot] = b
            # print 'Assignling %i bins:' % len(b), b

        # Finished computing the bin map for one slot of one crate
        # Apply the bin selection to all slots of all crates in the array
        for bmap in bin_map.values():
            bmap['cb1'][:] = bin_map[(0, 0)]['cb1']

        # Check the integrity of the result. Should always be good.
        if set(bin_map[(0, 0)]['cb1'].flatten()) != set(range(1024)):
            raise RuntimeError('Invalid bin map')

    @staticmethod
    def compute_cb2_bin_map(bin_map, bad_links, bin_priority, verbose=1):
        """ Compute crossbar 2 frequency mapping that routes the optimal
        number of best and worst bins to crates knowing how many can be
        processed by the GPU nodes connected to that crate.

        Parameters:

          bin_map (dict): Dict that contains the bin maps for all crossbars. In the format

            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}

            `bin_map` is modified in place with the new optimized map.

          bad_links (list of tuple): List of (crate,slot,lane) GPU links that are inoperative

          bin_priority (ndarray): 1024-element vector indicating the priority of each bin.
              Element 0 is the priority for bin 0. A lower value has a higher priority.

        Returns:
          None. `bin_map` is modified in-place.

        Algorithm:

        This mapper decides on which crate (0 or 1) will be routed the bins
        selected for the current slot by the previous crossbar. We have full
        flexibility here: any bin can go on any crate.

        For each slot, we look at how many unprocessable (`nbad`) bins there
        are in each crate. We assign `nbad` worst available bins to that
        crate, and the rest is filled with the best available bins. Used bins
        are removed from the available bins. We repeat for the other crate.


        """
        Nslots, _ = bin_map[(0, 0)]['cb1'].shape
        Ncrates, Nbins_out = bin_map[(0, 0)]['cb2'].shape
        # Compute number of unprocessable bins
        Nbad = np.zeros((Ncrates, Nslots), dtype=int)
        for (crate, slot, link) in bad_links:
            Nbad[crate, slot] += 4
        for slot in range(Nslots):
            # CB1 map is the for all slots in both crates.  # cb1 has absolute
            # bin numbers. Assumes that cb2 remap is such as cb1[0] goes to
            # slot 0, etc.
            bins = bin_map[(0, 0)]['cb1'][slot]
            bix = list(bin_priority[bins].argsort())  # bin index order by bin_priority
            for crate, bs_map in enumerate(bin_map[(0, slot)]['cb2']):
                nbad = Nbad[(crate, slot)]
                ngood = len(bs_map) - nbad
                bs_map[:ngood] = bix[:ngood]
                del bix[:ngood]
                # print bs_map
                # print 'good=', ngood, 'bad=', nbad
                if nbad:
                    bs_map[-nbad:] = bix[-nbad:]
                    del bix[-nbad:]
                bs_map[:] = sorted(bs_map)
            bin_map[(1, slot)]['cb2'][:] = bin_map[(0, slot)]['cb2']
            if set(bin_map[(0, slot)]['cb2'].flatten()) != set(range(Ncrates * Nbins_out)):
                print('Selected', bin_map[(0, slot)]['cb2'])
                raise RuntimeError('Invalid crossbar 2 bin selection')

    @staticmethod
    def compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=1):
        """ Compute the Crossbar 3 frequency map that optimally routes the
        bins to each GPU link by sending the worst bins to the GPUs that are
        inoperative and the remaining bins to the others.


        Parameters:

          bin_map (dict): Dict that contains the bin maps for all crossbars. In the format

            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}

            `bin_map` is modified in place with the new optimized map.

          bad_links (list of tuple): List of (crate,slot,lane) GPU links that are inoperative

          bin_priority (ndarray): 1024-element vector indicating the priority of each bin.
              Element 0 is the priority for bin 0. A lower value has a higher priority.

        Returns:
          None. `bin_map` is modified in-place.

        The algorithm is identical to the crossbar2 bin mapper.

        """
        # Ncrates, _ = bin_map['cb2'].shape # (8,4) or (8,8)
        Nlanes, Nbins = bin_map[(0, 0)]['cb3'].shape  # (8,4) or (8,8)

        # Distribute bins in each CB3 output lanes
        for (crate, slot), bmap in bin_map.items():
            cb2_bix = bmap['cb2'][crate]  # Bin indices for crate
            bins = bmap['cb1'][slot][cb2_bix]  # absolute bins for slot
            bix = list(bin_priority[bins].argsort())  # bin index order by bin_priority
            for lane in range(Nlanes):
                if (crate, slot, lane) in bad_links:
                    # bs_map[:] = bix[-Nbins:]
                    bmap['cb3'][lane] = sorted(bix[-Nbins:])
                    del bix[-Nbins:]
                else:
                    # bs_map[:] = bix[:Nbins]
                    bmap['cb3'][lane] = sorted(bix[:Nbins])
                    del bix[:Nbins]
                # print '(%i,%i,%i)' % (crate, slot, lane), bmap['cb3'][lane], bin_map[(crate,slot)]['cb3'], bix
                # print '   -> (%i,%i)' % (0, 0), bin_map[(0,0)]['cb3']
            if set(bmap['cb3'].flatten()) != set(range(Nlanes * Nbins)):
                print('Selected', bmap['cb3'])
                raise RuntimeError('Invalid crossbar 3 bin selection')
        # for (crate, slot), bmap in bin_map.items():
        #     print '--_>(%i,%i)' % (crate, slot), bmap['cb3']

    @staticmethod
    def shuffle512_cb3_freq_remap(bin_map, bad_links, bin_priority):
        """
        Generates a frequency map by assigning flagged/less important frequency bins to
        links connected to bad/down GPU nodes. The remapping is
        restricted to changes at the third crossbar for 'shuffle512' operation.

        Parameters:
        -----------
        bin_map: dict
            Dict that contains the bin maps for all crossbars. In the format
            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}
            `bin_map` is modified in place with the new optimized map.
        bad_links: list of (crate parity, slot, link) tuples
            List of links connected to bad/down GPU nodes. Least important frequencies are
            assigned to these links. Crate parity is either 0 (even) or 1 (odd). Slot
            is an integer between 0 and 15, and link is an integer between 0 and 7
        bin_priority: list or np.array
            1024-long array with the frequency priority of each frequency bin.
            bin_priority[i] is the priority of the ith frequency bin.
            A lower value has a higher priority.
        """
        import itertools

        # Number of freq. bins, crates (per crate pair), slots (per crate), links (per board)
        # (SHOULD BE ABLE TO GET THIS FROM FPGA ARRAY OBJECT)
        Nfreq, Ncrate, Nslot, Nlink = 1024, 2, 16, 8
        Nfreq_cs = Nfreq // (Ncrate * Nslot)  # Freq. bins per (crate, slot)
        Nfreq_link = Nfreq_cs // Nlink   # Freq. bins per (crate, slot, link)

        # Order links by how easy it is to assign RFI bins to them (easier for middle links
        # according to FIFO constraints) links at the top of the list will have more RFI bins
        link_rfi_assign_order = np.array([3, 4, 2, 5, 1, 6, 0, 7])
        for (crate, slot), bmap in bin_map.items():
            # Standard CB3 bin assignment (each row is a link)
            cb3_bins = np.arange(Nfreq_cs).reshape((Nlink, Nfreq_link), order='F')
            # Standard absolute frequency assignment (each row is a link)
            freq_bins_cs = np.arange(crate * Nslot + slot, Nfreq, Nfreq_cs).reshape(
                (Nlink, Nfreq_link), order='F')
            # Importance of each CB3 bin
            cb3_bin_order = bin_priority[freq_bins_cs.ravel()].reshape((Nlink, Nfreq_link))
            # Start clustering RFI bins on nodes according to link_rfi_assign_order
            # Assumes that there are no overflows in cb3 FIFOs as long as the
            # separation between cb3 bins (in range(31)) for a given lane is at least 4
            for gpu in range(Nfreq_link):
                # Sort all links of given gpu by lowest priority (RFI first)
                link_priority = np.argsort(cb3_bin_order[:, gpu])[::-1]
                # Go over all permutations of links until we find one
                # that meets the requirement of at least 4 cb3 bin separation.
                # By the way itertools.permutations works, it will start
                # with the permutations that cluster RFI bins on the links
                # according to link_rfi_assign_order
                for lp in itertools.permutations(link_priority):
                    if np.all(abs(np.array(lp) - link_rfi_assign_order) <= 4):
                        # The permutation is allowed. done
                        break
                cb3_bins[link_rfi_assign_order, gpu] = cb3_bins[lp, gpu]
                freq_bins_cs[link_rfi_assign_order, gpu] = freq_bins_cs[lp, gpu]

            i_top, i_bottom = 0, Nlink
            for link in range(Nlink):
                stream_id = (crate, slot, link)
                if stream_id in bad_links:  # Bad link: assign less important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_rfi_assign_order[i_top]]
                    i_top += 1
                else:  # Good link: assign important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_rfi_assign_order[i_bottom - 1]]
                    i_bottom -= 1

    @staticmethod
    def shuffle512_cb3_lane_remap(bin_map, bad_links, bin_priority):
        """
        Generates a frequency map by assigning flagged/less important frequency bins to
        links connected to bad/down GPU nodes. The remapping is
        restricted to changes at the third crossbar for 'shuffle512' operation.
        Also, this remapping is limited to swap frequency lists between links
        (it does not alter the content of the frequency lists).

        Parameters:
        -----------
        bin_map: dict
            Dict that contains the bin maps for all crossbars. In the format
            {(crate, slot):'cb1':cb1_map, 'cb2':cb2_map, 'cb3':cb3_map},...}
            `bin_map` is modified in place with the new optimized map.
        bad_links: list of (crate parity, slot, link) tuples
            List of links connected to bad/down GPU nodes. Least important frequencies are
            assigned to these links. Crate parity is either 0 (even) or 1 (odd). Slot
            is an integer between 0 and 15, and link is an integer between 0 and 7
        bin_priority: list or np.array
            1024-long array with the frequency priority of each frequency bin.
            bin_priority[i] is the priority of the ith frequency bin.
            A lower value has a higher priority.
        """

        # Number of freq. bins, crates (per crate pair), slots (per crate), links (per board)
        # (SHOULD BE ABLE TO GET THIS FROM FPGA ARRAY OBJECT)
        Nfreq, Ncrate, Nslot, Nlink = 1024, 2, 16, 8
        Nfreq_cs = Nfreq // (Ncrate * Nslot)  # Freq. bins per (crate, slot)
        Nfreq_link = Nfreq_cs // Nlink   # Freq. bins per (crate, slot, link)

        # Standard CB3 bin assginment (each row is a link)
        cb3_bins = np.arange(Nfreq_cs).reshape((Nlink, Nfreq_link), order='F')
        for (crate, slot), bmap in bin_map.items():
            # Standard absolute frequency assignment (each row is a link)
            freq_bins_cs = np.arange(crate * Nslot + slot, Nfreq, Nfreq_cs).reshape(
                (Nlink, Nfreq_link), order='F')
            # Importance of each CB3 bin
            cb3_bin_order = bin_priority[freq_bins_cs.ravel()].reshape((Nlink, Nfreq_link))
            # Check which cb3_bins are RFI.
            # Assumes freq_bins with order > 779 are RFI (expected from static RFI mask)
            cb3_bin_rfi_mask = cb3_bin_order > 779
            # Number of rfi bins per link
            rfi_per_link = np.sum(cb3_bin_rfi_mask, axis=1)
            # Indices that sort links by increasing number of RFI bins (links at the
            # of the list have more RFI bins)
            link_priority = np.argsort(rfi_per_link)
            i_top, i_bottom = 0, Nlink
            for link in range(Nlink):
                stream_id = (crate, slot, link)
                if stream_id in bad_links:  # Bad link: assign less important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_priority[i_bottom - 1]]
                    i_bottom -= 1
                else:  # Good link: assign important freq. bins
                    bmap['cb3'][link] = cb3_bins[link_priority[i_top]]
                    i_top += 1

    @staticmethod
    def shuffle512_cb3_remap(mode, bin_map, bad_links, freq_bins, output_cb3_bins=False):
        """
        Generates a frequency map by assigning flagged/less important frequency bins to
        links connected to bad/down GPU nodes. The remapping is
        restricted to changes at the third crossbar for 'shuffle512' and 'shuffle256' operation.

        Parameters:

        mode (str): operational mode of the corner turn engine. Either
            `'shuffle512'` or '`shuffle256'`.

        bad_links (list of tuples): list of [crate parity, slot, link] lists
            List of links connected to bad/down GPU nodes. Least important frequencies are
            assigned to these links. Crate parity is either 0 (even) or 1 (odd). Slot
            is an integer between 0 and 15, and link is an integer between 0 and 7

        freq_bins (list of int): list or np.array
            1024-long array with frequency bins ordered by importance (important bins first).
            Frequency bins are assigned to good/up links/nodes when available based on their
            importance.

        output_cb3_bins: bool (optional)

            If False, the frequency assignment is given as a relative bin
            index that address the 32 bins at the input of the 3rd crossbar
            (in the range 0-31).

            If True, the frequency assignment is given as frequency bins (in the range
            0-1023)


        Returns

        freq_map: dict
            Describes the frequency bin assignment for each link. Its items have the form

            {..., (crate parity, slot, link): [freq. bin 0, ..., freq. bin 3], ...} if
            output_cb3_bins=False, or

            {..., (crate parity, slot, link): [cb3 bin 0, ..., cb3 bin 3], ...} if
            output_cb3_bins=True.
        """

        freq_bins = np.array(freq_bins)  # Make sure freq_bins is an np.array
        freq_bins_priority = np.argsort(freq_bins)  # indices that sort freq_bins in ascending order
        bad_links = [tuple(link) for link in bad_links]

        Nfreq = 1024  # Number of frequency bins
        Nslot = 16  # boards per crates
        Nlink = 8  # GPU links per board
        if mode == 'shuffle512':
            Ncrate = 2
            Nbix = 32  # Bins per CB3 input
            Nbins = 4  # Bins per CB3 output
            bix_to_bin = lambda crate, slot, bix: (Nslot * Ncrate * bix) + (Nslot * crate) + slot
        elif mode == 'shuffle256':
            Ncrate = 1
            Nbix = 64
            Nbins = 8
            bix_to_bin = lambda crate, slot, bix: (Nslot * bix) + slot
        else:
            raise ValueError(f'Invalid shuffle mode {mode}')

        # Number of freq. bins, crates (per crate pair), slots (per crate), links (per board)
        # (SHOULD BE ABLE TO GET THIS FROM FPGA ARRAY OBJECT)
        # Nfreq_cs = Nfreq // (Ncrate * Nslot) # Freq. bins per (crate, slot) = 32 (4 per lane) (Now Nbix)
        # Nbins = Nfreq_cs // Nlink   # Freq. bins per (crate, slot, link) = 4 per lane (now Nbins)

        freq_remap = {}
        for slot in range(Nslot):
            for crate in range(Ncrate):
                # Freq bins that can be assigned to (crate parity, slot) under standard map
                # cb3 output, shuffle512: bin = 16*2*8*bix + 16*2*lane + 16*crate + slot, bix=0..3
                # cb3 output, shuffle256: bin = 16*8*bix + 16*lane  + slot, bix=0...7, bix=0..7

                # available_bins = np.arange(crate * Nslot + slot, Nfreq, Nfreq_cs)
                available_bix = np.arange(Nbix)
                # Compute absolute bin number available at the input of CB3 on this (crate, slot)
                available_bins = bix_to_bin(crate, slot, available_bix)
                # Compute indices of available bins/bix, in priority order (first is most important)
                available_priority = np.argsort(freq_bins_priority[available_bins])
                # Compute bin indices, sorted by importance. Convert to a list so we can easily delete items.
                available_bix = list(available_bix[available_priority])
                # Indices of allowed freq_bins, sorted by importance
                # available_bins_indices = np.sort(freq_bins_priority[available_bins])
                # available_bins = list(available_bins[available_priority]) # absolute bins, sorted by importance

                # i_top = 0
                # i_bottom = Nbix
                # Assign bix, lane per lane
                for link in range(Nlink):
                    link_id = (crate, slot, link)
                    if link_id in bad_links:  # Bad link: assign less important freq. bins
                        bix = available_bix[-Nbins:]  # take the bottom (lower priority) bix
                        del available_bix[-Nbins:]  # remove from the list
                        # freq_remap[link_id] = list(
                        #     available_bix[i_bottom - Nbins:i_bottom] if output_cb3_bins else
                        #     freq_bins[available_bins_indices[i_bottom - Nbins:i_bottom]])
                        # i_bottom -= Nbins
                    else:  # Good link: assign important freq. bins
                        bix = available_bix[:Nbins]  # take the top bix (higher priority)
                        del available_bix[:Nbins]  # remove from the list
                        # freq_remap[link_id] = list(available_bix[i_top:i_top + Nbins] if
                        #                         output_cb3_bins else
                        #                         freq_bins[available_bins_indices[i_top:i_top + Nbins]])
                        # i_top += Nbins
                    # Order selected bins in increasing order. Now a numpy
                    # array again.  The bins are always transmitted that way.
                    bix = np.sort(bix)
                    freq_remap[link_id] = list(bix if output_cb3_bins else bix_to_bin(crate, slot, bix))
                if len(available_bix):
                    raise RuntimeError('Not all bins were processed. This should not happen')
        return freq_remap

    @classmethod
    async def init_corner_turn_array(
            cls,
            fpga_array,
            mode,
            dsmap=list(range(16)),
            frames_per_packet=1,
            send_flags=True,
            chan8_channel_map=list(range(16)),
            tx_power=None,
            bin_map=None,
            bad_links=None,
            bin_priority=None,
            remap_level=0,
            sync=True):
        """ Setup the crossbars and data shuffling in every board of the array.

        Parameters:

            fpga_array (chfpga.chfpga_array.ChFpgaArray): The array of boards to configure.
            
            mode (str): One of the crossbar engine operational mode
                ('shuffle16', 'shuffle256' etc.). Is passed to
                mb.init_crossbar().

            dsmap (list of int): Shuffle remap that is passed to
                mb.init_crossbar(). Defaults to `range(16)`.

            frames_per_packet (int): Number of frames per packet. Defaults to
                1. Is passed to mb.init_crossbar().

            chan8_channel_map (list): Map that is passed to
                mb.init_crossbar(). Defaults to `range(16)`.

            tx_power (dict): Describes the initial (training) and final TX
                 power to be used by the backplane PCB and QSFP links. applied
                 to the backplane PCB GTX links.

            bin_map (dict): Bin map to be passed to mb.init_crossbar(). Defaults to None.   

            bad_links (list): List of bad links to be passed to mb.init_crossbar(). Defaults to None.

            bin_priority (list): List of bin priority to be passed to mb.init_crossbar(). Defaults to None.

            remap_level (int): Remap level to be passed to mb.init_crossbar(). Defaults to 0.

            sync (bool): Whether to sync the boards after initialization. Defaults to True.


        The GTX receivers that have no corresponding transmitter is put in
        reset so it won't generate random packets into the following crossbar.
        """

        (CT_LEVEL,) = {(mb.CT.CT_LEVEL if mb.CT else None) for mb in fpga_array.ib}  # get a set of all different CT engine types
        does_shuffle = CT_LEVEL > 1 and mode in ('shuffle256', 'shuffle512', 'shuffle128') # if true, we need to initialize the shuffle engine

        if tx_power is None:
            tx_power = {'corner_turn': [
                dict(lane_group='pcb', default=(10, 15)),
                dict(lane_group='qsfp', default=(10, 15))]}


        fpga_array.logger.info(f'{fpga_array!r}: Configuring array-wide corner-turning')

        fpga_array.corner_turn_stream_ids = {} if does_shuffle else None # CT_LEVEL > 1: shuffle128, 256, 512
        fpga_array.corner_turn_frequency_bins = None

        bin_map = cls.compute_bin_routing_map(
            mode=mode,
            boards=fpga_array.ib,
            bad_links=bad_links,
            bin_priority=bin_priority,
            remap_level=remap_level)

        #####################
        # Set-up transmitters
        #####################
        if does_shuffle: # shuffle128, 256, 512
            if not all(fpga_array.ib.CT.CROSSBAR2) or not all(fpga_array.ib.CT.CROSSBAR3):
                raise RuntimeError(f' Mode {mode} requires all boards to have their CROSSBAR2 and CROSSBAR 3 implemented')

            fpga_array.logger.info(f'{fpga_array!r}: Setting GTX transmit power for all boards')
            fpga_array.logger.debug(f'{fpga_array!r}: Using tx_power={tx_power!r}')
            tx_list = []
            fpga_array.ib.CT.BP_SHUFFLE.set_tx_power(13)
            fpga_array.ib.CT.CROSSBAR3.SOF_WINDOW_STOP = 110
            fpga_array.ib.CT.CROSSBAR3.TIMEOUT_PERIOD = 0
            fpga_array.ib.CT.BP_SHUFFLE.reset_rx_equalizers()


        for i, mb in enumerate(fpga_array.ib):
            fpga_array.logger.debug(f'{fpga_array!r}: **** Initializing transmitters for IceBoard {mb} (SN{mb.serial}) ****')
            mb.set_corr_reset(0)  # Put the corner_turn engine out of reset

            if does_shuffle: # shuffle128, 256, 512
                tx_list.append((mb.slot, 0))  # Register Bypass lane (lane 0) as a transmitter in this slot
                for j, gtx in enumerate(mb.CT.BP_SHUFFLE.gtx):
                    gtx.TXINHIBIT = 0
                    tx_list.append((mb.slot, j + 1))

            # Initialize the crossbars to select and send data in a specific format
            # mb.CT.init_crossbars(dsmap, frames_per_packet=frames_per_packet, cb1_lanes=cb1_lanes, cb1_bins=cb1_bins,
            #                   cb1_bypass=cb1_bypass, cb2_lanes=cb2_lanes, cb2_bins=cb2_bins, cb2_bypass=cb2_bypass,
            #                   remap=remap, bp_bypass=bp_bypass)
            stream_ids = mb.CT.init_crossbars(
                mode,
                dsmap=dsmap,
                frames_per_packet=frames_per_packet,
                send_flags=send_flags,
                chan8_channel_map=chan8_channel_map,
                bin_map=bin_map[mb.get_id()])

            if does_shuffle: # shuffle128, 256, 512
                for lane, stream_id in enumerate(stream_ids):
                    fpga_array.corner_turn_stream_ids[mb.get_id(lane)] = stream_id

        # Check if stream IDs are unique
        if fpga_array.corner_turn_stream_ids and len(fpga_array.corner_turn_stream_ids) != len(set(fpga_array.corner_turn_stream_ids.values())):
            raise fpga_array.logger.warning(f'{fpga_array!r}: Stream IDs are not unique across the array')

        if does_shuffle: # If we have a level 2 or higher CT engine, set up the transmitter and receivers 

            #####################
            # Set-up receivers
            #####################
            for i, mb in enumerate(fpga_array.ib):
                # Disable all receivers for which there are no transmitters
                for j, gtx in enumerate(mb.CT.BP_SHUFFLE.gtx[0:mb.CT.BP_SHUFFLE.NUMBER_OF_PCB_LINKS]):
                    if mb.slot is None or not mb.crate:
                        continue
                    rx = (mb.slot, j + 1)
                    tx = mb.crate.get_matching_tx(rx)

                    # disable receivers that have no corresponding transmitters
                    if tx in tx_list:
                        gtx.USER_GTRXRESET = 0
                    else:
                        gtx.USER_GTRXRESET = 1
                        # gtx.USER_RESET = 1

            # reset DFE at low power, then increase power
            for index in (0, 1):
                for tx_group in tx_power['corner_turn']:
                    lane_group = tx_group['lane_group']
                    default = tx_group['default']
                    exceptions = tx_group.get('exceptions', [])
                    fpga_array.logger.debug(
                        f'{fpga_array!r}: TX power parameters are: {tx_group!r} '
                        f'(default={default!r}, exceptions={exceptions!r})')
                    fpga_array.set_tx_power(lane_group=lane_group, default_power=default, exceptions=exceptions, index=index)
                if index == 0:
                    time.sleep(0.3)
                    for mb in fpga_array.ib:
                        fpga_array.ib.CT.BP_SHUFFLE.reset_rx_equalizers()

            fpga_array.ib.CT.BP_SHUFFLE.reset_stats()

            # Print links
            for mb in fpga_array.ib:
                for i in range(mb.NUMBER_OF_CROSSBAR1_OUTPUTS):
                    if mb.slot is None or not mb.crate:
                        continue
                    rx = (mb.slot, i)
                    tx = mb.crate.get_matching_tx(rx)
                    if tx not in tx_list:
                        fpga_array.logger.debug(f'{fpga_array!r}: In {mb.crate!r}, {rx} has no corresponding transmitter')

        # Compute the corner turn frequency bins for modes that needs them  
        if mode in ('shuffle256', 'shuffle512'):  # ***JFC: shuffle128 excluded because get_frequency_map() crashes. Other modes don't necessarily have the crate info to make unique maps.
            # Get the exhaustive frequency map that is implemented by the current actual corner turn engine.
            # The map is in the format (crate, slot, lane): freq_bin_list. crate and slot might not be numeric if there is no crate or crate number.
            freq_map = fpga_array.get_frequency_map(format='l:bb')
            # Retain only one bin number  for each bin
            fpga_array.corner_turn_frequency_bins = {lane_id: sorted(set(data['data']) - set([None]))
                                               for lane_id, data in freq_map.items()}

            # Double check that the frequency map that we obtained matches our target bin map.
            errors = 0
            for (crate, slot, lane), actual_bins in list(fpga_array.corner_turn_frequency_bins.items()):
                bs = bin_map[(crate % 2, slot)]
                expected_bins = bs['cb1'][slot][bs['cb2'][crate % 2]][bs['cb3'][lane]]
                if not all(np.equal(expected_bins, actual_bins)):
                    print(f'Link {(crate, slot, lane)} do not match: '
                          f'Expected bins: {expected_bins!r}, got bins {actual_bins!r}')
                    errors += 1

            if errors:
                raise RuntimeError('Actual frequency mapping does not match the expected one')
 

        if does_shuffle: # shuffle128, 256, 512
            fpga_array.ib.CT.BP_SHUFFLE.reset_stats()
            fpga_array.ib.CT.CROSSBAR2.reset_stats()
            fpga_array.ib.CT.CROSSBAR3.reset_stats()

        if any(fpga_array.ib.GPU): # If we have any offload engine, reset them
            fpga_array.logger.debug(f'{fpga_array!r}: Resetting the GPU transmitters.')
            fpga_array.ib.GPU.CORE_RESET = 1
            time.sleep(.1)
            fpga_array.ib.GPU.CORE_RESET = 0
            time.sleep(.1)

        fpga_array.logger.info(f'{fpga_array!r}: Shuffling initialization completed.')

        if sync:
            await fpga_array.sync_async()

    def init_crossbars(
            self,
            mode=None,
            frames_per_packet=2,
            bin_map=None,

            cb1_lanes=16,
            cb1_bins=64,
            dsmap=list(range(16)),
            cb1_bypass=False,
            cb1_combine_data_flags=0,

            bp_shuffle_bypass=1,

            cb2_lanes=None,
            cb2_bins=1,
            cb2_bypass=False,

            crate_shuffle_bypass=1,

            remap=True,
            chan8_channel_map=list(range(8)),
            send_flags=True):
        """ Initializes the Corner Turn engine in the specified operation mode.

        This method configured the 1st, 2nd and 3rd crossbars (which each can include a remap, frame
        aligner and an array of bin selectors), as well as the PCB and QSFP data shuffle links.


        Parameters:

            mode (str): String describing a predefined corner-turn engine operation mode. If None,
                the crossbars and shuffle enginecan be set manually with the other provided
                parameters. The valid modes are:

                    - 'chan8': Corner-turn engine is mostly bypassed and raw
                      8-bit data from 8 channelizers is sent directly to the 8
                      CT-Engine outputs.

                    - 'chan4': Corner-turn engine is mostly bypassed and raw
                      8-bit data from 16 channelizers is sent directly to the
                      8 CT-Engine outputs.

                    - 'shuffle16 and 'chord16': A corner-turn operation is applied only
                      within the 16 channelizer outputs of this board.

                    - 'shuffle256': The corner-turn operation is applies
                      between 16 channelizers within a board and between the
                      16 boards within a crate using the backplane PCB links.

                    - 'shuffle512': The corner-turn operation is applies
                      between 16 channelizers within a board, between the 16
                      boards within a crate using the backplane PCB links, and
                      between 2 crates using the backplane QSFP links.

                    - 'corr16': The corner-turn engine is configured to feed
                      the internal firmware correlator 


            frames_per_packet (int): Number of frames to be packaged in a packet. Defaults to 2. Is
                limited by the amount of memory used by the FPGA to store data and flags.

            bin_map (dict): Describes the bin indices to be assigned to the
                outputs of each crossbar.

            cb1_lanes (int): Number of input lanes considered in the
                CROSSBAR1. Defaults to 16. Used only if `mode` = `None`.

            cb1_bins (int): Number of frequency bins to be included in the first
                crossbar. Defaults to 64. Used only if `mode` = `None`.


            dsmap (list) : Destination slot for each of the bin selectors of
               the first crossbar. Defaults to range(16). Used only in modes
               that use the backplane PCB links (``shuffle256``,
               ``shuffle512``).


            cb1_bypass (bool): If True, the first crossbar will be bypassed.
                Used only if `mode` = `None`.

            cb1_combine_data_flags (bool): if True, the data flags at the output of CROSSBAR1 will
                be packed in 32-bit words. Defaults to False, where each data word is associated
                with a data flag word filled with only 16 flag bits, which is needed to allow the
                next crossbar to further split the flags and data as part of the 2nd stage corner
                turn operation.

            cb2_lanes (list of tuple): list of (lower_lane, upper_lane), one for each of the 2
                CROSSBAR2 bin selector, that describe the range of input lanes that shall be read
                out by the bin selectors. Only partial number of lanes is used if the data is to be
                further recombined by the 3rd stage corner turn operation. If None, both bin
                selectros will combine data from all 16 input lanes.


            cb2_bins (int): Number of bins to be included in the bin selectors
                of the second crossbar.



            cb2_bypass (bool): If True the 2nd stage cirner-turn operation (CROSSBAR2) will be
                bypassed. Defaults to `False`


            bp_shuffle_bypass (bool): If `False`, the 16 data lanes form the CROSSBAR1 will be sent
                to other boards through the backplane PCB links, and the CROSSBAR2 will receiver 16
                lanes of data from the other boards (note than lane 0 is in fact always internal).
                If `True`, the 16 data lanes from the CROSSBAR1 will be forwrded directly to the the
                input of CROSSBAR2.


            crate_shuffle_bypass (bool): If False, half of the 8 outputs lanes of CROSSBAR2 will be
                sent to other boards in the other crate through the backplane QSFP links, and half
                will be sent to the CROSSBAR3. CROSSBAR3 will receive half its data from the other
                crate the corresponding board.If True, the 8 output lanes of CROSSBAR2 go directly
                to the 8 input lanes of CROSSBAR3.



            remap (bool). Defaults to True

            chan8_channel_map (list) : channel remapping to be used in `chan8` mode. Defaults to the
                identity map (range(8))

            send_flags (bool): If False, the Scaler and Frame flags will not be sent.

        Returns:

            list of the stream ids at each output lane of the corner turn engine

        Note:
            bin_map is a dict in the format

                {'cb1':cb1_bin_indices, 'cb2':cb2_bin_indices, 'cb3':cb3_bin_indices}

            where

            -   cb1_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector (output lane) of the first
                crossbar.

                There are 16 bin selectors in the first crossbar.
                `cb1_bin_indices` should therefore consist of 16 lists, even
                in modes where only the first 8 bin selectors are used. The
                list is in the order of the destination slot. The list will be
                reordered to account for the backplane connectivity in the
                modes where those links are used..

                If `None`, the default bin selection is used: bins are
                interleaved between each bin selector. `shuffle16` and `corr16` select 128
                bins per bin selector (only the first 8 bin selector will be
                used);and `shuffle256`/`shuffle512` select 64 bins.

                The Bin selectors require 4 clocks to process a selected bin.
                A bin pair (even,odd) is processed on each clock. This means
                that if a bin from a bin pair is selected, no bin can be
                selected from the following 3 bin pairs.

            -  cb2_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector of the second
                crossbar.

                There are 2 bin selectors in the second crossbar.

                If `None`, the default assignments are used. In `shuffle256`,
                all 64 incoming  bins are selected (from half the input lanes,
                to be reassembled by the following crossbar). In `shuffle512`,
                the bins are interleaved between the 2 bin_selectors, with the
                even bins sent to the even crate number. `cb2_bin_indices` is
                not used in other modes.

                The first list is always for the even crate and the second
                list is for the odd crate. In `shuffle512`, The list is reordered automatically
                to account for crate connectivity (i.e the lists for an odd
                crate are swapped).

            -   cb3_bin_indices (list of list): List of bin indices that will be
                selected for each bin selector of the third (and final)
                crossbar.

                There are 8 bin selectors in the third crossbar.

                If `None`, the default assignments are used.  In `shuffle512`,
                the bins are interleaved between the 8 bin_selectors.
                `cb3_bin_indices` is not used in other modes.

        Returns:

            list of stream IDs that will be present at each output (i.e GPU link) of the corner turn
            engine

        """

        cb1 = self.CROSSBAR
        cb2 = self.CROSSBAR2
        cb3 = self.CROSSBAR3

        number_of_cb1_bin_sel = 16
        number_of_cb2_bin_sel = 2
        number_of_cb3_bin_sel = 8

        if bin_map is None:
            bin_map = {}
        cb1_bin_indices = bin_map.get('cb1', None)
        cb2_bin_indices = bin_map.get('cb2', None)
        cb3_bin_indices = bin_map.get('cb3', None)

        cb2_ignore_lane = 0
        cb3_ignore_lane = 0

        if not (cb1_bin_indices is None or len(cb1_bin_indices) == number_of_cb1_bin_sel):
            raise ValueError(f'1st crossbar bin map should have {int(number_of_cb1_bin_sel)} lists of bins. Ir currently has {len(cb1_bin_indices)}')
        if not (cb2_bin_indices is None or len(cb2_bin_indices) == number_of_cb2_bin_sel):
            raise ValueError(f'2nd crossbar bin map should have {int(number_of_cb2_bin_sel)} lists of bins. Ir currently has {len(cb2_bin_indices)}')
        if not (cb3_bin_indices is None or len(cb3_bin_indices) == number_of_cb3_bin_sel):
            raise ValueError(f'3rd crossbar bin map should have {int(number_of_cb3_bin_sel)} lists of bins. Ir currently has {len(cb3_bin_indices)}')

        def get_dest_slot_for_src_lane(src_lane):
            tx = (self.slot, src_lane)  # unique transmitter id (slot, lane)
            dest_slot = self.crate.get_matching_rx(tx)[0]
            return dest_slot

        # def get_src_slot_for_dest_lane(dest_lane):
        #     rx = (self.slot, dest_lane)
        #     src_slot = self.crate.get_matching_tx(rx)[0]
        #     return src_slot

        if frames_per_packet < 1 or frames_per_packet > 4:
            raise ValueError('Number of frames per packet must be between 1 and 4')
        if cb1_lanes in (4, 8, 12, 16):
            cb1_lanes = [(0, cb1_lanes // 4 - 1)] * number_of_cb1_bin_sel
        else:
            raise ValueError('Crossbar 1 number of input lanes must be 4,8,12 or 16')

        if cb2_lanes is None:
            cb2_lanes = ((0, 15), (0, 15))  # Both bin selectors

        # if cb2_lanes % 2:
        #     raise ValueError('Crossbar 2 number of input lanes must be a multiple of 2')

        cb2_timeout_period = None
        cb2_sof_window_stop = None

        if mode == 'chan8':

            if self.CROSSBAR1_TYPE=="URAM":
                self.set_corr_reset(0)
                self.set_chan_reset(0)
                return (0,)

            # Get raw data from the channelizer (all 32-bit sent as is). Only 8 lanes are available to the GPU.
            #############################
            # 1st Crossbar
            #############################
            # In bypass mode. Bin sel 0-15 get every word out of channelizers 0-15
            cb1_bypass = True
            cb1_four_bit = False
            cb1_combine_data_flags = 0
            cb1_bin_select_map = [[]] * number_of_cb1_bin_sel

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. Lanes are reordered to select which of the 8 channelizers we want to forward.
            cb2_lane_map = np.hstack((chan8_channel_map, chan8_channel_map))  # Here we could select which 8 inputs we want to stream to the GPU
            cb2_bypass = True
            cb2_bin_select_map = [[]] * number_of_cb2_bin_sel

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.
            cb3_lane_map = list(range(8))
            cb3_bypass = True
            crate_number = self.fpga.mb.get_crate_number(default=0)
            cb3_bin_select_map = [[]] * number_of_cb3_bin_sel

            cb3_output_bins = 0  # debug
            cb3_output_words_per_bin = 0  # debug
            cb3_output_data_flags_words_per_bin = 0  # debug
            cb3_output_frame_flags_words_per_frame = 0  # debug

            stream_type = 0
            cb2_ignore_lane = 0
            cb3_ignore_lane = 0


        elif mode == 'chan4':
            """
            In 'chan4' mode, we combine the high nibble of each pair of channelizers, allowing us to
            stream data from ALL channelizers. depending on the configuration of the channelizer, the data can be:

              - 4-bit ADC bit data from all channelizers
              - (4+4) bit FFT value from all channelizers

            """
            #############################
            # 1st Crossbar
            #############################
            # In bypass mode. Bin sel 0 selects 4+4 bit data for all bins of
            # channelizer 0 and 1, etc.  Bin selectors cover all 16
            # channelizers. Bin selectors 8-15 has the same info as bin sel
            # 0-7.
            cb1_bypass = True
            cb1_four_bit = True  # combines high nibbles of pair of input lanes
            cb1_combine_data_flags = 0
            cb1_bin_select_map = []

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering. Data from input lanes 8-15 is redundant and is not forwarded.
            cb2_lane_map = list(range(16))  # All information
            cb2_bypass = True
            cb2_bin_select_map = []

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.
            cb3_lane_map = list(range(8))
            cb3_bin_select_map = []
            cb3_bypass = True
            crate_number = self.fpga.mb.get_crate_number(default=0)
            stream_type = 0

        elif mode in ('shuffle16', 'chord16'):
            """
            In shuffle16 mode, each of the GPU links output data for 128 bins,
            each bins containing the data from 16 channels. The data for each
            channel is a byte, representing the FFT output as a (4+4) bit
            compler number.

            In this mode, each of the 16 bin selector select data from lane
            groups 0-3 (i.e. lanes 0-15). They each select 128 bins, i.e. 1/64
            of the 1024 bins incoming from the channelizer. The first 8 bin
            selectors contain all the data; the last 8 will not be passed by
            the following crossbar.

            There are 4 words per bins (16 channels). The data flags of two
            consecutive bins are combined to build a single data flag word.

            The packet format is as follows (assume one frame per packet):
                - Header: 4 words (16 bytes) for the header
                - Data block: 128 bins x 16 channels = 2056 bytes
                - Data flags block: We combine the data flags, so scaler flags
                  from two consecutive bins are combined into a single 32 bit
                  word ( one bit per channel for each of the 2 bins). This
                  represents 128*16/32=64 words = 256 bytes
                - Frame flags: 16 bit FFT overflow + 16 bit ADC overflow flags
                  per frame. This is 1 word = 4 bytes.
                - Packet flags: 32 bit word (4 bytes)
                - Total: 2328 bytes

            """

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # the first 8 bin selectors so the data can go through the
            # following bypassed crossbars.
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            # set the first and last inlut lane group
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Set the bins selected by each bin selector
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:  # Use default
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 bins, or 4 clocks
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]
            cb1_combine_data_flags = 1
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if we combine_flags
            cb1_output_frame_flags_words_per_frame = 1


            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = True

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering.

            cb2_lane_map = list(range(16))
            cb2_bypass = True
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_bins
            # cb2_lanes : Not applicable because of bypass
            # cb2_bins : Not applicable because of bypass
            # cb2_bin_spacing : Not applicable because of bypass
            # cb2_bin_select_map : Not applicable because of bypass
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.fpga.mb.get_crate_number(default=0)
            stream_type = 1

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True


            #############################
            # 3rd Crossbar
            #############################
            # Bypassed. No channel reordering.

            cb3_lane_map = list(range(8))
            cb3_bypass = True
            cb3_output_words_per_bin = cb2_input_words_per_bin
            cb3_output_bins = cb2_input_bins
            cb3_output_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_output_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame

        elif mode == 'shuffle128':
            """ Configures the corner-turn engine for a 8-board shuffle.

            Boards are assumed to be in slots 1-8 of the crates (slots 0-7).

            In this mode, crossbar1 merges 16 channelizer outputs into 8
            output lanes, with 128 bins per lane, like 'shuffle16'. However,
            we do not bypass the backplane PCB shuffle. The lanes that end up
            in one of the 8 populated slots are used. The backplane data rate is close to the limit (7.5 Gbps). We cannot send the flags due to bandwidth limitations.

            Crossbar2 remaps the 8 received lanes only. Crossbar 2 bin selectors doubles the data rate, so we can't use them and they are bypassed.

            Crossbar3 operates as in shuffle512: data from 8 input lanes is sent to the 8 output GPU lanes. We have 16 bins per lane.
            """
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # the first 8 bin selectors so the data can go through the
            # following bypassed crossbars.
            cb1_bypass = False
            cb1_four_bit = True
            # Bin selectors grab data from all lanes
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel

            # Set the bins selected by each bin selector
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:  # Use default
                cb1_bins = 128
                cb1_bin_spacing = 1024 // cb1_bins  # = 8 bins, or 4 clocks

                # *** JFC debug
                cb1_bin_spacing = 8  # Normally  8 bins, which is the minimum
                cb1_bins = 1024 // cb1_bin_spacing


                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + (i % cb1_bin_spacing)
                    for i in range(number_of_cb1_bin_sel)]

            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]

            cb1_combine_data_flags = 0 # we cannot combine the flags of two bins because we further bin-select them in crossbar 3
            send_flags = False  # There is not enough bandwidth on the backplane to send uncombined flags
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = send_flags * (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if we combine_flags
            cb1_output_frame_flags_words_per_frame = 1 * send_flags


            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            #############################
            # 2nd Crossbar
            #############################
            # Bypassed. No channel reordering.

            # CB2 packet aligner
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            # get a list that indicates which input lanes to get data from so we get it in increasing order of origin slot number
            # eg, if we are in slot 2 (zero-based)
            # (tx_slot => rx_lane) = (0=>1), (1->3), 2->0, 3->2, we have a map of [1, 3, 0, 2]
            # If slot 3 is not sending data,  we should discard  data from lanes lane_map[3] = 2
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            print(f'slot {self.slot-1}/15: CB2 lane map: {cb2_lane_map}') #[ 0  8  6 14  5  4 15  7  3  2 11  1 13 12  9 10]
            cb2_ignore_lane = sum((1<<lane) for lane in cb2_lane_map[8:16])
            cb2_lane_map[8:16] = [0,0,0,0,0,0,0,0]  # hack to make sure all the bin sel input lanes have valid data
            print(f'slot {self.slot-1}/15: CB2 lane map: {cb2_lane_map}') #[ 0  8  6 14  5  4 15  7  3  2 11  1 13 12  9 10]
            cb2_bypass = True

            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_output_bins
            # cb2_lanes : Not applicable because of bypass
            # cb2_bins : Not applicable because of bypass
            # cb2_bin_spacing : Not applicable because of bypass
            # cb2_bin_select_map : Not applicable because of bypass
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_input_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.fpga.mb.get_crate_number(default=0)
            stream_type = 1

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True


            #############################
            # 3rd Crossbar
            #############################
            # Crossbar 2 just passed the 8 lanes from the 8 boards, reordered by slot number origin
            # Crossbar 3 can finish the job, i.e. channels are spread over its
            # 8 input links.

            # Remap input lanes so data is selected in proper channel order
            cb3_lane_map = [0, 1, 2, 3, 4, 5, 6, 7]
            cb3_ignore_lane = 0
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
                cb3_bins = cb3_input_bins // 8
                cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]

            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode == 'shuffle256':
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins betweeen
            # all 16 bin selectors to spread the data across 16 boards in a crate.
            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Select the bins to be assigned to each bin selector output.
            # Here, we assume that the  list is in the order of the
            # destination slot.
            if cb1_bin_indices is not None:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 64
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + i
                    for i in range(number_of_cb1_bin_sel)]
            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]
            cb1_output_words_per_bin = 16 // 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            #############################
            # 2nd Crossbar
            #############################
            #
            # CB2 has 2 BIN_SEL
            # Each BS captures data from 16 input lanes and has 4 outputs.
            # Each output covers gathers data from 4 input lanes (sublanes 0-3).
            #   Output 0: Sublanes 0-3 = Input Lanes 0-3
            #   Output 1: Sublanes 0-3 = Input Lanes 4-7
            #   Output 2: Sublanes 0-3 = Input Lanes 8-11
            #   Output 3: Sublanes 0-3 = Input Lanes 12-15
            #
            # In this config, we will bypass the crate_shuffle. We therefore want all outputs to
            # output the same bins. So BS0 gets data from half of its sublanes, and BS1 gets data
            # from the other half.
            #
            # CB2 Output Lane 0: BS0.0: all 64 bins from sublanes 0-1 (Input lanes 0-1   = CH0-31)
            # CB2 Output Lane 1: BS0.1: all 64 bins from sublanes 0-1 (Input lanes 4-5   = CH64-95)
            # CB2 Output Lane 2: BS0.2: all 64 bins from sublanes 0-1 (Input lanes 8-9   = CH128-159)
            # CB2 Output Lane 3: BS0.3: all 64 bins from sublanes 0-1 (Input lanes 12-13 = CH192-223)
            # CB2 Output Lane 4: BS1.0: all 64 bins from sublanes 2-3 (Input lanes 2-3   = CH32-63)
            # CB2 Output Lane 5: BS1.1: all 64 bins from sublanes 2-3 (Input lanes 6-7   = CH96-127)
            # CB2 Output Lane 6: BS1.2: all 64 bins from sublanes 2-3 (Input lanes 10-11 = CH160-191)
            # CB2 Output Lane 7: BS1.3: all 64 bins from sublanes 2-3 (Input lanes 14-15 = CH224-255)
            # Crate shuffle is bypassed.
            # CB3 inputs are therefore identical to CB2 output
            # CB3 lane map selects data in the order: [BS0.0, BS1.0, BS0.1, BS1.1 ...]
            # CB3 remapped BIN_SEL inputs are
            #    CB3 Input Lane 0: 64 bins CH0-31
            #    CB3 Input Lane 1: 64 bins CH32-63
            #    CB3 Input Lane 2: 64 bins CH64-95
            #    CB3 Input Lane 3: 64 bins CH96-127
            #    CB3 Input Lane 4: 64 bins CH128-159
            #    CB3 Input Lane 5: 64 bins CH160-191
            #    CB3 Input Lane 6: 64 bins CH192-223
            #    CB3 Input Lane 7: 64 bins CH224-255
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 8 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 8 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 8 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 8 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 8 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 8 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 8 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 8 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            # CB2 ALIGN (JM changed cb2_sof_window_stop from 70 to 50 which the default value of CROSSBAR.SOF_WINDOW_STOP in shuffle_crossbar module. When 70, get errors when establishing connection with gpu nodes, reporting wrong packet size). JFC: CHanged to 55 during backplane shuffle debugging
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            cb2_bypass = False
            # CB2 BIN_SEL
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_data_flags_words_per_bin = 1
            cb2_input_frame_flags_words_per_frame = 1

            # BS0 selects sublanes 0-1, i.e. its 4 outputs gather data from lanes 0-1, 4-5, 8-9, and 12-13
            # BS1 selects sublanes 2-3 (lanes 2-3, 6-7, 10-12 and 14-15 )
            cb2_lanes = ((0, 1), (2, 3))
            cb2_combine_data_flags = True  # hardwired to True in crossbar 2
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1  # 2 input lanes per output

            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices is not None:
                cb2_bins = len(cb2_bin_indices[0])
                cb2_bin_select_map = cb2_bin_indices
            else:
                # Default: we select all 64 incoming bins, but from half the input lanes
                cb2_bins = 64
                cb2_bin_spacing = 1
                cb2_bin_select_map = [np.arange(cb2_bins) * cb2_bin_spacing
                                      for i in range(number_of_cb2_bin_sel)]

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane

            cb2_output_data_flags_words_per_bin = (
                cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane
                // (2 if cb2_combine_data_flags else 1))

            cb2_output_frame_flags_words_per_frame = (
                cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane)

            cb2_output_bins = cb2_bins
            crate_number = self.fpga.mb.get_crate_number(default=0)
            stream_type = 2

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Crossbar 2 partially combined the channels in a way that
            # Crossbar 3 can finish the job, i.e. channels are spread over its
            # 8 input links.

            # Remap input lanes so data is selected in proper channel order In
            # shuffle256, input lanes 0-3 are from BS2 input anes 0-1, 4-5,
            # 8-9, and 12-13, and input lanes 4=7 are from lanes 2-3, 6-7,
            # 10-12 and 14-15.
            cb3_lane_map = [0, 4, 1, 5, 2, 6, 3, 7]
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
                cb3_bins = 8
                cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]

            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode == 'shuffle512':
            """
            Like shuffle256, but adds a corner-turn operation between pair of crates using the
            backplane QSFP connections between them.
            """
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins '
                                   'to the target boards in the same crate')

            #############################
            # 1st Crossbar
            #############################
            # Selects data from all channelizers, and spread the bins between
            # all 16 bin selectors to spread the data across 16 boards in a crate.
            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            # get data from channel group 0 - 3. Each channel group combines data from 4 channelizers (in 4 bit mode)
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            # Select the bins to be assigned to each bin selector output.
            # Here, we assume that the  list is in the order of the
            # destination slot.
            if cb1_bin_indices is not None:
                cb1_bin_select_map = cb1_bin_indices
                cb1_bins = len(cb1_bin_indices[0])
            else:
                cb1_bins = 64
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    np.arange(cb1_bins) * cb1_bin_spacing + i
                    for i in range(number_of_cb1_bin_sel)]
            # Reorder cb1_bin_select_map with the knowledge of the backplane
            # PCB links connectivity so slot 0 gets cb1_bin_select_map[0],
            # slot 1 gets cb1_bin_select_map[1] etc.
            cb1_bin_select_map = [
                cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i) - 1]]
                for i in range(16)]

            # Output packet geometry
            cb1_output_words_per_bin = 16 // 4
            cb1_output_bins = cb1_bins

            #################################
            # Backplane PCB (intra-crate) shuffle
            #################################
            bp_shuffle_bypass = False

            # In this config, we do not bypass the crate_shuffle. Half the bins are sent out, and we
            # receive bins that are the same as those of the direct lanes.
            #
            # We therefore want BS0 to get half the bins from all input lanes, and BS1 gets the
            # other half of the bins also from all input lanes.
            #
            # CB2 Output lanes are:
            #    CB2 Output Lane 0: BS0.0: 32 even bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 1: BS0.1: 32 even bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 2: BS0.2: 32 even bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 3: BS0.3: 32 even bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            #    CB2 Output Lane 4: BS1.0: 32 odd  bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 5: BS1.1: 32 odd  bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 6: BS1.2: 32 odd  bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 7: BS1.3: 32 odd  bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            # Crate shuffle is *not* bypassed
            # CB3 inputs are therefore:
            #    CB3 Input Lane 0: 32 even bins CH0-63
            #    CB3 Input Lane 1: 32 even bins CH64-127
            #    CB3 Input Lane 2: 32 even bins CH128-191
            #    CB3 Input Lane 3: 32 even bins CH192-255
            #    CB3 Input Lane 4: 32 even bins CH256-319
            #    CB3 Input Lane 5: 32 even bins CH320-383
            #    CB3 Input Lane 6: 32 even bins CH384-447
            #    CB3 Input Lane 7: 32 even bins CH448-511
            # CB3 lane map selects data in the input lane order: [0, 1, 2, 3 ... 7]
            # CB3 BIN sel inputs are therefore identical to CB3 inputs
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 4 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 4 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 4 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 4 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 4 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 4 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 4 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 4 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            #############################
            # 2nd Crossbar
            #############################

            # CB2 ALIGN
            # cb2_timeout_period = 0

            crate_number = self.fpga.mb.get_crate_number(default=0)
            stream_type = 3  # crossbar 3-level data

            # Input packet geometry

            cb2_input_data_flags_words_per_bin = 1
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_frame_flags_words_per_frame = 1

            # Configuration
            cb2_sof_window_stop = 55
            cb2_bypass = False
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            # CB@ BIN_SEL
            cb2_lanes = [(0, 3), (0, 3)]  # Every output of both bin sels get data from all the 4 sublanes they get.
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1  # 4 input lanes per output
            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices is not None:
                cb2_bin_select_map = cb2_bin_indices
                cb2_bins = len(cb2_bin_indices[0])
            else:
                # Default: we select half the bins (from all input lanes)
                cb2_bins = cb1_output_bins // number_of_cb2_bin_sel  # 64/2 = 32
                cb2_bin_spacing = number_of_cb2_bin_sel  # 2
                cb2_bin_select_map = [
                    np.arange(cb2_bins) * cb2_bin_spacing + i
                    for i in range(number_of_cb2_bin_sel)]
            # swap bin selection list on odd crates so the bins on the first
            # list are sent to the other (even) crate
            if crate_number & 1:
                cb2_bin_select_map = cb2_bin_select_map[::-1]

            cb2_combine_data_flags = True  # hardwired to True in crossbar 2

            # Output packet geometry

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane
            cb2_output_bins = cb2_bins
            cb2_output_data_flags_words_per_bin = (
                cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane //
                (2 if cb2_combine_data_flags else 1))
            cb2_output_frame_flags_words_per_frame = (
                cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane)
            # print("cb2_output frame flags words=%i, input frame flags words=%i, input_lanes=%i" % (
            #   cb2_output_frame_flags_words_per_frame,
            #   cb2_input_frame_flags_words_per_frame,
            #   cb2_input_lanes_per_output_lane))

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = False
            # crate_shuffle_bypass = True #***debug

            #############################
            # 3rd Crossbar
            #############################
            # Crossbar gathers the data from the 2 crates.

            # Input packet geometry
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins

            # Configuration

            # Input lane remapping to keep the channel number consistent
            # whether we are on an odd or even crate number.
            #
            # For the even crate, input lane 0-3 contains the local even bins
            # for the low channels, and lane 4-7 get the even bins from the
            # other crate (high channels). For the odd crate, input lane  0-3
            # are the odd bins from the local high channels, and lanes 3-7 are
            # the odd bins from the other crate low channels.
            #
            # JM: modified this line to have consistent input ordering between
            # pairs of crates. Before this change this line was just range(8)
            cb3_lane_map = [4, 5, 6, 7, 0, 1, 2, 3] if (crate_number & 1) \
                else [0, 1, 2, 3, 4, 5, 6, 7]
            cb3_bypass = False
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1  # 8 input lanes per bin sel output

            # Select the bins to be assigned to each bin selector output.
            if cb3_bin_indices is not None:
                cb3_bins = len(cb3_bin_indices[0])
                cb3_bin_select_map = cb3_bin_indices
            else:
                # Default: we select 1/8th of the incoming bins from all the input lanes
                cb3_bins = cb2_output_bins // number_of_cb3_bin_sel  # 32/8 = 4
                cb3_bin_spacing = number_of_cb3_bin_sel  # = 8
                cb3_bin_select_map = [
                    np.arange(cb3_bins) * cb3_bin_spacing + i
                    for i in range(number_of_cb3_bin_sel)]
            cb3_combine_data_flags = False  # hardwired to False in crossbar 3

            # Output packet geometry
            cb3_output_words_per_bin = cb3_input_words_per_bin * cb3_input_lanes_per_output_lane
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = (
                cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane
                // (2 if cb3_combine_data_flags else 1))
            cb3_output_frame_flags_words_per_frame = (
                cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane)

        elif mode in ('corr16', ):
            """
            Corr16 now has a fixed, wired corner-turn engine that pass the channelizer data to the correlator. Ther is not much to do here.
            """

            # corr16 mode uses the hardwired corner-turn, so there is nothing more to do here, so return immediately

            if self.CT_LEVEL != 1:
                raise ValueError(f'corr16 mode requires a CT level of 1. {self.CT_LEVEL} is not supported.')
            return range(16)


        elif mode is None:  # Manual config
            cb1_four_bit = True

            # cb1_bypass = False

            # Select the bins to be assigned to each bin selector output.
            if cb1_bin_indices:
                cb1_bin_select_map = cb1_bin_indices
            else:
                cb1_bin_spacing = 1024 // cb1_bins
                cb1_bin_select_map = [
                    (np.arange(cb1_bins) * cb1_bin_spacing + i) % 1024
                    for i in range(number_of_cb1_bin_sel)]

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            crate_shuffle_bypass = 1

            crate_number = self.fpga.mb.get_crate_number(default=0)
            stream_type = 4

            cb2_input_bins = cb1_bins
            if bp_shuffle_bypass and self.slot is not None:
                cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            else:
                cb2_lane_map = list(range(16))

            cb2_input_frame_flags_words_per_frame = 1
            cb2_input_data_flags_words_per_bin = 1
            # Select the bins to be assigned to each bin selector output.
            if cb2_bin_indices:
                cb2_bins = len(cb2_bin_indices[0])
                cb2_bin_select_map = cb2_bin_indices
            else:
                # Default: we select all 64 incoming bins, but from half the input lanes
                cb2_bins = cb2_input_bins
                cb2_bin_spacing = 1
                cb2_bin_select_map = [np.arange(cb2_bins) * cb2_bin_spacing
                                      for i in range(number_of_cb2_bin_sel)]

            #################################
            # Backplane QSFP (crate) shuffle
            #################################
            # Not supported in this mode
            crate_shuffle_bypass = True

            #############################
            # 3rd Crossbar
            #############################
            # Not supported in this mode
            cb3_bypass = True
            cb3_lane_map = list(range(8))

            cb1_output_words_per_bin = cb1_lanes[0][1] - cb1_lanes[0][0] + 1
            cb1_output_bins = cb1_bins

        else:
            raise ValueError('Unknown mode')

        # cb2_payload_size = header_size + packet_flags_size + frames_per_packet \
        #   * (cb2_input_words_per_bin * cb2_bins* cb2_lanes + 1*cb2_bins*cb2_lanes/2 + cb2_lanes) * 4

        self.logger.debug(
            f'{self!r}: Configuring crossbars 1 & 2 with frames_per_packet={int(frames_per_packet)}, cb1_lanes={cb1_lanes}, cb1_bins={int(cb1_bins)}, cb2_lanes={cb2_lanes}, cb2_bins={int(cb2_bins)}, cb2_bypass={bool(cb2_bypass)}, bp_shuffle_bypass={bool(bp_shuffle_bypass)}')

        # Put everything in reset
        self.fpga.set_chan_reset(1)
        self.fpga.set_corr_reset(1)

        # slot_number = self.slot - 1 if self.slot is not None else 0
        slot_number = self.slot - 1 if self.slot else 0  # SC 03/17/2019 changed for individual iceboard

        ###########################
        #  Configure CROSSBAR 1
        ###########################

        stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                     for lane in range(cb1.NUMBER_OF_CROSSBAR_OUTPUTS)]
        for (cb1_output_lane, bs) in enumerate(cb1):
            bs.BYPASS = cb1_bypass
            bs.STREAM_ID = stream_id[cb1_output_lane] >> 4  # lane is already hardwared in the last 4 bits
            bs.COMBINE_DATA_FLAGS = cb1_combine_data_flags
            bs.SEND_FLAGS = send_flags
            bs.GROUP_FRAMES = frames_per_packet
            # print(stream_type, crate_number, slot_number)
            bs.FOUR_BITS = cb1_four_bit
            bs.FIRST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][0]
            bs.LAST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][1]
            # bs.NUMBER_OF_LANES = cb1_lanes
            bs.select_bins(cb1_bin_select_map[cb1_output_lane])

        ###########################
        # Configure BP_SHUFFLE
        ###########################
        if self.BP_SHUFFLE:
            # Configure PCB (intra-crate) shuffle
            self.BP_SHUFFLE.BYPASS_PCB_SHUFFLE = bp_shuffle_bypass
            # Configure CRATE (inter-crate) shuffle
            self.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE = crate_shuffle_bypass
        elif not bp_shuffle_bypass:
            raise RuntimeError("The FPGA firmware implement BP_SHUFFLE in the '%s' operational mode "
                               "(shuffle bypass flag is not set in that mode)", mode)
        ###########################
        # Configure CROSSBAR 2
        ###########################
        if cb2:
            if cb2_bypass:  # if we bypass, just remap the stream ids from the previous crossbar
                stream_id = [stream_id[i] for i in cb2_lane_map]
            else:  # otherwise, the bin selector overrides
                stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                             for lane in range(cb2.NUMBER_OF_CROSSBAR_OUTPUTS)]
            cb2.IGNORE_LANE = cb2_ignore_lane
            cb2.set_lane_map(cb2_lane_map)
            if cb2_timeout_period is not None:
                cb2.TIMEOUT_PERIOD = cb2_timeout_period
            if cb2_sof_window_stop is not None:
                cb2.SOF_WINDOW_STOP = cb2_sof_window_stop
            for (cb2_bin_sel, bs) in enumerate(cb2):
                bs.BYPASS = bool(cb2_bypass)
                if not cb2_bypass:
                    bs.STREAM_ID = stream_id[cb2_bin_sel * cb2.NUMBER_OF_OUTPUTS_PER_BIN_SEL] >> 4
                    bs.SEND_FLAGS = send_flags
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    bs.NUMBER_OF_BINS_PER_FRAME = cb2_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb2_input_words_per_bin
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = cb2_input_data_flags_words_per_bin
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb2_input_frame_flags_words_per_frame
                    bs.FIRST_LANE = cb2_lanes[cb2_bin_sel][0]
                    bs.LAST_LANE = cb2_lanes[cb2_bin_sel][1]
                    bs.select_bins(cb2_bin_select_map[cb2_bin_sel])
        elif not cb2_bypass:
            raise RuntimeError("The FPGA firmware must have a CROSSBAR2 in the '%s' operational mode", mode)

        ###########################
        # Configure CROSSBAR 3
        ###########################
        if cb3:
            cb3.IGNORE_LANE = cb3_ignore_lane
            cb3.set_lane_map(cb3_lane_map)
            if cb3_bypass:  # if we bypass, just remap the stream ids from the previous crossbar
                stream_id = [stream_id[i] for i in cb3_lane_map]
            else:  # otherwise, the bin selector overrides
                stream_id = [((stream_type << 12) | (crate_number << 8) | (slot_number << 4) | lane)
                             for lane in range(cb3.NUMBER_OF_CROSSBAR_OUTPUTS)]
            for (cb3_bin_sel, bs) in enumerate(cb3):
                bs.BYPASS = bool(cb3_bypass)
                if not cb3_bypass:
                    bs.STREAM_ID = stream_id[cb3_bin_sel * cb3.NUMBER_OF_OUTPUTS_PER_BIN_SEL] >> 4
                    bs.SEND_FLAGS = send_flags
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    assert cb3_input_data_flags_words_per_bin == int(cb3_input_data_flags_words_per_bin), f'CB3 number of flag words is not an integer ({cb3_input_data_flags_words_per_bin})'
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = int(cb3_input_data_flags_words_per_bin)
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb3_input_frame_flags_words_per_frame
                    # print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.FIRST_LANE = cb3_lanes[cb3_bin_sel][0]
                    bs.LAST_LANE = cb3_lanes[cb3_bin_sel][1]
                    # print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.NUMBER_OF_BINS_PER_FRAME = cb3_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb3_input_words_per_bin
                    bs.select_bins(cb3_bin_select_map[cb3_bin_sel])

                    # bs.SEND_FLAGS = 0  # JFC debug. Does not affect data.
            # print("cb3 input frame flags words=%i" %cb3_input_frame_flags_words_per_frame)
        elif not cb3_bypass:
            raise RuntimeError("The FPGA firmware must implement CROSSBAR3 in the '%s' operational mode", mode)

        def print_packet_size(
                crossbar_name,
                frames_per_packet,
                bins,
                data_words_per_bin,
                data_flags_words_per_bin,
                frame_flags_words_per_frame):
            header_words_per_packet = 4
            packet_flags_words_per_packet = 1
            if not send_flags:
                data_flags_words_per_bin = 0
                frame_flags_words_per_frame = 0
            payload_size = 4 * (
                header_words_per_packet +
                frames_per_packet * (
                    (data_words_per_bin + data_flags_words_per_bin) * bins +
                    frame_flags_words_per_frame) +
                packet_flags_words_per_packet)
            ethernet_packet_overhead_bytes = 42
            ethernet_packet_size = (ethernet_packet_overhead_bytes + payload_size + 7) // 8 * 8
            # eth_data_rate = 156.25e6 * 66 * 32/33
            # bp_data_rate = 156.25e6* 50 * 32/33
            packet_rate = 800e6 / 2048 / frames_per_packet
            ethernet_data_rate = (packet_rate * ethernet_packet_size) * 8
            self.logger.info(
                f'{self!r}: {crossbar_name}, Eth packet size {ethernet_packet_size} bytes, Eth data rate {ethernet_data_rate/1e9:0.1f} Gbit/s')
            self.logger.debug(
                f'{self!r}: {crossbar_name}:'
                f'   UDP payload size: {payload_size} bytes\n'
                f'   Ethernet packet size: {ethernet_packet_size} bytes\n'
                f'   Ethernet data rate: {ethernet_data_rate/1e9:0.1f} Gbit/s\n'
                f'   Packet geometry: {frames_per_packet} frames_per_packet\n'
                f'                    {bins} bins\n'
                f'                    {data_words_per_bin} data words/bin\n'
                f',                   {data_flags_words_per_bin} data flags_words/bin\n'
                f'                    {frame_flags_words_per_frame} frame_flags_words/frame)'
                )
            # self.logger.info('%r: %s config: frames_per_packet=%i, cb1_lanes=%s, cb1_bypass=%s, '
            #                   'cb1_combine=%s, cb1_bins=%i, cb1_words_per_bin=%i' % (
            #                   self, frames_per_packet, cb1_lanes, bool(cb1_bypass), bool(cb1_combine_data_flags),
            #                   cb1_bins, cb1_output_words_per_bin ))
            # self.logger.debug(
            #   '%r: CROSSBAR1 output packets payload = %i bytes (%i words)' % (
            #       self, cb1_payload_size, (cb1_payload_size+3)//4))

        print_packet_size('CROSSBAR3',
                          frames_per_packet=frames_per_packet,
                          bins=cb3_output_bins,
                          data_words_per_bin=cb3_output_words_per_bin,
                          data_flags_words_per_bin=cb3_output_data_flags_words_per_bin,
                          frame_flags_words_per_frame=cb3_output_frame_flags_words_per_frame)

        self.set_corr_reset(0)
        self.set_chan_reset(0)
        return stream_id
