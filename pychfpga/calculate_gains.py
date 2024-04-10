#!/usr/bin/python

"""
calculate_gains.py: Digital gain computation engine.

Computes and sets ideal gain for 4bit gaussian noise.
"""
# import logging

# Standard library packages
import time
import traceback

# PyPy external packages

import numpy as np
# Private external packages

# from wtl.rest import RunSyncWrapper  # For testing
from wtl import log

# local imports
from . import raw_acq # this assumed that '..' has been put into the search path

class GainCalc(object):

    # States
    SET_GAINS = 'set_gains'  # call get_gains() and set the gains of the fpga to the specified values
    SEND_DATA = 'send_data'  # call process_data() with a new set of data that has the new gains
    DONE = 'done'
    NBINS = 1024

    def __init__(
            self,
            channel_ids,
            stream_ids,
            n_iterations=18,
            target_rms=1.5*np.sqrt(2),
            weight=0.2,
            initial_gains=[('*', (1.0, 22))]):
        """ Computes the frequency-dependent digital gains of the specified
            channels to bring the signals within the target RMS values across
            the band.

            GainCalc does not care about stream IDs.

        Parameters:

            channel_ids (list): list of channel_ids that identify the channels
                for which we wish to compute gains. These will be used as a
                key of the resulting gain result map, which is meant to be
                passed to set_gains().

            stream_ids (list): list of stream_ids of the channels
                for which we will to compute gains. Will be used by
                update_gains() to identify which gain entries to update in the
                buffer.

            initial_gains (list or dict): describes the initial gains to be used for the computation. In the format::

                [ (target, (glin, log)), ...]

                where target is a crte/board/channel tuple that can include wildcards
                glin is a scalar of a 1024-element vector
                glog is an integer

        """
        self.log = log.get_logger(self)
        self.channel_ids = channel_ids
        self.stream_ids = stream_ids

        # Compute a map that allow us to convert a stream id into an index in the buffer
        self.stream_id_map = {sid: i for i, sid in enumerate(self.stream_ids)}
        # self.stream_id_to_index_map = {sid:index for index, sid in enumerate(self.channel_ids)}
        self.nchan = len(self.channel_ids)
        # self.n_rms_samples = n_frames
        self.n_target_iterations = n_iterations

        self.weight = weight

        # for 4 bit number *sqrt2 since real and imag, check this
        self.target_rms = target_rms  # 2.83 is 1.5bits  1.5 is 0.6bits

        # Buffer in which we'll accumulate the incoming data
        self.temp_gains = np.zeros((self.nchan, self.NBINS), dtype=np.float32)  # temp buffer
        self.mask = np.zeros((self.nchan, self.NBINS), dtype=np.int8)  # We store  abs(x)**2

        self.gains = np.zeros((self.nchan, self.NBINS), dtype=np.float32)
        self.glin = np.zeros((self.nchan, self.NBINS), dtype=np.int16)
        self.glog = np.zeros((self.nchan), dtype=np.int8)

        # Set initial default gains of (glin, glog)
        # We will start converging towards the final value from there
        if isinstance(initial_gains, dict):
            initial_gains = list(initial_gains.items())

        self.initial_gains = initial_gains
        for ix, cid in enumerate(self.channel_ids):
            for target, (glin, glog) in initial_gains:
                if all(((target[i] == '*') or target[i] == cid[i]) for i in range(len(target))):
                    self.glin[ix] = glin
                    self.glog[ix] = glog
                    self.gains[ix] = np.array(glin) * 2.0**glog
                    self.temp_gains[ix] = np.array(glin) * 2.0**glog

        # self.state = self.SET_GAINS

        # useful constants
        # channels = range(16)

        # RMS averaging
        # keep track of the iteration number
        self.frame_count = np.zeros((self.nchan), dtype=np.int8)
        self.iteration_number = np.zeros((self.nchan), dtype=np.int8)
        self.done = np.zeros((self.nchan), dtype=bool)

    def __repr__(self):
        return f'{self.__class__.__name__}()'

    def get_gains(self, ix=None):
        """ Return the gains from the current iteration in a format compatible with the FPGAArray.set_gains().

        Parameters:

            ix (ndarray or None): indices of the gains to be returned. If None, all gains are returned.

        Returns:

        """
        if ix is None:
            ix = np.arange(self.nchan)
        # return self.channel_ids, self.glin.astype(np.int16), self.glog.astype(np.int8)
        # return [(ch, (glin,glog)) for ch, glin, glog in zip(*g.get_gains())]

        return {tuple(self.channel_ids[i]): (self.glin[i].astype(np.int16), self.glog[i].astype(np.int8)) for i in ix}

    def get_filtered_gains(self, ix=None):
        """ Compute and return a filtered version of the gains along with a RFI mask.

        Parameters:

            ix (ndarray or None): indices of the gains to be returned. If
                None, all completed gains are returned.

        Returns:

            (gains, mask), there `gains` is the channel-id-indexed gain table
                for ALL freqencies of selected channel indices, and `mask`
                indicates whether each of the frequancy is flagged as RFI.
        """
        if ix is None:
            ix = np.arange(self.nchan)

        # Select the only the gains that are done
        ix_done = ix[self.done[ix]]

        # Get a filtered version of the linear gains as a Masked Array, with RFI spikes masked.
        filtered_masked_glin = self.filter_gains(self.glin[ix_done])

        gains = {tuple(self.channel_ids[j]): (filtered_masked_glin[i].astype(np.int16), self.glog[j].astype(np.int8))
                 for i, j in enumerate(ix_done)}
        mask = filtered_masked_glin.mask
        return gains, mask

    def update_gains(self, stream_ids, rms):
        """ Process incoming data, and return gains that need to be applied to the pipleine as the algorithm converges.


        Parameters:

            ix (ndarray): list of channel indices that correspond to each row of the fft data.

            rms (ndarray, dtype=float32): array of RMS values meaured at the output of the scaler .

        Returns:

            (ids, glin, glog, done): Gains (glin, glog) that need to be
            set on the specified ids before new data is sent form those
            channels. `done` is a vector that indicates if the stream_id gain
            computation is complete, in which case the specified gain is the
            final solution.

        """
        try:

            ######################
            # Phase 1: iteratively converge the gain until we reach the target RMS
            ######################
            # Accumulate square of FFT values

            # get the buffer index of the provided ids
            t1 = time.time()

            # Find the input index of valid stream_ids
            ix = np.array([i for i, sid in enumerate(stream_ids) if sid in self.stream_id_map], dtype=np.int16)

            # find the buffer index of the channels with the specified stream IDs
            bix = np.array([self.stream_id_map[sid] for sid in stream_ids[ix]], dtype=np.int16)

            # remove channels that are already completed
            ix_done = ~self.done[bix]  # ~ is equivalent to logical not only if the array is bool

            ix = ix[ix_done]
            bix = bix[ix_done]

            # Scale the current gain to the value that would get us the target RMS
            # new_gain = ideal_rms / (data / current_gain)

            # Compute current linear gain from glin/glog
            # print 'CG: Gain Iteration', self.iteration_number[ix]
            # print 'CG: RMS is ', np.median(rms[ix, 1:], axis=-1)
            # N=np.array([0,13,313,513])
            N = 6
            self.log.info(f'{self!r}: Received RMS data from {rms.shape[0]} channels. Processing {bix.size} of those.')
            self.log.debug(f'{self!r}: Got Stream IDs. First {N} are: {stream_ids[ix[:N]]}')
            self.log.debug(f'CG: Median Actual/target RMS ratio are {np.median(rms[ix[:N], 1:] / self.target_rms, axis=-1)}')
            self.log.debug(f'CG: Median RMS are {np.median(rms[ix[:N], 1:], axis=-1)}')
            # Compute new gain base don the ratio of the acrual rms vs target rms
            # We want to slowly ease into that gain to avoid being affected too much by transients,
            # so just take 20% of thhat target and 80% of the old gain
            # self.temp_gains[ix][...] = (20.0 * target_gains + 80.0 * self.temp_gains[ix]) / 100.0
            # self.temp_gains[ix][...] = 0.2 * target_gains + 0.8 * self.temp_gains[ix]
            # self.temp_gains[ix][...] = 0.2 * target_gains + 0.8 * self.temp_gains[ix]
            a = self.weight
            gmax = 4.0
            self.log.debug(f'{self!r}: temp gains.shape={self.temp_gains[bix].shape}')
            self.log.debug(f'{self!r}: rms.shape={rms[ix].shape}')

            # Compute new gains. Equivalent to idealRMS*glin*(2**(glog-4))/outrms (?)
            self.temp_gains[bix] = np.clip(
                self.temp_gains[bix] * np.clip(
                    (1-a) + a*self.target_rms / rms[ix], 1/gmax, gmax),
                1, 2**(31+16))  # g[j].shape=(1024)

            # Debugging code: prints rms and gain for selected stream ID.
            S = 16*10 + 0
            if S in stream_ids[ix]:
                x = np.where(stream_ids[ix] == S)[0][0]
                bx = self.stream_id_map[S]
                # print 'CG: Stream 0 Median Actual/target RMS ratio is ', rms[ix[0]] / self.target_rms
                median_rms_info = ',  '.join('%7.3f' % rms[x, i] for i in range(10))
                gain_info = ',  '.join('%7.3e' % self.temp_gains[bx, i] for i in range(10))
                self.log.debug(f'{self!r}: Stream 0 Median RMS is {median_rms_info}')
                self.log.debug(f'{self!r}: Stream 0 Gain is {gain_info}')

            # print 'CG: new_gain is ', self.temp_gains[ix]

            # Convert linear gain into (glin, glog) values
            # glin.shape=(16,1024), glog.shape=(16)
            self.glin[bix], self.glog[bix] = self.calc_gains(self.temp_gains[bix])

            # #####################################
            # Keep track of how many iteration we have done
            # #####################################

            # increment iteration counter for all processed channels
            self.iteration_number[bix] += 1

            # Identifies which channels reached the target RMS, and return the corresponding gains
            # Here, we just stop when we reached a fixed iteration number
            bix_done = bix[self.iteration_number[bix] == self.n_target_iterations]
            self.done[bix_done] = True
            # print 'Done indices:', ix_done
            # print self.iteration_number[ix]
            # print 'Gain is glin=%i, glog=%i, g=%f' % (self.glin[ix[0]][0], self.glog[ix[0]], self.glin[ix[0]][0] * 2**self.glog[ix[0]])
            t2 = time.time()
            self.log.info(f'{self!r}: Gain updating time: {(t2 - t1) * 1000:0.3f} ms for {bix.size} channels')

            return self.get_gains(bix)

        except Exception as e:
            self.log.error(f'{self!r}: Exception:\n{e!r}')
            traceback.print_exc()
            raise

    def is_done(self):
        return all(self.done)

    def percent_done(self):
        """ Returns the number of channels that have reached the target number of gain calculation iterations.

        Returns:

            float from 0 to 100.

        """
        return float(sum(self.done)) / self.done.size * 100.0

    def calc_gains(self, g, target_glin=2**13):
        """ Convert an array of linear gain into a (glin, glog) gain format.

        Parameters:

            g:  is a linear global complex gain array, with the last dimension corresponding to the frequency bin axis.

        Returns:

            (glin, glog):

                - `glin` is an array with the same shape than `g`, containing
                  complex gains that are around 2**13.

                - `glog` is an array with one less dimension than 'g', and
                  contain a power-of-two scaling factor that is needed to
                  express the target gain `g` such as g = glin* 2**glog.


        The maximum ``glin`` positive gain values is (2**15 - 1) (int16). We
        normalize the gain to aim for a median gain of 2**13 so we have the
        maximum resolution (13 bits) in the gain value but still keep a dynamic
        range headroom of about 4.


        #2**14 is max for linear gain
        #ignore dc component
        #check for nans

        benchmark:
            2019-03-29: 776 ms on TP520 with gains(2048, 1024). original version

        """
        # print g

        # Eliminate gains that would be too high from the computations by creating
        # a masked array
        bad_values = (g > 2**(31+16)) | ~ np.isfinite(g)
        # ignore bin 0, which has a DC components that is way larger than the signal in other bins
        bad_values[:, 0] = True
        g = np.ma.array(g, mask=bad_values)

        # ############
        # Compute glog
        # ############
        # np.abs(g) / 2**13 is the postscaler gain that needs to be applied to have ``glin`` be 2**13
        #
        # we take the median of that postscaler across all frequencies (the last
        # dimension of `g`) to be less sensitive to outliers, and take the log2 of
        # it, which is rounded up so we keep our headroom of at least 4.
        #
        # glog has one less dimension than `g`.
        glog = np.clip((np.ceil(np.log2(np.ma.median(np.abs(g) / target_glin, axis=-1)))).astype(np.int32), 0, 31)
        # ma.median will result in a masked value if all elements are masked. In
        # these cases, give to glog the the median glog from all channels
        # (hopefully there is at lease one good glog) .
        #
        # glog will also be masked if log2 is invalid (zero or negative gain)
        glog[glog.mask] = np.ma.median(glog)

        # ############
        # Compute glin
        # ############

        glin = g / (2.**glog[..., None])  # glog is broadcasted along the last dimension of g.
        glin[bad_values] = 2**14  # Set a high gain the saturated gains (should probably be 2**15-1)
        # saturate gains that are getting too close to the maximum range
        glin[glin > 2**14] = 2**14
        # truncate to integer, and convert to complex (necessary?)
        # np.floor(glin, out=glin)
        # glin = glin.astype(np.int).astype(np.complex)

        return glin, glog

    def filter_gains(self, signal, filter_type='hybrid', num_components=50, zero=False):
        """ Create a filtered version of a signal that excludes spikes.

        Parameters:

            signal (ndarray): signal to filter across the last dimension.

            filter_type (str): type of filtering.

                - 'fourier' : Applies low pass filter, with a bandpass
                  frequency of `num_components` frequency samples. No sample is masked.

                - 'poly' : Use an iteratively higher order polynomial fit to
                  mask outliers and generate a smoothed version of the signal.
                  masked values are replaced by the original signal samples.

                - 'hybrid': Applies the 'poly' filtr to mask RFI samples, but
                  return 'fourier'-filter data using that mask.

            num_components (int): Bandpass of the Fourier low pass filter,
                expressed in number of frequency samples

            zero: if True, masked values identified by the 'poly' filter are zeroed out.

        Returns:
            np.MaskedArray

        """
        signal = np.array(signal)
        # mask = np.ma.make_mask_none((len(signal),))
        # The first bin is always bad for some reason
        # mask[0] = True
        # self.masked = np.ma.array(np.log(signal), mask=mask)

        if filter_type == 'fourier':
            filtered_mask_signal = np.ma.array(self.fourier_filter(signal, num_components))

        elif filter_type == 'poly' or filter_type == 'hybrid':
            filtered_mask_signal = self.iterative_poly_filter(signal)
            if filter_type == 'hybrid':
                # Take a copy of the signal and replacce the values that were masked due to RFI by interpolated values
                in_arr = signal.copy()
                in_arr[filtered_mask_signal.mask] = filtered_mask_signal[filtered_mask_signal.mask]
                # Apply fourir filter
                filtered_mask_signal.data[:] = self.fourier_filter(in_arr, num_components)
            if zero:
                filtered_mask_signal[filtered_mask_signal.mask] = 0
            else:
                filtered_mask_signal[filtered_mask_signal.mask] = signal[filtered_mask_signal.mask]
        else:
            raise ValueError
        filtered_mask_signal = (filtered_mask_signal.real).astype(np.int32).astype(np.complex64)
        return filtered_mask_signal

    def fourier_filter(self, signal, num_components):
        """ Apply an ideal low-pass filter in the Fourier domain across the last dimension.

        The signal is padded on each end with mirror of itself to improve edge behavior.


        Parameters:

            signal (ndarray): signal(s) to filter. The array can contain any
                dimensions. Filtering is done on each signal individually
                across the last dimension.

        Should extend to other windows.

        Not assured to maintain signal size
        """
        signal = np.array(signal)
        signal_length = signal.shape[-1]  # length of the last dimension
        # Pad. If we represent the signal by 0123, we build the array 21+0123+ 3
        padded_signal = np.concatenate((
            signal[..., signal_length//2: 0: -1],
            signal,
            signal[..., -1: -signal_length//2: -1]), axis=-1)
        f_signal = np.fft.fft(padded_signal, axis=-1)  # FFT across the last axis
        # We eliminate all high frequency beyond num_components
        f_signal[..., num_components: -num_components] = 0
        filtered = np.fft.ifft(f_signal, axis=-1)[..., signal_length // 2: -signal_length // 2 + 1]
        filtered = (filtered.real).astype(np.int32).astype(np.complex64)
        return filtered

    def mask_rfi(self, signal, filtered_signal, threshold):
        """
        Set the mask flag of the element of `signal` that deviate from
        `filtered_signal` by a factor that exceeds `threshold`.

        Parameters:

            signal (numpy.MaskedArray): Signal to mask

            filtered_signal (ndarray): Smoothed out version of signal.

            threshold (float): threshold for flagging the data points as bad

        Returns:

             None, but the mask of the masked array `signal` is modified in-place.
        """
        signal.mask |= abs(signal) < abs(filtered_signal / threshold)

    def poly_filter(self, signal, threshold, degree):
        """ Filters signal using a polynomial fit across the last dimension of
        the array, ignoring masked values, AND masks in-place the values of
        `signal` that deviate too much from its filtered version

        Parameters:

            signal (MaskedArray): 2D masked array. Fit is perforemed across the last dimension

            threshold (float): Threshold used to flag `signal` data

            degree (int): degree of the polynomial fit


        Returns:

            (ndarray): filtered signal. `signal` mask is modified in-place.
        """

        x = np.arange(signal.shape[-1])
        filtered_signal = np.empty(signal.shape)

        # Unfortunately, ma.polyfit() does not treat the masks of each
        # seriesin a 2D array individually. It somehow combines them, which is
        # useless to us. So we need to process the data line by line.
        for i in range(signal.shape[0]):
            # Compute fit coefficients. ma.polyfit does not use masked data
            # points in signal to compute the polynomial coefficients
            fit_coeff = np.ma.polyfit(x, signal[i], degree)
            filtered_signal[i, :] = np.poly1d(fit_coeff)(x)
        self.mask_rfi(signal, filtered_signal, threshold)
        return filtered_signal

    def iterative_poly_filter(self, signal):
        """ Compute a filtered version `signal` using iteratively more refined
        polynomial fits and also return a array that indicate which of
        'signal' data points deviate too much from its filtered verison.


        A polynomial fit of increasing order is iteratively fitted to the
        signal. At each iteration, part of the signal that exceed the fit by a
        threshold factor are masked in `signal`. More and more of `signal`
        gets flagged by each iteration. The final polynomial fitted signal and
        the final mask are then returned.

        Parameters:

            signal (ndarray): 2D array of signals to process. Filtering is done across the second dimension.

        Returns:

            (filtered_signal, mask) tuple, where:

                - filtered_signal  (ndarray): filtered version of `signal`. The array has the same dimension as `signal`

                - mask (ndarray) : array of booleans indicating whether
                  `signal` samples have deviated too much from the filtered
                  version during the iterative filtering process.
        """
        # The first bin is always bad for some reason
        degree = 1
        threshold = 1.2
        # Create a copy of the signal as a masked array with nothing initially
        # masked. Masks will be addes gradually as we iterate. We wil work on the log of the signal.
        masked_signal = np.ma.array(np.log(signal), mask=False)
        masked_signal.mask[..., 0] = True  # the first bin is always bad because of DC component in bin 0
        # Pre-allocate storage for the poly-filtered results to save time
        filtered_signal = np.empty(signal.shape)

        # Fit with radually higher order polynomial and mask RFI with gradually lower thresholds
        while threshold > 1.01:
            # Compute filtered signal. This masked_signal mask is modified to mask RFI
            filtered_signal[...] = self.poly_filter(masked_signal, threshold, degree)
            threshold = 1 + (threshold - 1) * 0.8
            if degree < 15:
                degree += 2
        np.exp(filtered_signal, out=filtered_signal)
        np.floor(filtered_signal, out=filtered_signal)
        # filtered_signal = (filtered.real).astype(np.int).astype(np.complex)
        return np.ma.array(filtered_signal, mask=masked_signal.mask)


def compute_gains(ca, number_of_averages=100, ch=3):
    """
    Stand-alone compute_gains function for testing the gain calculation algorithm.

    It starts data capture on all boards of the array, instantiate an raw_acq
    receiver and a gain computation object, and iterate the gain calculation
    process for the specified number of times.

    The same process is implemented in the fpga_master framework.

    Parameters:

        ca (FPGAArray): A FPGAArray object containing the boards on which we
            want to compute digital gains. We operate only on the first board of the array.

        number_of_averages (int): Number of FFT averages that are captured by the
            receiver for each iteration

        ch (int): stream ID index on which we want to compute the gain (debug)

    """
    async def run():
        ca.set_sync_method('local_soft_trigger')

        ca.set_operational_mode('shuffle16', frames_per_packet=1)
        ca.ib.start_data_capture(period=.004, source='scaler')

        stream_id_map = ca.get_stream_id_map()
        channel_ids = list(stream_id_map.keys())
        stream_ids = list(stream_id_map.values())
        bank = 0

        port_map = [dict(
            port=ca.ib[0].get_data_socket().getsockname()[1],
            sources=[(ca.ib[0].hostname, 80)])
            ]
        r = raw_acq.RawAcqReceiver()
        g = GainCalc(channel_ids=channel_ids, n_iterations=20)
        g.rms = np.empty((g.n_rms_iterations, 1024))
        g.gain = np.empty((g.n_rms_iterations, 1024))
        # Set all gains to their initial values
        await ca.set_gains_async(gains=g.get_gains(), bank=bank, when='now')
        i = 0
        try:
            await r.start_async(ports=port_map, stream_ids=stream_ids, start_thread=True)

            while not g.is_done():
                print('.')
                ix, rms = r.get_fft_rms(stream_ids=stream_ids, target_gain_bank=bank, number_of_frames=number_of_averages)
                g.rms[i, :] = rms[ch]
                g.gain[i, :] = g.glin[ch] * 2.**g.glog[ch]
                i += 1
                new_gains = g.update_gains(ix, rms)
                # bank ^= 1 # switch bank
                await ca.set_gains_async(gains=new_gains, bank=bank, when='now')
            # Set the final gains
            filtered_gains, mask = g.get_filtered_gains()
            await ca.set_gains_async(gains=filtered_gains, bank=0, when='now')
        except Exception:
            raise
        finally:
            r.stop()
        return g, filtered_gains, mask
    g, filtered_gains, mask = asyncio.run(run())
