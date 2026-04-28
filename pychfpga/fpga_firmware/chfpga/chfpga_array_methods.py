"""Array-wide methods for :class:`pychfpga.fpga_firmware.chfpga.chfpga.chFPGA`."""

import time

import numpy as np

from pychfpga.common.array_methods import ArrayMethods


class chFPGAArrayMethods(ArrayMethods):
    """Array-wide methods for chFPGA firmware arrays."""


    def sync(self, delay=2 - 0.006556800, check=True, align_to_seconds=True, max_trials=3, max_sync_time_difference=None):
        """Generate a SYNC event across the whole array.

        Parameters:

        delay (float): Sets in how much time in the future after the current
            time the sync time  will happen. The system will determine and the
            time it takes to issue the command across the array. If
            ``align_to_seconds`` is True, the delay is applied after the
            current time + propagation time is rounded to the second, allowing
            a find tuning of the trigger time down to 10 nanosecond
            increments.

        check (bool): If True, the method will read the SYNC
            counters on every board to confirm that the SYNC really happened
            everywhere.

        align_to_seconds (bool): if True, the trigger time **before** the
            ``delay`` is applied is rounded to the closest integer second.
        """

        # Don't try to check the SYNC status if we don't have a SYNC engine (REFCLK) in the firmware of all boards

        if check and not all(list(mb.REFCLK for mb in self.ib)):
            check = False
            self.logger.warning(f'{self!r}: SYNC will not be checked as not all firmware have SYNC logic')

        trial = 0
        while True:
            try:
                if check:
                    sync_ctr_before = self.ib.REFCLK.SYNC_CTR

                self.sync_timestamps = []
                self.sync_timestamp = None

                if self.sync_method in self.TRIG_SYNC_METHODS:
                    self.sync_master.generate_sync()

                # elif self.sync_method == 'centralized_time_trigger':
                #     dt = self.sync_master.get_irigb_time_sync()
                #     print('Triggering SYNC at ', dt.isoformat())
                #     self.sync_master.set_irigb_trigger_time_sync(dt, delay=delay)
                #     t0 = time.time()
                #     while self.sync_master.is_irigb_before_trigger_time_sync():
                #         if time.time() - t0 > delay + 1:
                #             raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')

                elif self.sync_method in self.IRIGB_SYNC_METHODS:
                    # Estimate how much time it takes to set the trigger time
                    dt = self.ib[0].get_irigb_time_sync()
                    t0 = time.time()
                    # set the trigger far enough in time it should not happen before we reprogram another delay
                    self.ib.set_irigb_trigger_time_sync(dt, delay=300)
                    setting_time = (time.time() - t0)
                    self.logger.info(f'{self!r}: It takes {setting_time:0.3f} seconds to set the trigger time across the array')
                    setting_time = round(2 * setting_time)
                    # Now set the trigger time using that delay
                    dt = self.ib[0].get_irigb_time_sync()
                    if align_to_seconds:
                        self.logger.info(f'{self!r}: Rounding trigger time to the second')
                        dt = dt.replace(microsecond=0)
                    # self.print_flush()
                    t0 = time.time()
                    if isinstance(delay, (list, tuple)):
                        max_delay = max(delay)
                        sync_time = []
                        for mb,dly in zip(self.ib, delay):
                            self.logger.info(f'{self!r}: Triggering SYNC on {mb!r} {setting_time+dly:0.9f} seconds after {dt.isoformat()}')
                            sync_time.append(mb.set_irigb_trigger_time_sync(dt, delay=setting_time+dly))
                        self.sync_start_time = sync_time
                    else:
                        max_delay = delay
                        self.logger.info(f'{self!r}: Triggering SYNC {setting_time+delay:0.9f} seconds after {dt.isoformat()}')
                        self.sync_start_time = sync_time = self.ib.set_irigb_trigger_time_sync(dt, delay=setting_time+delay)
                    self.logger.info(f'{self!r}: It took {time.time() - t0:0.3f} seconds to set the final trigger time')
                    t0 = time.time()
                    # for _ in range(30):
                    while any(self.ib.is_irigb_before_trigger_time_async()):
                        #     print(f'trig={any(self.ib.is_irigb_before_trigger_time_async())}, sync={self.ib.REFCLK.SYNC_CTR}')
                        if time.time() - t0 > max_delay + 1:
                            raise RuntimeError('Timout while waiting for the IRIG-B-based SYNC to complete')
                        time.sleep(0.1)
                    time.sleep(0.1) # make sure the sync sequence has time to finish
                elif self.sync_method in self.LOCAL_SYNC_METHODS:
                    self.ib.sync()
                # elif self.sync_method == 'external':
                #     # Wait until one of the board sees a trig. We assume all
                #     # of them will have triggered by the time that is checked
                #     # later.
                #     t0 = time.time()
                #     while all([mb.REFCLK.SYNC_CTR == sync_ctr_before[i] for i, mb in enumerate(self.ib)]):
                #         if time.time() - t0 > 5 + 1:
                #             raise RuntimeError('Timout while waiting for the external sync signal')
                else:
                    valid_methods = ', '.join((self.IRIGB_SYNC_METHODS[0], self.TRIG_SYNC_METHODS[0], self.LOCAL_SYNC_METHODS[0]))
                    raise ValueError(f"Unknown syncing method '{self.sync_method}'. Valid methods are {valid_methods}")

                # Check if all boards of the array have sync'ed by looking at the sync counter
                if check:
                    sync_ctr_after = self.ib.REFCLK.SYNC_CTR
                    bad_mb = [mb for i, mb in enumerate(self.ib) if (sync_ctr_after[i] - sync_ctr_before[i]) & 0xf != 1]
                    if bad_mb:
                        bad_boards_str = ','.join(repr(mb) for mb in bad_mb)
                        raise RuntimeError(f'The following IceBoards did not SYNC properly: {bad_boards_str}')

                if self.sync_method in self.IRIGB_SYNC_METHODS:
                    self.sync_timestamps = ts = self.ib.get_irigb_time_sync(trig=False, format='raw')
                    self.sync_timestamp = ts[0]

                    delta_ts = max(ts.nano) - min(ts.nano)
                    irigb_times = '\n'.join(
                        f'         {mb!r}: {ts[i].isoformat()} '
                        f'({ts[i].nano} ns since epoch, '
                        f'{ts[i].nano - sync_time[i].nano} ns after sync)'
                        for i, mb in enumerate(self.ib))
                    self.logger.info(f'{self!r}: The IRIG-B time for Frame 0 on all boards is:\n{irigb_times}')
                    self.logger.info(f'{self!r}: The maximum Frame 0 time difference is {delta_ts} ns')
                    max_sync_time_difference = max_sync_time_difference or self.max_sync_time_difference
                    if delta_ts > max_sync_time_difference:
                        raise RuntimeError(f'The Frame 0 time difference of {delta_ts} '
                                           f'exceeds the maximum limit of {max_sync_time_difference}')
                for mb in self.ib:
                    mb.reset_scaler_overflow_flags()

                break # we made it all the way through successfulle, so exit the trial loop

            except Exception as e:
                trial += 1
                if trial >= max_trials:
                    raise RuntimeError(f'SYNC failed after {trial} trials. The last exception was:\n{e!r}')
                self.logger.warning(f'{self!r}: SYNC failed on trial {trial}/{max_trials} '
                                    f'due to the following error. Will retry.\n{e!r}')


    def get_corner_turn_bin_map(self, mode, bad_links=None, bin_priority=None, remap_level=0, verbose=0):
        """
        Get the bin selection map for every board of the array in order to
        route the bins toward the  desired output links

        Parameters:

            mode (str): opertional mode. 'shuffle256' , 'shuffle512', 'shuffle16' and 'chord16'
                implement frequency-remaping. Other modes return de default
                map.

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
                self.compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 2:
                self.compute_cb2_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 1:
                self.compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
                # self.shuffle512_cb3_freq_remap(bin_map, bad_links, bin_priority)
            # Apply crates 0 & 1 map to all pair of crates
            bin_map = {(crate, slot): bin_map[((crate or 0) & 1, slot)]
                       for (crate, slot) in self.ib.get_id()}

            # cb3_map = self.shuffle512_cb3_remap(mode=mode,
            #                                     bad_links=bad_links,
            #                                     freq_bins=bin_priority,
            #                                     output_cb3_bins=True)
            # # cb3_map describes only odd and even crate numbers. We expand the list to cover all crates explicitely.
            # for mb in self.ib:
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
                self.compute_cb1_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            if remap_level >= 1:
                self.compute_cb3_bin_map(bin_map, bad_links, bin_priority, verbose=verbose)
            # Apply crates 0 map to all other crates
            bin_map = {(crate, slot): bin_map[(0, slot)]
                       for (crate, slot) in self.ib.get_id()}
        # elif mode == 'shuffle16':
        #     # use only the default bin map: we have no flexibility. All we can do is to remap the outputs.
        #     bin_map = dict(
        #             cb1=np.arange(1024).reshape((16, 64), order='F'),
        #             cb2=np.arange(64).repeat(2).reshape(2, 64, order='F'), # bypassed
        #             cb3=np.arange(64).reshape((8, 8), order='F')) # bypassed
        #     # Assign the same bin map to all boards in the array
        #     bin_map = {(crate, slot):bin_map
        #                 for (crate, slot) in self.ib.get_id()}
        else:  # other modes. Use defaults.
            bin_map = {mb.get_id(): {'cb1': None, 'cb2': None, 'cb3': None}
                       for mb in self.ib}
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


    IRIGB_SYNC_METHODS = ('irig-b', 'irigb', 'distributed_time')
    TRIG_SYNC_METHODS = ('trig',  'centralized_soft_trigger')
    LOCAL_SYNC_METHODS = ('local', 'local_soft_trigger')

    def set_sync_method(self, method='irig-b', source=None, master=None, master_source=None, master_output=0):
        """ Sets the global syncing method, and setup the boards accordingly.

        Parameters:

            method (string): Specifies the synchronization method:

                - ``'irig-b'`` (or ``'irigb'``, ``'distributed_time'``): All boards sychronize when
                  the target IRIG-B time is reached. `source` specifies the source of the IRIG-B
                  signal that feeds the IRIG-B decoder module.

                - ``'trig'`` (or ``'centralized_soft_trigger'``): All boards synchronize on the
                  rising edge of the signal specified in ``source``.

                - ``'local'`` (or ``'local_soft_trigger'``): Each board generates its own
                  software-generated SYNC trigger when the `sync()` method is called. This can be
                  used to reset the processing pipeline of each board, but does not allow
                  synchronized capture packed capture between board or use of multi-board
                  corner-turning operational modes.

            source: (string): source of synchronization signal.
                - If ``method='irig-b'``, `source` must match one of the available IRIG-B sources (see motherboard method `set_irigb_source_async()`).

                - If ``method='trig'``, `source` must match one of the available REFCLK sources (see REFCLK method `set_sync_source`).

            master (Motherboard, string, int): Motherboard that is to be configured to generate the time
                or trigger signals.

                - None: No master board is set up.
                - `Motherboard` object: use the provided Motherboard object
                - str: Use board with specified serial number (must include all leading zeros)
                - int: Use board in specified slot number (starts with index 1)

            master_source: (string): source of the synchornization signal that the master board will
                send to the master output. Used as ``source`` paramater in method
                `set_user_output_source` called on the master board.

            master_output: (int): User output to which the synchronization signal will be sent. Used
                as ``output`` paramater in method `set_user_output_source` called on the master
                board.

        The boards start their sync process on the next rising edge of the 10
        MHz reference after the trigger event.  This events shoudld
        to occur between two reference clock edges in order to guarantee
        detection on the same edge across the entire array.

        """
        if master:
            if isinstance(master, str):
                master = self.ib.get(serial=master)
            elif isinstance(master, int):
                master = self.ib.get(slot=master)
            master.set_user_output_source(source=master_source, output=master_output or 0)
            if source == 'bp_gpio_int':
                # Enable GPIO_INT as an output only on the master board
                # TODO: JFC: This code should be moved to lower-level methods
                self.ib.GPIO.BP_GPIO_INT_EN = 0
                master.GPIO.BP_GPIO_INT_EN = 1

        self.sync_master = master
        self.sync_method = method

        if source == 'bp_gpio_int' and not master:
            raise RuntimeError('A master board should be specified when bp_gpio_int is used as a source')

        if method in self.IRIGB_SYNC_METHODS: # REFCLK trigs on the output of the IRIG-B time comparator
            source = source or 'bp_time'
            self.ib.set_sync_source('irigb')
            self.ib.set_irigb_source_sync(source)
        # elif method == 'centralized_time_trigger':
        #     source = source or 'bp_time'
        #     # if not master or not master_source:
        #     #     raise ValueError('In the centralized time trigger mode, the master board '
        #     #                      'and its time source must be specified')
        #     # self.ib.set_sync_source(source)
        #     master.set_irigb_source_sync(master_source)
        #     master.set_user_output_source('irigb_trig')
        elif method in self.TRIG_SYNC_METHODS: # REFCLK trigs directly on the rising edge of the specified source signal
            source = source or 'bp_time'
            self.ib.set_sync_source(source)
        elif method in self.LOCAL_SYNC_METHODS:
            self.ib.set_sync_source('local')
            self.ib.sync()
        # elif method == 'external':
        #     source = source or 'sma_a'  # if no source is specified
        #     if master:
        #         raise ValueError('In the local soft trigger mode, a master board should NOT specified')
        #     if master_source:
        #         raise ValueError('In the local soft trigger mode, a master_source should NOT be specified')
        #     self.ib.set_sync_source(source)
        else:
            raise ValueError(f"Unknown syncing method '{method}'")



    def set_channelizers(self, channels=None, local_sync=None, sync=True, **kwargs):
        """
        Configures all channelizers in the array.

        See firmware set_channelizer(...) for details.
        """
        if local_sync is not None:
            raise RuntimeError('local_sync parameter is not supported on array-wide set_channelizer call')
        for mb in self.ib:
            mb.set_channelizer(**kwargs, channels=channels, local_sync=False)
        if sync:
            self.sync()

    async def set_channelizers_async(self, channels=None, local_sync=None, sync=True, **kwargs):
        """
        Configures all channelizers in the array.

        See firmware set_channelizer(...) for details.
        """
        if local_sync is not None:
            raise RuntimeError('local_sync parameter is not supported on array-wide set_channelizer call')
        for mb in self.ib:
            mb.set_channelizer(**kwargs, channels=channels, local_sync=False)
            await asyncio.sleep(0)
        if sync:
            self.sync()

    def set_noise_injection(
            self,
            board,
            enable=False,
            offset=0,
            high_time=8388608,
            period=16777216,
            local_sync=True,
            output='bp_sma'):
        """ Configure noise injection gating signal.

        Parameters:

            board:  is either the serial number (as a string) of the target
                board, or is the target motherboard object. If board evaluates to
                False (empty string), the noise injection setup is skipped.

        Notes:

            A sync event is necessary to restart the counters so the gating signal will be generated properly.

        """
        if not board:
            return

        mb = self.get_iceboard(board)

        if enable:
            mb.set_user_output_source('pwm', output=output)

        mb.set_pwm(enable=enable, offset=offset, high_time=high_time,
                   period=period, local_sync=local_sync)

    async def get_user_output_state_async(self, board, output='bp_sma'):
        """ Get the current state of the specified user output and the
        information about its source.

        Parameters:

            board (tuple/list, str, Motherboard):  is either a 2-element tuple or
                list describing the board, or a string containing the board's
                serial number or hostname.

            output (str): name of the user output to which the noise injection
                hardware is connected. This will be used to verify if the PWM
                signal is actually sent to that output.

        Returns:

            A dict containing the following fields:

                board (str or tuple/list): the target board, as specified in the `board` parameter

                output (str): output to which the noise injection electronics
                    is connected, as specified by the `output` parameter.

                output_source (str): name of the source driving the specified
                    'output'.

                pwm_offset (int): the PWM generator waveform offset from frame 0, in frames

                pwm_high_time (int): the high time of the PWM generator waveform, in frames

                pwm_period (int): the period of the PWM generator waveform, in frames

                pwm_enabled (bool): True when the PWM generator is not in reset. Useful when output_source is 'pwm'.

                user_bit0 (int): State of the user bit 0. Useful when output_source is 'user_bit0'.

                user_bit1 (int): State of the user bit 1. Useful when output_source is 'user_bit1'.


            If `board` evaluates to False, only the first 3 fields are returned, with
            output_source set to None.

        """
        status = dict(board=board,
                      output=output,
                      output_source=None)
        if not board:
            return status

        mb = self.get_iceboard(board)
        output_source = mb.get_user_output_source(output)
        offset, high_time, period, reset = mb.get_pwm()
        user_bit0, user_bit1 = mb.get_user_bits()
        status.update(
            output_source=output_source,
            pwm_offset=offset,
            pwm_high_time=high_time,
            pwm_period=period,
            pwm_enabled=not reset,
            user_bit0=user_bit0,
            user_bit1=user_bit1)
        return status

    def get_stream_id_map(self):
        """ Return the stream_ids if every channel of the array, indexed by channel_id.

        Returns:
            dict if the format {channel_id:stream_id}, where channel_id is a (crate, slot, channel) tuple.
        """

        stream_id_map = {}
        for mb in self.ib:
            stream_id_map.update(mb.get_stream_id_map())

        if len(stream_id_map) != len(set(stream_id_map.values())):
            self.logger.warning(f'{self!r}: Stream IDs are not unique!')

        return stream_id_map