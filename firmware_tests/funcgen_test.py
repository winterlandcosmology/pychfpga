
import logging
import numpy as np
import pytest
import socket
import psutil

from .utils import ping_sources_async, gen_data
from functools import wraps 
import matplotlib.pyplot as plt
from pathlib import Path
import math
import time
from time import sleep
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
from pychfpga.fpga_array import FPGAArray
import sys
from .module_test import TestModule

pytestmark = pytest.mark.funcgen


#1. Separate config files for scaler and funcgen (conftest)
#2. Import pytest_generate_tests, board_conn,  to be used by both scaler and funcgen
#3. overflow buffer can be programmed from set_data_source
#4. noise seeds can be set in set_data_source

class TestFuncgen(TestModule):
    """
    1. When pytest is run on the command line, the pytest.ini file provides the config file as a command-line argument
    2. In a session start hook, xr parses this config file and converts it to a namespace in xr.config
    3. The pytest-generate-tests hook is called on each test (with a new instance of TestFuncgen each time)
        a. The xr object is extracted from the metafunc passed to this function, from which the config data can be accessed
        b. the config data is used to connect to and then cache the board, from which various constants and parameters are accessed and stored
        c. Any candidate fixtures in the metafunc are paramatrised according to an arbitrary user-provided python expression in the config file,
        optionally using the constants set in the previous board.
        All fixtures which are present in the argument list of the function as well as in the config file (either as global or test-specific parameters) are parameterized.
    4. Before each test, the function-level setup fixture is called. Via the xr fixture, it gains access to the xr object and thus all the parameters contained therein. 
    It then either connects to the board or extracts the cached board object from the xr object, as needed.
    Finally, it transfers important parameters, including the board object, from xr to the namespace of self. 
    5. The test is run.

    TODO: 1. Import pytest_generate_tests into .utils so that both funcgen, scaler and future test modules use the same hook.
          2. Separate config files for each module if config.yaml becomes too convoluted.  
    """
    def get_constants(_, board):
        constants = NameSpace()
        constants.SAMPLES_PER_FRAME = board.ADC_SAMPLES_PER_FRAME
        constants.ADC_BITS_PER_SAMPLE = board.ADC_BITS_PER_SAMPLE
        constants.NUM_ADC_VALUES = 1 << constants.ADC_BITS_PER_SAMPLE
        constants.NUM_CHANNELS = board.NUMBER_OF_CHANNELIZERS
        constants.PLATFORM = board.mb.part_number #indicates if board is "CRS", "ZCU111" or "MGK7MB" (IceBoard)
        constants.BURST_PERIOD = 10_000 #frames between bursts
        # Should throw an exception when ADC_BYTES_PER_FRAME not 1 or 2 but the connection does it itself
        if board.ADC_BYTES_PER_SAMPLE == 1:
            constants.FG_DTYPE = 'u1'
            constants.FG_DTYPE_SIGNED = 'i1'
        else:
            constants.FG_DTYPE = 'u2'
            constants.FG_DTYPE_SIGNED = 'i2'
        return constants
    
    @pytest.fixture(scope='function', autouse=True)
    async def setup(self, xr, request):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = await self.board_conn(xr, request)
        
        self.seeds = self.test_config["seeds"]
        self.loglevelrecv = xr.config.test_config.loglevelrecv

        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.logleveltest)

        
    def get_logger(self):
        return self.logger

    def plot_and_test(self, xr, data, expected_data, title, xlabel="Number of samples", ylabel="Sample value", test=True):
        if data.shape != expected_data.shape:
            raise ValueError(f"data and expected_data have different shapes. {data.shape=} and {expected_data.shape=}")
        print(f'Plotting {title}')
        plt.figure()
        plt.plot(expected_data, marker=".", linestyle='', label='expected')
        plt.plot(data, label='measured')
        plt.xlabel(xlabel)
        plt.ylabel(ylabel)
        plt.title(title)
        plt.legend()
        xr.insert_plot()
        plt.close()
        if test:
            np.testing.assert_array_equal(data, expected_data)

    # def test_udp_buffers_size(self):
    #     pytest.skip() #for now
    #     logger = self.get_logger()
    #     buff_config = list()
    #     logger.debug("Reading system files in /proc/sys/net")
    #     with open("/proc/sys/net/core/rmem_max") as file:
    #         buff_config.append(int(file.readline()))
    #     with open("/proc/sys/net/core/rmem_default") as file:
    #         buff_config.append(int(file.readline()))
    #     with open("/proc/sys/net/ipv4/udp_mem") as file:
    #         for val in file.readline().split():
    #             buff_config.append(int(val))
    #     with open("/proc/sys/net/ipv4/udp_rmem_min") as file:
    #         buff_config.append(int(file.readline()))

    #     if any(val < 26214400 for val in buff_config):
    #         raise RuntimeError(
    #             "The UDP buffers' memory parameters (net.core.rmem_max, net.core.rmem_default, "
    #             "net.ipv4.udp_mem, net.ipv4.udp_rmem_min) are too low. Increase them to at least 26214400 "
    #             "bytes. \nYou can do it by running:\n"
    #             ">>> sudo sysctl -w net.core.rmem_max=26214400 net.core.rmem_default=26214400 "
    #             "net.ipv4.udp_mem='26214400 26214400 26214400' net.ipv4.udp_rmem_min=26214400"
    #         )

    # @pytest.mark.asyncio
    # async def test_mtu_size(self, xr):
    #     pytest.skip() #for now
    #     logger = self.get_logger()
    #     logger.debug("Getting the board's address")
    #     ping_port = self.board.mb.port
    #     port_map = dict(
    #         port=self.board.get_data_socket().getsockname()[1],
    #         sources=[(self.board.hostname, ping_port)]
    #     )
    #     logger.debug(f"The board located at {port_map['sources'][0][0]}:{ping_port}")
    #     logger.debug("Pinging the board to determine interface used")
    #     src_if_addrs = await ping_sources_async([port_map])
    #     if_ip = list(src_if_addrs.values())[0]
    #     if if_ip is None:
    #         raise RuntimeError(
    #             f"Could not establish a TCP connection with {self.board.hostname}:{ping_port} "
    #             f"to determine the local network interface. Check network connectivity."
    #         )
    #     logger.debug(f"Interface address: {if_ip[0]}:{if_ip[1]}")
    #     if_addrs = psutil.net_if_addrs()
    #     if_name = None
    #     for key in if_addrs.keys():
    #         for snic_addr in if_addrs[key]:
    #             if snic_addr[0] == socket.AF_INET:
    #                 if snic_addr[1] == if_ip[0]:
    #                     if_name = key
    #     logger.debug(f"Interface name: {if_name}")
    #     if_stats = psutil.net_if_stats()
    #     mtu = if_stats[if_name].mtu
    #     logger.debug(f"Interface MTU size: {mtu}")

    #     if mtu < 9000:
    #         raise RuntimeError(f"Default network interface has MTU={mtu} (<9000). Please set MTU to 9000 to "
    #                            f"prevent loss of packets.")

    async def test_packets_number(self, xr):
        """
        Ensures that all packets sent by the motherboard are received.
        """
        logger = self.get_logger()
        logger.debug("Starting data capture for one period")
        self.board.start_data_capture(period=1)
        receiver = await self.get_data_receiver()
        logger.debug("Reading raw frames")
        ts, data, count = receiver.read_raw_frames(1, source=0)
        logger.info(f"Expected {len(count[0])} packets, received {np.sum(count[0])}.")
        assert np.all(count[0]), (f"Missing packets from ICE board. Received {count[0]} packets (per channel). "
                               f"Check connection and system configuration.")

    async def _set_capture_funcgen(self, source: str, period: float = 1, frames_per_burst: int = 2, mode = 0, number_of_bursts: int = 1,
                             burst_period_in_frames=None, keep_all_frames = False, **func_kwargs):
        """
        Set funcgen to specified data output and capture outcoming data within specified period.

        if keep_all_frames is False, only the last frame is returned
        """
        logger = self.get_logger()
        logger.debug("Starting data capture from FUNCGEN")
        self.board.start_data_capture(period=period, source='funcgen', frames_per_burst=frames_per_burst, mode=mode, burst_period_in_frames=burst_period_in_frames)
        logger.debug(f"Setting data source to {source.upper()}")
        self.board.set_channelizer(data_source=source, fft_bypass=1, scaler_bypass=1, **func_kwargs)
        logger.debug("Initializing data receiver")
        receiver = await self.get_data_receiver(
            verbose=self.loglevelrecv,
        )
        receiver.flush()
        self.board.sync()

        # After flush/sync the first burst can already be in flight, so early
        # packets are discarded and the assembled first frame is zero-padded.
        # Capture one extra burst and drop the first when every frame is needed.
        ncap = number_of_bursts + 1 if keep_all_frames else number_of_bursts
        _, data, _ = receiver.read_raw_frames(ncap=ncap, split=True, source=0)
        logger.debug(f"Received {len(data)} packets from FUNCGEN")
        if keep_all_frames:
            frames_per_cap = data.shape[1] // ncap
            return data[:, frames_per_cap:]
        else:
            return data[:, -1]

    @pytest.mark.funcgen_patterns
    async def test_funcgen_ramp(self, xr):
        '''
        Tests function generation with simple ramp over all possible values.
        '''
        expected_data = (np.arange(self.data.SAMPLES_PER_FRAME) - self.data.NUM_ADC_VALUES / 2) % self.data.NUM_ADC_VALUES - self.data.NUM_ADC_VALUES / 2
        data = (await self._set_capture_funcgen('ramp'))[0]
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen ramp",
            )

    @pytest.mark.funcgen_patterns
    async def test_funcgen_sin(self, xr):
        '''
        Tests function generation with sine wave with amplitude 127 and frequency 1.
        '''
        sin_freq = 1
        expected_data = (np.sin(np.arange(self.data.SAMPLES_PER_FRAME) * 2 * np.pi / self.data.SAMPLES_PER_FRAME * sin_freq) * 127).astype(self.data.FG_DTYPE_SIGNED)
        data = (await self._set_capture_funcgen('sin', freq=sin_freq, ampl=127))[0]
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen sin",
            )

    @pytest.mark.funcgen_patterns
    async def test_funcgen_arb(self, xr):
        '''
        Tests arb function generation mode with constant data equal to 1.
        '''
        expected_data = np.ones(self.data.SAMPLES_PER_FRAME)
        data = (await self._set_capture_funcgen('arb', data=expected_data))[0]
        logger = self.get_logger()
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen arb",
            )

    @pytest.mark.funcgen_patterns
    async def test_funcgen_const(self, xr):
        a = 13
        expected_data = np.full(self.data.SAMPLES_PER_FRAME, a, self.data.FG_DTYPE).astype(self.data.FG_DTYPE_SIGNED)
        data = (await self._set_capture_funcgen('a', a=a))[0]
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen const",
            )

    @pytest.mark.funcgen_patterns
    async def test_funcgen_ab(self, xr):
        a = 0 
        b = 42
        expected_data = np.tile(np.array((a, b), self.data.FG_DTYPE), self.data.SAMPLES_PER_FRAME // 2).astype(self.data.FG_DTYPE_SIGNED)
        data = (await self._set_capture_funcgen('ab', a=a, b=b))[0]
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen ab",
            )

    @pytest.mark.funcgen_patterns
    async def test_funcgen_real_ramp(self, xr):
        '''
        Tests function generation with ramp from 0 to 255 only in real components.
        '''
        expected_data = np.ravel([(i,0) for i in range(self.data.SAMPLES_PER_FRAME//2)]).astype(self.data.FG_DTYPE_SIGNED)
        data = (await self._set_capture_funcgen('real_ramp'))[0] # First burst, first channel
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen real ramp",
            )

    @pytest.mark.funcgen_noise_gen
    async def test_funcgen_deterministic(self, xr):
        '''
        Tests that channels programmed with the same noise seed match, and that
        different seeds produce different streams.
        '''
        seed0 = np.random.randint(2 ** 16 - 1)  # seed is 16-bit value
        seed1 = np.random.randint(2 ** 16 - 1)
        while seed1 == seed0:
            seed1 = np.random.randint(2 ** 16 - 1)
        half_chan = self.data.NUM_CHANNELS // 2
        self.board.start_data_capture(period=1, source='funcgen', frames_per_burst=2, mode=0)
        self.board.set_channelizer(data_source='noise', seed=seed0, channels=range(half_chan))
        self.board.set_channelizer(data_source='noise', seed=seed1, channels=range(half_chan, 2 * half_chan))
        receiver = await self.get_data_receiver(verbose=self.loglevelrecv)
        receiver.flush()
        self.board.chan[0].FUNCGEN.RST_LFSR = 1
        self.board.chan[0].FUNCGEN.RST_LFSR = 0
        self.board.sync()
        _, data, _ = receiver.read_raw_frames(split=True, source=0)
        assert data.shape[0] == self.data.NUM_CHANNELS, (
            f"Expected {self.data.NUM_CHANNELS} channels, got shape {data.shape}"
        )
        # Channels in the same half share a seed; the two halves use different seeds.
        plt.plot(data[half_chan, -1] - data[0, -1])
        plt.title(f'Difference between funcgen noise with seeds {seed0} and {seed1}')
        plt.xlabel('Sample index')
        plt.ylabel('Difference')
        xr.insert_plot()
        plt.close()
        assert np.all(data[:half_chan, -1] == data[0, -1])
        assert np.all(data[half_chan:, -1] == data[half_chan, -1])
        assert not np.all(data[0, -1] == data[half_chan, -1])

    # @pytest.mark.funcgen_noise_gen
    # def test_lfsr_output(self, xr):
    #     """Outputs lfsr noise binary values into a txt file. Can be used for further statistical tests"""
    #     seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value
    #     data = await self._set_capture_funcgen('noise',seed = seed).astype(self.data.FG_DTYPE_SIGNED)
    #     processed_data = self.noise_data_process(data, self.board.ADC_BITS_PER_SAMPLE)
    #     processed_data = np.unpackbits(processed_data)
    #     print(f"Binary values of the random noise generated are saved at {Path('../../fw_test_results/test_results_assets')}")
    #     np.savetxt(Path('../../fw_test_results/random.txt'), processed_data, fmt='%.1d', delimiter=' ', newline='')

    def _monobit_bit_sum(self, data: np.ndarray) -> int:
        """Sum of (+1 for each 1-bit, -1 for each 0-bit) over ADC_BITS_PER_SAMPLE low bits per sample."""
        nbits = self.data.ADC_BITS_PER_SAMPLE
        mask = (1 << nbits) - 1
        samples = data.ravel().astype(np.int64) & mask

        if nbits <= 8:
            bits = np.unpackbits(samples.astype(np.uint8), bitorder="little").reshape(-1, 8)[:, :nbits]
        else:
            u16 = samples.astype(np.uint16)
            bytes_le = u16.view(np.uint8).reshape(-1, 2) #groups of two bytes
            bits = np.unpackbits(bytes_le, bitorder="little").reshape(-1, 16)[:, :nbits]

        return int(2 * int(bits.sum()) - bits.size)

    @pytest.mark.funcgen_noise_gen
    async def test_funcgen_random_pval(self, xr, number_of_bursts):
        '''
        Tests noise generator with the monobit test (from NIST's Statistical Test Suite for Random and Pseudorandom Number Generators)
        Counts the ratio of generated 1 bits to generated 0 bits in a long run and generates the p-value corresponding to the probability this would arise randomly
        Ensures the number of p-values below a certain threshold is small.

        Parameters:
            number_of_bursts (int): Number of 4-frame bursts to capture as part of a run
        '''
        p_val_threshold = 0.01
        number_runs = len(self.seeds)
        p_vals= np.zeros(number_runs)
        logger = self.get_logger()
        for i in range(number_runs):
            print(f"Random Pval test run {i} / {number_runs}")
            # Returns data in shape (nchannels, ncap*nframes_per_cap, nsamples)
            #self.board.set_funcgen(function='noise', seed = self.seeds[i])
            data = (await self._set_capture_funcgen('noise',
                                            period=None,
                                            frames_per_burst=4,
                                            mode=1, #4 frames on both ICE and CRS
                                            number_of_bursts=number_of_bursts,
                                            burst_period_in_frames=self.data.BURST_PERIOD,
                                            keep_all_frames=True,
                                            seed=self.seeds[i]
                                            ))[0].astype(self.data.FG_DTYPE_SIGNED)
            bit_sum = self._monobit_bit_sum(data)
            #print(bit_sum)
            sn = abs(bit_sum) / math.sqrt(data.size * self.data.ADC_BITS_PER_SAMPLE)
            p_val = math.erfc(sn/np.sqrt(2))
            p_vals[i] = p_val
        plt.hist(p_vals, bins=5, range=(0, 1))
        plt.ylabel("Number of runs")
        plt.xlabel("p-value")
        plt.title(f"Histogram of p-values with {number_of_bursts} bursts")
        xr.insert_plot()
        plt.close()
        success_proportion = np.sum(p_vals >= p_val_threshold) / len(self.seeds)
        print(f"Number of good p-values (greater than 0.01): {np.sum(p_vals >= p_val_threshold)}")
        print(f"Percentage of good p-values: {success_proportion * 100}%")
        std_dev = math.sqrt(p_val_threshold * (1 - p_val_threshold) / len(self.seeds))
        assert (1 - p_val_threshold - 3 * std_dev) <= success_proportion <= (1 - p_val_threshold + 3 * std_dev)

    def sim_random_gen(self, number_of_frames):
        """Python simulation of noise generator"""
        num_samples = number_of_frames * self.data.SAMPLES_PER_FRAME
        data = np.zeros(num_samples)
        for i in range(len(data)):
            data[i] = np.random.randint(-(2**(self.data.ADC_BITS_PER_SAMPLE - 1)), 2**(self.data.ADC_BITS_PER_SAMPLE - 1) -1)
        return data
        
    
    @pytest.mark.funcgen_noise_gen
    async def test_funcgen_noise_histogram(self, xr, number_of_bursts):
        '''
        Plots a histogram indicating, for each number, how many values appear this many times in both the noise generator stream and a numpy pseudorandom stream.

         Parameters:
            number_of_bursts (int): Number of 4-frame bursts to capture as part of a run
        
        Notes:
            As of yet does not actually perform a test and thus cannot fail; this test is solely useful for naked-eye inspection of results.
        '''
        seed = np.random.randint(0, 2 ** 16 - 1) #seed is 16-bit value

        data = (await self._set_capture_funcgen('noise',
                                        period=None,
                                        frames_per_burst = 4,
                                        mode=1,
                                        keep_all_frames=True,
                                        number_of_bursts = number_of_bursts,
                                        burst_period_in_frames = self.data.BURST_PERIOD,
                                        seed = seed))[0].astype(self.data.FG_DTYPE_SIGNED)

        print(data.shape, data.size)
        sim_data = self.sim_random_gen(4 * number_of_bursts) #same for CRS and ICE, see above

        _, counts = np.unique(data.ravel(), return_counts=True)
        _, sim_counts = np.unique(sim_data.ravel(), return_counts=True)

        min_count = min(counts.min(), sim_counts.min())
        max_count = max(counts.max(), sim_counts.max())
        bin_edges = np.arange(min_count, max_count + 2) - 0.5
        bins, _ = np.histogram(counts, bins=bin_edges)
        sim_bins, _ = np.histogram(sim_counts, bins=bin_edges)
        xlim = (bin_edges[0], bin_edges[-1])
        centers = np.arange(min_count, max_count + 1)

        plt.subplot(2, 2, 1)
        plt.hist(counts, bins=bin_edges)
        plt.xlim(xlim)
        plt.xlabel("Counts in stream")
        plt.ylabel("Number of values")
        plt.title(f"Count distribution with LFSR")

        plt.subplot(2, 2, 2)
        plt.hist(sim_counts, bins=bin_edges)
        plt.xlim(xlim)
        plt.xlabel("Counts in stream")
        plt.ylabel("Number of values")
        plt.title(f"Count distribution with numpy random")

        plt.subplot(2, 2, (3, 4))
        ratio = np.full(centers.shape, np.nan, dtype=float)
        np.divide(bins, sim_bins, out=ratio, where=sim_bins > 0)
        plt.bar(centers, ratio, width=1, align='center')
        plt.xlim(xlim)
        plt.xlabel("Counts in stream")
        plt.ylabel("Ratio of counts")
        plt.title("Ratio of count distribution")

        plt.tight_layout()
        xr.insert_plot()
        plt.close()

    @pytest.mark.funcgen_noise_gen
    async def test_funcgen_noise_min_max(self, xr, number_of_bursts, n=10):
        '''
        Plot the noise values that appear most and least frequently in the noise generator stream and a numpy pseudorandom stream.

         Parameters:
            number_of_bursts (int): Number of 4-frame bursts to capture as part of a run
            n (int): how many of the least and most frequent values to plot (n = 10 means 20 values will be plotted)

        Note:
            This does not yet perform a test and thus cannot fail. It is useful to manually identify any suspicious patterns
        '''
        seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value
        data = (await self._set_capture_funcgen('noise',
                                        period=None,
                                        frames_per_burst = 4,
                                        keep_all_frames=True,
                                        mode=1,
                                        number_of_bursts = number_of_bursts,
                                        burst_period_in_frames = self.data.BURST_PERIOD,
                                        seed = seed))[0].astype(self.data.FG_DTYPE_SIGNED)
        
        unique, counts = np.unique(data, return_counts=True)
        sorted_counts = np.sort(counts)
        top_n_counts = sorted_counts[sorted_counts.size - n:]
        bottom_n_counts = sorted_counts[:n]
        print("Distinct counts: " + str(len(set(counts))))
        print("Range of counts: " + str(sorted_counts[0]) + " - " + str(sorted_counts[-1]))
        top_n_pairs = [(unique[idx], value) for idx, value in enumerate(counts) if value in top_n_counts] #can be larger than n if multiple values have the same lowest count
        bottom_n_pairs = [(unique[idx], value) for idx, value in enumerate(counts) if value in bottom_n_counts]

        #print("Most common values: " + str(top_n_pairs))
        #print("Least common values: " + str(bottom_n_pairs))
        plt.scatter(*list(zip(*top_n_pairs)), label = f"values with {n} highest counts", s=1)
        plt.scatter(*list(zip(*bottom_n_pairs)), label = f"values with {n} lowest counts", s=1)
        plt.xlabel("noise value")
        plt.ylabel("count")
        plt.legend()

        plt.tight_layout()
        xr.insert_plot()
        plt.clf()

    @pytest.mark.funcgen_noise_gen
    async def test_funcgen_noise_psd(self, xr, number_of_bursts):

        '''
        Plots the spectrum of a noise generator stream and a numpy pseudorandom stream with plt.psd.

        Parameters:
            number_of_bursts (int): Number of 4-frame bursts to capture as part of a run
        '''
        seed = np.random.randint(0, 2 ** 16) #seed is 16-bit value
        data = (await self._set_capture_funcgen('noise',
                                        period=None,
                                        frames_per_burst = 4,
                                        mode=1,
                                        number_of_bursts = number_of_bursts,
                                        burst_period_in_frames = self.data.BURST_PERIOD,
                                        seed = seed)).astype(self.data.FG_DTYPE_SIGNED)
        plt.subplot(2,1,1)
        noise_psd, _ = plt.psd(data[0].ravel())
        plt.title(f"Power spectrum density of LFSR noise")

        plt.subplot(2,1,2)
        sim_data = self.sim_random_gen(4 * number_of_bursts)
        sim_psd, _ = plt.psd(sim_data)
        plt.title(f"Power spectrum density of simulated noise")

        noise_cv = np.std(noise_psd) / np.mean(noise_psd)
        sim_cv = np.std(sim_psd) / np.mean(sim_psd)
        print(noise_cv, sim_cv)

        plt.tight_layout()
        xr.insert_plot()
        plt.close()

        assert(noise_cv < 0.16) # 0.15 is a bit tight for 1 and 10 samples (we sometimes get .1508).

    @pytest.mark.funcgen_counters
    async def test_word_and_frame_ctr(self, xr, number_of_bursts, burst_period_in_frames):
        '''Tests that frame counter and word counter have the correct count for each received word and frame in the word_ctr_buffer_flags mode

         Parameters:
            number_of_bursts (int): Number of 4-frame bursts to capture as part of a run
        '''

        logger = self.get_logger()
        frames_per_burst = 4
        #burst_period_in_frames = self.data.BURST_PERIOD
        data = (await self._set_capture_funcgen(
            "word_ctr_buffer_flags",
            period=None,
            frames_per_burst=frames_per_burst,
            mode = 1,
            number_of_bursts=number_of_bursts,
            burst_period_in_frames=burst_period_in_frames,
            keep_all_frames=True,
        ))[0, ...].ravel()

        words_per_frame = self.data.SAMPLES_PER_FRAME // self.board.SAMPLES_PER_WORD
        words_per_burst = words_per_frame * 4
        words_per_cap = words_per_burst * number_of_bursts

        #process data to get word count and frame count
        packed_samples = np.zeros(data.size // self.board.SAMPLES_PER_WORD, dtype = np.uint32)
        sample_mask = (1 << self.board.ADC_BITS_PER_SAMPLE) - 1
        if self.data.PLATFORM == "CRS":
            #For CRS, the 32 LSBs of each word contains is split into frame counter (word[31:11]) and word counter (word[10:0])            
            K = self.board.SAMPLES_PER_WORD
            for i in range(len(packed_samples)):
                packed_samples[i] = sum((int(data[self.board.SAMPLES_PER_WORD * (i + 1) - 1 - w]) & sample_mask) << (self.board.ADC_BITS_PER_SAMPLE * w) for w in range(K))        
            word_counter_mask = 0x7FF #11 LSBs
            frame_counter_mask = 0x1FFFFF
            word_bits = 11
        else:
            data = data.astype("u1")
            #For ICE, each sample is split into frame counter (sample[31:8]) and word counter (sample[7:0])
            for i in range(len(packed_samples)):
                packed_samples[i] = sum(int(data[self.board.SAMPLES_PER_WORD * i + w]) << (self.board.ADC_BITS_PER_SAMPLE * (self.board.SAMPLES_PER_WORD - w - 1)) for w in range(self.board.SAMPLES_PER_WORD))
            word_counter_mask = 0x1FF #9 LSBs
            frame_counter_mask = 0x7FFFFF
            word_bits = 9
        # packed_samples = data.view(np.int32)

        #Process bits for frame counter
        frame_count = np.bitwise_and((packed_samples >> word_bits), frame_counter_mask)
        #Process bits for word counter
        word_count = np.bitwise_and(packed_samples, word_counter_mask)

        cn = (np.arange(words_per_cap) // (words_per_burst)) * burst_period_in_frames
        expected_frame_count = (np .arange(words_per_cap) // words_per_frame) % frames_per_burst + frame_count[0] + cn
        expected_word_count = np.tile(np.arange(words_per_frame), frames_per_burst * number_of_bursts)
        
        plt.figure(f"{number_of_bursts}")
        plt.subplot(2, 1, 1)
        plt.plot(expected_frame_count, marker='.', label="expected frame count")
        plt.plot(frame_count, label="measured frame count")
        plt.xlabel("Sample Count")
        plt.ylabel("Frame Count")
        plt.subplot(2, 1, 2)
        plt.plot(expected_word_count, marker=".",label="expected word count")
        plt.plot(word_count, label="measured word count")
        plt.xlabel("Sample Count")
        plt.ylabel("Word Count")
        plt.legend()
        plt.suptitle(f"Frame/Word Count with: \n4 frames per burst, {number_of_bursts} bursts and {burst_period_in_frames} frames between bursts")
        xr.insert_plot()
        plt.close()

        np.testing.assert_array_equal(frame_count, expected_frame_count)
        logger.debug(f"Frame count matches expected frame count")
        np.testing.assert_array_equal(word_count, expected_word_count)
        logger.debug(f"Word count matches expected word count")

    def _extract_counter_samples(self, raw_data, fmt):
        """
        Extract frame-counter values from raw FUNCGEN samples.

        The mapping follows funcgen.vhd:
        - frame8: frame_ctr[ADC_BITS_PER_SAMPLE-3:0] in bits [ADC_BITS_PER_SAMPLE-1:2].
        - frame4: frame_ctr[3:0] in the 4 MSBs of each sample.
        """
        if fmt == "frame4":
            counter_bits = 4
            shift = self.board.ADC_BITS_PER_SAMPLE - 4
            decoded = (raw_data >> shift) & 0xF
        elif fmt == "frame8":
            counter_bits = self.board.ADC_BITS_PER_SAMPLE - 2
            decoded = (raw_data >> 2) & ((1 << counter_bits) - 1)
        else:
            raise ValueError(f"Unsupported frame-counter format: {fmt}")

        counter_mod = 1 << counter_bits
        return decoded, counter_bits, counter_mod

    def _samples_to_frame_ids(self, decoded_samples, total_frames):
        """
        Convert decoded samples into one frame ID per frame.

        Each frame must contain a constant counter value across all samples.
        """
        if decoded_samples.ndim != 2:
            raise ValueError(f"Expected 2D array (nframes, nsamples), got {decoded_samples.shape=}")
        if decoded_samples.shape[0] != total_frames:
            raise ValueError(
                f"Unexpected number of frames: got {decoded_samples.shape[0]}, expected {total_frames}"
            )

        per_frame_constant = np.all(decoded_samples == decoded_samples[:, :1], axis=1)
        assert np.all(per_frame_constant), "Counter value is not constant within at least one frame."
        return decoded_samples[:, 0]

    def _expected_frame_ids(self, start_id, total_frames, frames_per_burst, burst_period_in_frames, counter_mod):
        """
        Build expected frame IDs using first measured frame as baseline.
        """
        frame_idx = np.arange(total_frames)
        in_burst_idx = frame_idx % frames_per_burst
        burst_idx = frame_idx // frames_per_burst
        return (start_id + in_burst_idx + burst_idx * burst_period_in_frames) % counter_mod

    def _decode_nibble4_samples(self, raw_channel_frames):
        """
        Decode nibble4 frame values from raw samples.

        FN_RAM_NIBBLE4 places a 4-bit value in the 4 MSBs of each sample.
        """
        shift = self.board.ADC_BITS_PER_SAMPLE - 4
        return (raw_channel_frames >> shift) & 0xF

    def _frame_constant_values(self, decoded_frames):
        """
        Ensure each frame has a constant decoded value and return one value per frame.
        """
        if decoded_frames.ndim != 2:
            raise ValueError(f"Expected 2D frame data, got {decoded_frames.shape=}")
        per_frame_constant = np.all(decoded_frames == decoded_frames[:, :1], axis=1)
        assert np.all(per_frame_constant), "Nibble4 value is not constant within at least one frame."
        return decoded_frames[:, 0]

    def _expected_nibble4_frame_values(self, start_value, total_frames, low_nibble, high_nibble):
        """
        Build expected nibble4 values, auto-aligned to first captured frame parity.
        """
        if start_value not in (low_nibble, high_nibble):
            raise AssertionError(
                f"First frame value must be either low nibble ({low_nibble}) or high nibble ({high_nibble}), "
                f"got {start_value}."
            )
        first = low_nibble if start_value == low_nibble else high_nibble
        second = high_nibble if first == low_nibble else low_nibble
        pattern = np.array([first, second], dtype=np.uint16)
        return np.tile(pattern, (total_frames + 1) // 2)[:total_frames]

    @pytest.mark.funcgen_counters
    @pytest.mark.parametrize("fmt", ("frame8", "frame4"))
    @pytest.mark.parametrize("frames_per_burst", (2, 4, 8, 16))
    async def test_frame_counter_byte(self, xr, fmt, frames_per_burst, burst_period_in_frames, number_of_bursts):
        '''Tests frame counter encoding and sequence for modes frame8 and frame4 
        
        Parameters:
            fmt (str): either frame8 or frame4
            frames_per_burst (int): Number of consecutive frames to capture as part of one burst
            burst_period_in_frames (int): Number of frames to wait between capturing bursts
            number_of_bursts (int): Number of bursts to capture as part of a run
        '''
        
        
        if getattr(self.board, "CAPTURE_TYPE", None) == "PROBER" and frames_per_burst > 4:
            pytest.skip(f"PROBER supports at most 4 frames per burst, got {frames_per_burst}.")

        frames_to_mode = {2: 0, 4: 1, 8: 2, 16: 3}
        mode = 0 if getattr(self.board, "CAPTURE_TYPE", None) == "PROBER" else frames_to_mode[frames_per_burst]

        for chan in self.board.chan.values():
            chan.FUNCGEN.RESET = 1
            chan.FUNCGEN.RESET = 0
            # Keep shifts neutral so the sample encoding matches firmware definitions exactly.
            chan.FUNCGEN.LEFT_SHIFT = 0
            chan.FUNCGEN.RIGHT_SHIFT = 0


        
        frames_to_mode = {2: 0, 4: 1, 8: 2, 16: 3}
        mode = frames_to_mode.get(frames_per_burst, -1)
        if mode == -1 and self.data.PLATFORM == 'CRS':
            raise ValueError(f'UCAP does not support {frames_per_burst} frames per burst')
        # Keep explicit (frame, sample) boundaries.x
        raw_data = (await self._set_capture_funcgen(
            source=fmt,
            period=None,
            frames_per_burst=frames_per_burst,
            mode=mode,
            number_of_bursts=number_of_bursts,
            burst_period_in_frames=burst_period_in_frames,
            keep_all_frames=True,
        )).astype(self.data.FG_DTYPE)

        # Use one deterministic channel for sequence checks.
        channel_data = raw_data[0]
        total_frames = frames_per_burst * number_of_bursts

        # Decode mode-specific counter bits from each sample.
        decoded_samples, counter_bits, counter_mod = self._extract_counter_samples(channel_data, fmt=fmt)

        # Reduce to one frame ID per frame and verify per-frame constancy.
        measured_frame_ids = self._samples_to_frame_ids(decoded_samples, total_frames)

        # Auto-align to first captured frame value, then validate strict increments and burst jumps.
        expected_frame_ids = self._expected_frame_ids(
            start_id=int(measured_frame_ids[0]),
            total_frames=total_frames,
            frames_per_burst=frames_per_burst,
            burst_period_in_frames=burst_period_in_frames,
            counter_mod=counter_mod,
        )

        self.plot_and_test(
            xr,
            measured_frame_ids,
            expected_frame_ids,
            title=f"Frame Counter (format={fmt}, {counter_bits}-bit)\n"
                  f"{frames_per_burst} frames/burst, {number_of_bursts} bursts, period {burst_period_in_frames}",
            xlabel="Frame index",
            ylabel="Frame ID",
        )

    @pytest.mark.funcgen_counters
    @pytest.mark.parametrize("frames_per_burst", (2, 4, 8, 16))
    async def test_frame_counter_nibble(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        '''
        Test that the frame counter is properly encoded in mode nibble4 (low nibble of buffer on one frame, high nibble on the next).

        Parameters:
            frames_per_burst (int): Number of consecutive frames to capture as part of one burst
            burst_period_in_frames (int): Number of frames to wait between capturing bursts
            number_of_bursts (int): Number of bursts to capture as part of a run
        '''
        if getattr(self.board, "CAPTURE_TYPE", None) == "PROBER" and frames_per_burst > 4:
            pytest.skip(f"PROBER supports at most 4 frames per burst, got {frames_per_burst}.")

        for chan in self.board.chan.values():
            chan.FUNCGEN.RESET = 1
            chan.FUNCGEN.RESET = 0
            # Keep output shifting neutral so nibble placement matches firmware behavior exactly.
            chan.FUNCGEN.LEFT_SHIFT = 0
            chan.FUNCGEN.RIGHT_SHIFT = 0

        # Deterministic source byte: low nibble=0, high nibble=1.
        injected_sample = 0x10
        low_nibble = injected_sample & 0xF
        high_nibble = (injected_sample >> 4) & 0xF
        injected_data = np.full(self.data.SAMPLES_PER_FRAME, injected_sample, dtype=self.data.FG_DTYPE)

        # Validate alternation within each burst; one frame per burst is insufficient to check this.
        capture_frames_per_burst = max(frames_per_burst, 2)

        frames_to_mode = {2: 0, 4: 1, 8: 2, 16: 3}
        mode = 0 if getattr(self.board, "CAPTURE_TYPE", None) == "PROBER" else frames_to_mode.get(frames_per_burst, -1)
        if mode == -1 and self.PLATFORM == 'CRS':
            raise ValueError(f'UCAP does not support {frames_per_burst} frames per burst')
        raw_data = (await self._set_capture_funcgen(
            "nibble4",
            period=None,
            frames_per_burst=capture_frames_per_burst,
            mode=mode,
            number_of_bursts=number_of_bursts,
            burst_period_in_frames=burst_period_in_frames,
            buffer=injected_data,
            keep_all_frames=True,
        )).astype(self.data.FG_DTYPE)

        # Use a deterministic channel and keep explicit (frame, sample) shape.
        raw_channel_frames = raw_data[0]
        total_frames = capture_frames_per_burst * number_of_bursts
        if raw_channel_frames.shape[0] != total_frames:
            raise ValueError(
                f"Unexpected number of frames: got {raw_channel_frames.shape[0]}, expected {total_frames}"
            )

        # Decode nibble4 values and reduce to one value per frame.
        decoded_frames = self._decode_nibble4_samples(raw_channel_frames)
        measured_frame_values = self._frame_constant_values(decoded_frames)
        if np.any((measured_frame_values != low_nibble) & (measured_frame_values != high_nibble)):
            raise AssertionError(
                f"Nibble4 produced values outside expected set {{{low_nibble}, {high_nibble}}}: "
                f"{np.unique(measured_frame_values)}"
            )

        # Check strict alternation within each burst, auto-aligned to each burst's first frame.
        measured_by_burst = measured_frame_values.reshape(number_of_bursts, capture_frames_per_burst)
        expected_by_burst = np.empty_like(measured_by_burst, dtype=np.uint16)
        for burst_idx in range(number_of_bursts):
            expected_by_burst[burst_idx] = self._expected_nibble4_frame_values(
                start_value=int(measured_by_burst[burst_idx, 0]),
                total_frames=capture_frames_per_burst,
                low_nibble=low_nibble,
                high_nibble=high_nibble,
            )
        expected_frame_values = expected_by_burst.ravel()

        self.plot_and_test(
            xr,
            measured_frame_values,
            expected_frame_values,
            title=f"Frame Counter nibble4\n"
                  f"{capture_frames_per_burst} frames/burst, {number_of_bursts} bursts, period {burst_period_in_frames}",
            xlabel="Frame index",
            ylabel="Nibble value",
        )

    async def test_reset(self, xr):
        '''Tests that all counters are reset after toggling funcgen reset'''
        logger = self.get_logger()
        injected_data = np.zeros(self.data.SAMPLES_PER_FRAME)
        self.board.set_channelizer(data_source='buffer', buffer=injected_data)
        logger.debug(f"Started data capture")
        await self._set_capture_funcgen('buffer', buffer=injected_data)
        self.board.stop_data_capture()
        logger.debug(f"Stopped data capture")
        for chan in self.board.chan.values():
            chan.FUNCGEN.RESET = 1
            chan.FUNCGEN.RESET = 0
            logger.debug(f"Channelizer {chan} reset")
            chan.FUNCGEN.reset()
        sleep(0.01)
        for chan in self.board.chan.values():
            # RAMP_CTR/FRAME_CTR are debug counters that resume counting immediately after reset,
            # so they are not guaranteed to still read as zero at software read time.
            # In firmware, reset sets SEND_FRAME to the current ENABLE value.
            assert chan.FUNCGEN.SEND_FRAME == chan.FUNCGEN.ENABLE
            assert chan.FUNCGEN.OVERFLOW_CTR == 0

    async def test_delay_capture(self, xr):
        '''Tests that all channelizers count the same delay'''
        for chan in self.board.chan.values():
            chan.FUNCGEN.RESET = 1
            chan.FUNCGEN.RESET = 0
        await self._set_capture_funcgen('ramp')
        delay_ctrs = [ch.FUNCGEN.DELAY_CTR for ch in self.board.chan.values()]
        assert(len(set(delay_ctrs)) == 1) #ensure all delay counters are the same

    async def test_shift_clip_width(self, xr, shift, clip_width):
        """
        Tests shifting and saturation.
        Hardware behavior is: left shift, clip to full-width, then right shift.

        Parameters:
            clip_width (int): amount to clip (shift left by)
            shift (int): amount to shift right by

        """
        nbits = self.data.ADC_BITS_PER_SAMPLE
        max_val = 2 ** (nbits - 1) - 1
        min_val = -2 ** (nbits - 1)

        source = gen_data('ramp', min=min_val, max=max_val, samples=self.data.SAMPLES_PER_FRAME).astype(np.int32)
        ref_data = np.right_shift(np.clip(np.left_shift(source, clip_width), min_val, max_val), shift)
        data = (await self._set_capture_funcgen("arb", data=source, funcgen_left_shift=clip_width, funcgen_right_shift=shift))[0]
        self.plot_and_test(
            xr,
            data,
            ref_data,
            title=f"Shift and clip with left_shift={clip_width} and right_shift={shift}",
        )

    @pytest.mark.funcgen_counters
    @pytest.mark.parametrize('fmt', ('word_ctr_buffer_flags', 'frame8', 'frame4'))
    async def test_overflow_ctr(self, xr, fmt, expected_overflows_per_frame, stats_frames):
        '''Tests that the overflow counter  works correctly by simulating overflows and ensuring the correct number is counted.
         Overflows can be simulated in frame8, frame4, and  word_ctr_buffer_flags by injecting '1's into the buffer.
         Each 1 injected in the buffer represents an overflow in the sample. The number of frames over which we count the number of overflows is
         specified in FUNCGEN.STATS_PERIOD which represents the desired frame period + 1. E.g. FUNCGEN.STATS_PERIOD = 11 => frame period of 10.
         
         Parameters:
            fmt (str): either frame8, frame4 or word_ctr_buffer_flags
            expected_overflows_per_frame: number of overflows to simulate per frame
            stats_frameS: number of frames to over which to collect overflow statistics
         '''
        

        logger = self.get_logger()
        injected_data = np.zeros(self.data.SAMPLES_PER_FRAME, dtype=self.data.FG_DTYPE)
        injected_data[:expected_overflows_per_frame] = 1  # sample LSB=1 -> simulated overflow

        # Keep shifting neutral so we only count injected/source overflows.
        for chan in self.board.chan.values():
            chan.FUNCGEN.set_output_shifting(left_shift=0, right_shift=0)
            chan.FUNCGEN.STATS_ENABLE = 0
            chan.FUNCGEN.RESET = 1
            chan.FUNCGEN.RESET = 0

        # Configure FUNCGEN source directly; capture is not required for overflow counter updates.
        self.board.set_channelizer(
            data_source=fmt,
            fft_bypass=1,
            scaler_bypass=1,
            overflow_buffer=injected_data,
        )

        for chan in self.board.chan.values():
            chan.FUNCGEN.STATS_PERIOD = stats_frames - 1
            chan.FUNCGEN.STATS_ENABLE = 1

        # Wait until at least one statistics integration period is completed.
        timeout_s = 2.0
        t0 = time.time()
        while any(chan.FUNCGEN.STATS_DONE == 0 for chan in self.board.chan.values()):
            if time.time() - t0 > timeout_s:
                raise TimeoutError("Timed out waiting for FUNCGEN overflow statistics completion (STATS_DONE).")
            sleep(0.01)

        expected_total = stats_frames * expected_overflows_per_frame
        for chan in self.board.chan.values():
            measured = chan.FUNCGEN.OVERFLOW_CTR
            logger.debug(f"Channel {chan.chan_number}: OVERFLOW_CTR={measured}, expected={expected_total}")
            assert measured == expected_total
