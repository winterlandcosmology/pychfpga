
import logging
import numpy as np
import pytest
import socket
import psutil
from .net_tools import ping_sources_async
from functools import wraps 
import matplotlib.pyplot as plt
from pathlib import Path
import math
from time import sleep
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
from pychfpga.fpga_array import FPGAArray
import sys

pytestmark = pytest.mark.funcgen


#1. Separate config files for scaler and funcgen (conftest)
#2. Import pytest_generate_tests, board_conn,  to be used by both scaler and funcgen
#3. overflow buffer can be programmed from set_data_source
#4. noise seeds can be set in set_data_source

class TestFuncgen:
    """
    1. When pytest is run on the command line, the pytest.ini file provides the config file as a command-line argument
    2. In a session start hook, xr parses this config file and converts it to a namespace in xr.config
    3. The pytest-generate-tests hook is called on each test (with a new instance of TestFuncgen each time)
        a. The xr object is extracted from the metafunc passed to this function, from which the config data can be accessed
        b. Any candidate fixtures in the metafunc are paramatrised according to an arbitrary user-provided python expression in the config file.
        All fixtures which are present in the argument list of the function as well as in the config file (either as global or test-specific parameters) are parameterized.
    4. Before each test, the function-level setup fixture is called. Via the xr fixture, it gains access to the xr object and thus all the parameters contained therein. 
    It then either connects to the board or extracts the cached board object from the xr object, as needed.
    Finally, it transfers important parameters, including the board object, from xr to the namespace of self. 
    5. The test is run.

    TODO: 1. Import pytest_generate_tests into .utils so that both funcgen, scaler and future test modules use the same hook.
          2. Separate config files for each module if config.yaml becomes too convoluted.  
    """

    def pytest_generate_tests(self, metafunc):
        xr = get_xr_from(metafunc)
        test_config = xr.config.test_config
        test_name = metafunc.function.__name__[5:] #get rid of test_ prefix
        for fixture in metafunc.fixturenames:
            #find global default, if there is
            global_param = 'global_' + fixture
            found_param = False
            if global_param in test_config.keys():
                value = test_config[global_param]
                found_param = True
            if test_name in test_config.keys(): #not elif, so global will be overriden if applicable
                test_params = test_config[test_name]
                if fixture in test_params:
                    value = test_params[fixture]
                    found_param = True
            if found_param:
                print(f'Parametrising {fixture}')
                metafunc.parametrize(fixture, value) 

    def board_conn(self, xr):
        if 'board' in xr.data: #check if board is cached to avoid unnecessary reconnection
            return xr.data.board
        ca = FPGAArray(**self.connection_config)
        xr.data.board = ca.ib[0]
        return xr.data.board
        
    @pytest.fixture(scope='function', autouse=True)
    def setup(self, xr):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = self.board_conn(xr)
        self.SAMPLES_PER_FRAME = self.board.ADC_SAMPLES_PER_FRAME
        self.ADC_BITS_PER_SAMPLE = self.board.ADC_BITS_PER_SAMPLE
        self.Nvalues = 1 << self.ADC_BITS_PER_SAMPLE
        self.NUMBER_OF_CHANNELIZERS = self.board.NUMBER_OF_CHANNELIZERS
        self.PLATFORM = self.board.mb.part_number #indicates if board is "CRS", "ZCU111" or "MGK7MB" (IceBoard)
        self.seeds = self.test_config["global_seeds"]

        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.loglevelconn)
        
        # Should throw an exception when ADC_BYTES_PER_FRAME not 1 or 2 but the connection does it itself
        if self.board.ADC_BYTES_PER_SAMPLE == 1:
            self.FG_DTYPE = 'u1'
            self.FG_DTYPE_SIGNED = 'i1'
            self.FG_LSHIFT = 8 - self.board.ADC_BITS_PER_SAMPLE
        else:
            self.FG_DTYPE = 'u2'
            self.FG_DTYPE_SIGNED = 'i2'
            self.FG_LSHIFT = 16 - self.board.ADC_BITS_PER_SAMPLE

        
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
            assert np.array_equal(data, expected_data)

    def test_udp_buffers_size(self):
        logger = self.get_logger()
        buff_config = list()
        logger.debug("Reading system files in /proc/sys/net")
        with open("/proc/sys/net/core/rmem_max") as file:
            buff_config.append(int(file.readline()))
        with open("/proc/sys/net/core/rmem_default") as file:
            buff_config.append(int(file.readline()))
        with open("/proc/sys/net/ipv4/udp_mem") as file:
            for val in file.readline().split():
                buff_config.append(int(val))
        with open("/proc/sys/net/ipv4/udp_rmem_min") as file:
            buff_config.append(int(file.readline()))

        if any(val < 26214400 for val in buff_config):
            raise RuntimeError(
                "The UDP buffers' memory parameters (net.core.rmem_max, net.core.rmem_default, "
                "net.ipv4.udp_mem, net.ipv4.udp_rmem_min) are too low. Increase them to at least 26214400 "
                "bytes. \nYou can do it by running:\n"
                ">>> sudo sysctl -w net.core.rmem_max=26214400 net.core.rmem_default=26214400 "
                "net.ipv4.udp_mem='26214400 26214400 26214400' net.ipv4.udp_rmem_min=26214400"
            )

    @pytest.mark.asyncio
    async def test_mtu_size(self, xr):
        logger = self.get_logger()
        logger.debug("Getting the board's address")
        port_map = dict(
            port=self.board.get_data_socket().getsockname()[1],
            sources=[(self.board.hostname, 80)]
        )
        logger.debug(f"The board located at {port_map['sources'][0][0]}")
        logger.debug("Pinging the board to determine interface used")
        src_if_addrs = await ping_sources_async([port_map])
        if_ip = list(src_if_addrs.values())[0]
        logger.debug(f"Interface address: {if_ip[0]}:{if_ip[1]}")
        if_addrs = psutil.net_if_addrs()
        if_name = None
        for key in if_addrs.keys():
            for snic_addr in if_addrs[key]:
                if snic_addr[0] == socket.AF_INET:
                    if snic_addr[1] == if_ip[0]:
                        if_name = key
        logger.debug(f"Interface name: {if_name}")
        if_stats = psutil.net_if_stats()
        mtu = if_stats[if_name].mtu
        logger.debug(f"Interface MTU size: {mtu}")

        if mtu < 9000:
            raise RuntimeError(f"Default network interface has MTU={mtu} (<9000). Please set MTU to 9000 to "
                               f"prevent loss of packages.")

    def test_packets_number(self, xr):
        """
        Ensures that all packages sent by the motherboard are received.
        """
        logger = self.get_logger()
        logger.debug("Starting data capture for one period")
        self.board.start_data_capture(period=1)
        receiver = self.board.get_data_receiver()
        logger.debug("Reading raw frames")
        _, _, count = receiver.read_raw_frames()
        logger.info(f"Expected {len(count)} packages, received {len([p for p in count if p])}.")
        assert all(count), "Missing packages from ICE board. Check connection and system configuration."

    def _set_capture_funcgen(self, source: str, period: float = 1, frames_per_burst: int = 2, number_of_bursts: int = 1, burst_period_in_frames=None, flatten = True, **func_kwargs):
        """
        Set funcgen to specified data output and capture outcoming data within specified period.
        """
        logger = self.get_logger()
        logger.debug("Starting data capture from FUNCGEN")
        self.board.start_data_capture(period=period, source='adc', frames_per_burst = frames_per_burst, number_of_bursts = number_of_bursts, burst_period_in_frames=burst_period_in_frames)
        logger.debug(f"Setting data source to {source.upper()}")
        self.board.set_channelizer(data_source=source, fft_bypass=1, scaler_bypass=1, **func_kwargs)
        logger.debug("Initializing data receiver")
        receiver = self.board.get_data_receiver()
        if self.PLATFORM == "CRS":
            #ucap
            _, data, _ = receiver.read_raw_frames(format='16', split=True, ncap=number_of_bursts)
        else:
            #prober
            number_of_frames = frames_per_burst * number_of_bursts
            _, data, _ = receiver.read_raw_frames(number_of_frames=number_of_frames)
        data = data[0].flatten() >> self.FG_LSHIFT if flatten else data[0] >> self.FG_LSHIFT
        return data

    @pytest.mark.funcgen_patterns
    def test_funcgen_ramp(self, xr):
        expected_data = (np.arange(self.SAMPLES_PER_FRAME) - self.Nvalues / 2) % self.Nvalues - self.Nvalues / 2
        data = self._set_capture_funcgen('ramp', flatten=False)[0] #first frame
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen ramp",
            )

    @pytest.mark.funcgen_patterns
    def test_funcgen_sin(self, xr):
        sin_freq = 1
        expected_data = (np.sin(np.arange(self.SAMPLES_PER_FRAME) * 2 * np.pi / self.SAMPLES_PER_FRAME * sin_freq) * 127).astype(self.FG_DTYPE_SIGNED)
        data = self._set_capture_funcgen('sin', freq=sin_freq, ampl=127, flatten=False)[0] #first frame
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen sin",
            )

    @pytest.mark.funcgen_patterns
    def test_funcgen_arb(self, xr):
        expected_data = np.ones(self.SAMPLES_PER_FRAME)
        data = self._set_capture_funcgen('arb', data=expected_data, flatten=False)[0] #first frame
        logger = self.get_logger()
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen arb",
            )

    @pytest.mark.funcgen_patterns
    def test_funcgen_const(self, xr):
        a = 13
        expected_data = np.full(self.SAMPLES_PER_FRAME, a, self.FG_DTYPE).astype(self.FG_DTYPE_SIGNED)
        data = self._set_capture_funcgen('a', a=a, flatten=False)[0] #first frame
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen const",
            )

    @pytest.mark.funcgen_patterns
    def test_funcgen_ab(self, xr):
        a = 0 
        b = 42
        expected_data = np.tile(np.array((a, b), self.FG_DTYPE), self.SAMPLES_PER_FRAME // 2).astype(self.FG_DTYPE_SIGNED)
        data = self._set_capture_funcgen('ab', a=a, b=b, flatten=False)[0] #first frame
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen ab",
            )

    @pytest.mark.funcgen_patterns
    def test_funcgen_real_ramp(self, xr):
        expected_data = np.ravel([(i,0) for i in range(self.SAMPLES_PER_FRAME//2)]).astype(self.FG_DTYPE_SIGNED)
        data = self._set_capture_funcgen('real_ramp', flatten=False)[0] #first frame
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Funcgen real ramp",
            )

    @pytest.mark.funcgen_noise_gen
    def test_funcgen_deterministic(self, xr):
        """Test that the noise generator resets with rst_lfsr and that the captured
            data between each run are identical for the same seed"""
        seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value
        self.board.start_data_capture(period=1, source='adc', frames_per_burst = 2, number_of_bursts = 0)
        self.board.set_channelizer(data_source='noise', seed = seed)
        receiver = self.board.get_data_receiver()
        self.board.chan[1].FUNCGEN.reset_noise()
        sleep(0.1)
        if self.PLATFORM == "CRS":
            _, data, _ = receiver.read_raw_frames(format='16', split = True)
        else:
            _, data, _ = receiver.read_raw_frames()
        assert not np.array_equal(data[0], data[1])

    # @pytest.mark.funcgen_noise_gen
    # def test_lfsr_output(self, xr):
    #     """Outputs lfsr noise binary values into a txt file. Can be used for further statistical tests"""
    #     seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value
    #     data = self._set_capture_funcgen('noise',seed = seed).astype(self.FG_DTYPE_SIGNED)
    #     processed_data = self.noise_data_process(data, self.board.ADC_BITS_PER_SAMPLE)
    #     processed_data = np.unpackbits(processed_data)
    #     print(f"Binary values of the random noise generated are saved at {Path('../../fw_test_results/test_results_assets')}")
    #     np.savetxt(Path('../../fw_test_results/random.txt'), processed_data, fmt='%.1d', delimiter=' ', newline='')

    @pytest.mark.funcgen_noise_gen
    def test_funcgen_random_pval(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        """Monobit test (from NIST's Statistical Test Suite for Random and Pseudorandom
            Number Generatorson funcgen noise generator). The generated noise is considered
            random when ``p_val_threshold`` percent of the total test runs have a P-val > ``p_val_threshold``. """
        p_val_threshold = 0.01
        number_runs = len(self.seeds)
        p_val_l= np.zeros(number_runs)
        for i in range(number_runs):
            sum = 0
            data = self._set_capture_funcgen('noise',
                                            period=None,
                                            frames_per_burst=frames_per_burst,
                                            number_of_bursts=number_of_bursts,
                                            burst_period_in_frames=burst_period_in_frames,
                                            seed = self.seeds[i]).astype(self.FG_DTYPE)
            data = np.unpackbits(self.noise_data_process(data, self.board.ADC_BITS_PER_SAMPLE))
            for bit in data:
                if bit == 0:
                    bit = -1
                sum += bit
            sn = abs(sum) / math.sqrt(len(data))
            p_val = math.erfc(sn/np.sqrt(2))
            p_val_l[i] = p_val
        plt.plot(self.seeds, p_val_l, marker='o', linestyle='None')
        plt.ylabel("p-val")
        plt.xlabel("seed")
        plt.title("p-val for each seed with: \n{frames_per_burst} frames per burst, {number_of_bursts} bursts and {burst_period_in_frames} frames between bursts")
        xr.insert_plot()
        plt.close()
        p = 1 - p_val_threshold
        success_proportion = np.sum(p_val_l >= p_val_threshold) / len(self.seeds)
        print(f"Number of successful pvals (greater than 0.01): {np.sum(p_val_l >= p_val_threshold)}")
        print(f"Percentage of successful runs: {success_proportion * 100}%")
        assert success_proportion >= (p - 3 * math.sqrt(p * (1 - p) / len(self.seeds))) and success_proportion <= (p + 3 * math.sqrt(p * (1 - p) / len(self.seeds)))

    def noise_data_process(self, data, bits_per_sample):
        """
        Transforms data of numpy type 'u2' into 'u1',
        where only the 14 LSBs of each u2 sample are processed
        and packed into bytes.
        """
        assert bits_per_sample <= 16, "Invalid bits_per_sample"
        processed_data = np.zeros(len(data))
        total_bits = bits_per_sample * len(data)
        total_bytes = math.ceil(total_bits / 8)
        processed_data = np.zeros(total_bytes, dtype='u1')

        bit_buffer = 0
        bit_count = 0
        byte_idx = 0

        for sample in data:
            sample &= (1 << bits_per_sample) - 1  # keep only lowest `bits_per_sample` bits
            bit_buffer = (bit_buffer << bits_per_sample) | sample
            bit_count += bits_per_sample

            while bit_count >= 8:
                bit_count -= 8
                byte = (bit_buffer >> bit_count) & 0xFF
                processed_data[byte_idx] = byte
                byte_idx += 1

        # If any bits remain (pad the last byte)
        if bit_count > 0:
            processed_data[byte_idx] = (bit_buffer << (8 - bit_count)) & 0xFF
        return processed_data
    
    def sim_random_gen(self, frames_per_burst, number_of_bursts):
        """Python simulation of noise generator"""
        num_samples = frames_per_burst * number_of_bursts * self.SAMPLES_PER_FRAME
        data = np.zeros(num_samples)
        for i in range(len(data)):
            data[i] = np.random.randint(-(2**(self.ADC_BITS_PER_SAMPLE - 1)), 2**(self.ADC_BITS_PER_SAMPLE - 1) -1)
        return data
    
    def plot_histogram(self, data):
        unique, counts = np.unique(data, return_counts=True)
        std_deviation = np.std(counts)
        avg = np.average(counts)
        upper_deviation = avg + std_deviation
        lower_deviation = avg - std_deviation

        plt.scatter(unique, counts, s=1)
        plt.axhline(y=upper_deviation, color="g", linestyle="--", label=r'+ 1$\sigma$')
        plt.axhline(y=lower_deviation, color="r", linestyle="--", label=r'- 1$\sigma$')
        plt.xlabel("noise value")
        plt.ylabel("count")
        plt.legend()
        
    
    @pytest.mark.funcgen_noise_gen
    def test_funcgen_noise_histogram(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        "Plot a histogram of noise generated by funcgen and noise generated by python simulation"
        seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value

        data = self._set_capture_funcgen('noise',
                                        period=None,
                                        frames_per_burst = frames_per_burst,
                                        number_of_bursts = number_of_bursts,
                                        burst_period_in_frames = burst_period_in_frames,
                                        seed = seed).astype(self.FG_DTYPE_SIGNED)

        sim_data = self.sim_random_gen(frames_per_burst, number_of_bursts)

        plt.subplot(2, 1, 1)
        self.plot_histogram(data)
        plt.title(f"Measured noise value frequencies with {seed=}")

        plt.subplot(2, 1, 2)
        self.plot_histogram(sim_data)
        plt.title(f"Simulated noise value frequencies")

        plt.tight_layout()
        xr.insert_plot()
        plt.close()

    def plot_min_max_count(self, data):
        unique, counts = np.unique(data, return_counts=True)
        min_count = np.min(counts)
        max_count = np.max(counts)
        min_values = [unique[idx] for idx, value in enumerate(counts) if counts[idx] == min_count]
        max_values = [unique[idx] for idx, value in enumerate(counts) if counts[idx]== max_count]
        plt.scatter(min_values, np.full(len(min_values), min_count), label = "values with min count", s=1)
        plt.scatter(max_values, np.full(len(max_values), max_count), label = "values with max count", s=1)
        plt.xlabel("noise value")
        plt.ylabel("count")
        plt.legend()

    @pytest.mark.funcgen_noise_gen
    def test_funcgen_noise_min_max(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        """Plot the noise values that appear the least often and the most often"""
        seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value
        data = self._set_capture_funcgen('noise',
                                        period=None,
                                        frames_per_burst = frames_per_burst,
                                        number_of_bursts = number_of_bursts,
                                        burst_period_in_frames = burst_period_in_frames,
                                        seed = seed).astype(self.FG_DTYPE_SIGNED)
        plt.subplot(2,1,1)
        self.plot_min_max_count(data)
        plt.title(f"Measured noise values with lowest and highest count with {seed=}")

        plt.subplot(2,1,2)
        sim_data = self.sim_random_gen(frames_per_burst, number_of_bursts)
        self.plot_min_max_count(sim_data)
        plt.title(f"Simulated noise values with lowest and highest count")

        plt.tight_layout()
        xr.insert_plot()

    @pytest.mark.funcgen_noise_gen
    def test_funcgen_noise_psd(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        """Plot the spectrum of measured noise vs spectrum of simulated noise using plt.pds"""
        seed = np.random.randint(-2 ** 15, 2 ** 15 - 1) #seed is 16-bit value
        data = self._set_capture_funcgen('noise',
                                        period=None,
                                        frames_per_burst = frames_per_burst,
                                        number_of_bursts = number_of_bursts,
                                        burst_period_in_frames = burst_period_in_frames,
                                        seed = seed).astype(self.FG_DTYPE_SIGNED)
        plt.subplot(2,1,1)
        plt.psd(data)
        plt.title(f"Measured Power Spectrum Density of noise with {seed=}")

        plt.subplot(2,1,2)
        sim_data = self.sim_random_gen(frames_per_burst, number_of_bursts)
        plt.psd(sim_data)
        plt.title(f"Simulated Power Spectrum Density of noise")

        plt.tight_layout()
        xr.insert_plot()
        plt.close()

    # @pytest.mark.skip
    @pytest.mark.funcgen_noise_gen
    def test_funcgen_noise_fft_avg_squared(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames, num_runs):
        """Plot the spectrum of measured noise vs spectrum of simulated noise by averaging the |fft|^2 of the measured/simulated noise over ``num_runs`` runs"""
        seeds = [np.random.randint(-2 ** 15, 2 ** 15 - 1) for i in range(num_runs)]
        total_samples = frames_per_burst * number_of_bursts * self.SAMPLES_PER_FRAME
        psd_a = np.empty((num_runs, total_samples))
        sim_psd_a = np.empty((num_runs, total_samples))
        for i in range(num_runs):
            data = self._set_capture_funcgen('noise',
                                            period=None,
                                            frames_per_burst = frames_per_burst,
                                            number_of_bursts = number_of_bursts,
                                            burst_period_in_frames = burst_period_in_frames,
                                            seed = seeds[i]).astype(self.FG_DTYPE_SIGNED)
            psd_a[i] = (np.abs(np.fft.fft(data)))**2

            sim_data = self.sim_random_gen(frames_per_burst, number_of_bursts)
            sim_psd_a[i] = (np.abs(np.fft.fft(sim_data)))**2
            
        psd_avg = np.mean(psd_a, axis=0)
        sim_psd_avg = np.mean(sim_psd_a, axis=0)

        plt.subplot(2,1,1)
        plt.plot(psd_avg)
        plt.ylim(0, max(psd_avg))
        plt.xlabel("frequency")
        plt.ylabel("magnitude")
        plt.title(f"Measured Average Fourier Transform of noise")

        plt.subplot(2,1,2)
        plt.plot(sim_psd_avg)
        plt.ylim(0, max(sim_psd_avg))
        plt.xlabel("frequency")
        plt.ylabel("magnitude")
        plt.title(f"Simulated Average Fourier Transform of noise")

        plt.tight_layout()
        xr.insert_plot()
        plt.close()

    @pytest.mark.funcgen_counters
    def test_word_and_frame_ctr(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        """Test for mode: "word_ctr_buffer_flags". Test that frame counter and word counter have the correct count according to the number of bursts and frames per burst."""
        logger = self.get_logger()
        data = self._set_capture_funcgen(
            "word_ctr_buffer_flags",
            period=None, 
            frames_per_burst=frames_per_burst, 
            number_of_bursts=number_of_bursts, 
            burst_period_in_frames=burst_period_in_frames
        ).astype(self.FG_DTYPE)
        receiver = self.board.get_data_receiver()
        ts, data, _ = receiver.read_raw_frames(format='16', split=True, ncap=number_of_bursts)
        print(ts)
        words_per_frame = self.SAMPLES_PER_FRAME // self.board.SAMPLES_PER_WORD
        words_per_burst = words_per_frame * frames_per_burst
        words_per_cap = words_per_burst * number_of_bursts

        #process data to get word count and frame count
        packed_samples = np.zeros(len(data) // self.board.SAMPLES_PER_WORD, dtype = np.uint32)
        print(f"{packed_samples.shape=}")
        if self.PLATFORM == "CRS":
            #For CRS, the 32 LSBs of each word contains is split into frame counter (word[31:11]) and word counter (word[10:0])            
            K = (32 + self.board.ADC_BITS_PER_SAMPLE + 1) // self.board.ADC_BITS_PER_SAMPLE #ceiling
            for i in range(len(packed_samples)):
                packed_samples[i] = sum(int(data[self.board.SAMPLES_PER_WORD * (i + 1) - 1 - w]) << (self.board.ADC_BITS_PER_SAMPLE * w) for w in range(K))        
            word_counter_mask = 0x7FF #11 LSBs
            frame_counter_mask = 0x1FFFFF
            word_bits = 11
        else:
            #For ICE, each sample is split into frame counter (sample[31:8]) and word counter (sample[7:0])
            for i in range(len(packed_samples)):
                packed_samples[i] = sum(int(data[self.board.SAMPLES_PER_WORD * i + w]) << (self.board.ADC_BITS_PER_SAMPLE * (self.board.SAMPLES_PER_WORD - w - 1)) for w in range(self.board.SAMPLES_PER_WORD))
            word_counter_mask = 0xFF #11 LSBs
            frame_counter_mask = 0xFFFFFF
            word_bits = 8    
        #Process bits for word counter
        frame_count = (packed_samples >> word_bits ) & frame_counter_mask
        #Process bits for frame counter
        word_count = np.bitwise_and(packed_samples, word_counter_mask)

        cn = (np.arange(words_per_cap) // (frames_per_burst * words_per_frame)) * burst_period_in_frames
        expected_frame_count = (np.arange(words_per_cap) // words_per_frame) % frames_per_burst + frame_count[0] + cn 
        expected_word_count = np.tile(np.arange(words_per_frame), frames_per_burst * number_of_bursts)
        
        plt.figure(f"{number_of_bursts}")
        plt.plot(expected_frame_count, marker='.', label="expected frame count")
        plt.plot(frame_count, label="Measured frame count")
        plt.plot(expected_word_count, marker=".",label="expected word count")
        plt.plot(word_count, label="Measured word count")
        plt.legend()
        plt.xlabel("Sample Count")
        plt.ylabel("Word/Frame Count")
        plt.title(f"Frame/Word Count with: \n{frames_per_burst} frames per burst, {number_of_bursts} bursts and {burst_period_in_frames} frames between bursts")
        xr.insert_plot()
        plt.close()

        assert np.array_equal(frame_count, expected_frame_count)
        logger.debug(f"Frame count matches expected frame count")
        assert np.array_equal(word_count, expected_word_count)
        logger.debug(f"Word count matches expected word count")

    @pytest.mark.funcgen_counters
    def test_frame_counter(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames, mode):
        """Test for mode: "frame8" or "frame4. Test that frame counter has the correct count according to the number of bursts and frames per burst."""
        for chan in self.board.chan.values():
            chan.FUNCGEN.reset()
        
        data = self._set_capture_funcgen(
            mode,
            period=None,
            frames_per_burst = frames_per_burst, 
            number_of_bursts = number_of_bursts, 
            burst_period_in_frames = burst_period_in_frames,
        ).astype(self.FG_DTYPE)

        if mode == "frame4":
            #4 LSBs of frame counter is in the 4 MSBs of each sample for both CRS and ICE
            shift = self.board.ADC_BITS_PER_SAMPLE - 4 #For CRS, shift right by 10 bits. For ICE, by 4 bits.
            frame_overflow_bit = 4
            data = (data >> shift) & 0xF
        else:
            #For CRS, the 12 LSBs of the frame count is in the 12 MSBs of each sample.
            #For ICE, the 6 LSBs of the frame count is in the 6 MSBs of each sample.
            shift = 2
            frame_overflow_bit = 12 if self.PLATFORM == "CRS" else 6
            data = (data >> shift) & 0xFFF if self.PLATFORM == "CRS" else (data >> shift) & 0x3F

        ref_incr_per_frame = np.tile(np.repeat(np.arange(frames_per_burst), self.SAMPLES_PER_FRAME), number_of_bursts)
        ref_incr_per_burst = np.repeat((np.arange(number_of_bursts) * burst_period_in_frames), self.SAMPLES_PER_FRAME * frames_per_burst)
        expected_data = (data[0] + ref_incr_per_frame + ref_incr_per_burst) % (2**frame_overflow_bit)

        if self.PLATFORM != 'CRS' and mode == 'frame4':
            #latency of one frame so first frame is not captured
            expected_data = np.concatenate(((data[0] - 1 + ref_incr_per_frame + ref_incr_per_burst)[self.SAMPLES_PER_FRAME:],
                                           np.full(self.SAMPLES_PER_FRAME, data[-1] + burst_period_in_frames))) % (2**frame_overflow_bit)
           
        self.plot_and_test(xr, 
            data, 
            expected_data, 
            title=f"Frame Count with mode: {mode} \n{frames_per_burst} frames per burst, {number_of_bursts} bursts and {burst_period_in_frames} frames between bursts",
            xlabel="Number of samples",
            ylabel="Frame Count",
            )
   
    @pytest.mark.funcgen_counters
    def test_frame_counter_nibble(self, xr, frames_per_burst, number_of_bursts, burst_period_in_frames):
        """Test for mode: "nibble4". Test that bits 3:0 of RAM samples are sent on even frames and bits 7:4 on odd frames.
            Test by injecting 0x10 into each sample of the buffer and by setting an odd burst_period_in frames and check that 
            the data values alternate between 0 and 1 between each frame"""
        #we want even frames to be 0b1111 and odd frames to be 0b1000. Send 0xF0 on each frame. Expected: alternating sample values of 1 and 0 depending on frame number.
        injected_data = np.full(self.SAMPLES_PER_FRAME, 0x10, dtype=self.FG_DTYPE)
        samples_to_compare = (number_of_bursts - 1) * frames_per_burst * self.SAMPLES_PER_FRAME
        data = self._set_capture_funcgen(
            'nibble4',
            period=None,
            frames_per_burst = frames_per_burst, 
            number_of_bursts = number_of_bursts, 
            burst_period_in_frames = burst_period_in_frames,
            buffer=injected_data,
        )
        data = data >> self.board.ADC_BITS_PER_SAMPLE - 4 # bits are aligned at 4MSBS of each sample
        second_frame = 0x0 if data[0] % 2 == 1 else 0x1
        pattern = np.tile(np.repeat(np.array([data[0], second_frame]), frames_per_burst * self.SAMPLES_PER_FRAME), (number_of_bursts + 2 - 1) // 2)
        expected_data = pattern[(frames_per_burst-1) * self.SAMPLES_PER_FRAME:len(pattern) - self.SAMPLES_PER_FRAME]
        expected_data[0] = data[0]
        expected_data[-1] = second_frame if number_of_bursts % 2 != 0 else data[samples_to_compare] #normalize until total samples because sometimes there is a latency for captured frame
        self.plot_and_test(xr, 
            data[:samples_to_compare], 
            expected_data[:samples_to_compare], #normalize until total samples because sometimes there is a latency for captured frame
            title=f"Frame Count with mode: nibble4 \n{frames_per_burst} frames per burst, {number_of_bursts} bursts and {burst_period_in_frames} frames between bursts",
            )

    def test_reset(self, xr):
        """Test that counters are reset after resetting funcgen"""
        logger = self.get_logger()
        injected_data = np.zeros(self.SAMPLES_PER_FRAME)
        self.board.set_channelizer(data_source='buffer', buffer=injected_data)
        logger.debug(f"Started data capture")
        self._set_capture_funcgen('buffer', buffer=injected_data)
        self.board.stop_data_capture()
        logger.debug(f"Stopped data capture")
        for chan in self.board.chan.values():
            chan.FUNCGEN.RESET = 1
            logger.debug(f"Channelizer {chan} reset")

        for chan in self.board.chan.values():
            assert chan.FUNCGEN.RAMP_CTR == 0
            assert chan.FUNCGEN.FRAME_CTR == 0
            assert chan.FUNCGEN.SEND_FRAME == 0
            assert chan.FUNCGEN.OVERFLOW_CTR == 0
            assert chan.FUNCGEN.OVERFLOW_CTR == 0

    def test_delay_capture(self, xr):
        """Test delay capture"""
        for chan in self.board.chan:
            chan.FUNCGEN.reset()
        self._set_capture_funcgen('ramp')
        delay_ctrs = [ch.FUNCGEN.DELAY_CTR for ch in self.board.chan.values()]
        assert(len(set(delay_ctrs)) == 1) #ensure all delay counters are the same
    
    def test_right_shift(self, xr, shift):
        """Test that the ouput is right shifted by specified bits then saturated at clip width"""
        logger = self.get_logger()
        injected_values = [1,-1, 8, -8]
        self.board.chan[0].FUNCGEN.CLIP_WIDTH = 15
        self.board.chan[0].FUNCGEN.SHIFT = shift
        for injected_value in injected_values:  
            injected_data = np.full(self.SAMPLES_PER_FRAME, injected_value, dtype=self.FG_DTYPE_SIGNED)
            expected_data = injected_data >> shift
            self.board.set_channelizer(data_source='buffer', buffer=injected_data, channels=[0, 1])
            data = self._set_capture_funcgen('buffer', buffer=injected_data, flatten=False)[0]
            self.plot_and_test(xr, 
                data, 
                expected_data, 
                title=f"Right shift by {shift} and with injected data {injected_value}",
                )

    def test_shift_clip_width(self, xr, shift, clip_width):
        """Test that the ouput is saturated at clip width"""
        logger = self.get_logger()
        max = 2 ** (self.board.ADC_BITS_PER_SAMPLE - 1) - 1
        min = - 2 **(self.board.ADC_BITS_PER_SAMPLE - 1)
        max_clipped = 2 ** clip_width - 1
        min_clipped = -2 ** clip_width
        injected_values = [max, min, 1, -1, max_clipped - 1, max_clipped + 1, min_clipped - 1, min_clipped - 2, min_clipped + 1]
        expected_values = []
        for value in injected_values:
            value = value >> shift
            if value >= max_clipped:
                expected_values.append(max_clipped)
            elif value <= min_clipped:
                expected_values.append(min_clipped)
            else:
                expected_values.append(value)
        self.board.chan[0].FUNCGEN.SHIFT = shift
        self.board.chan[0].FUNCGEN.CLIP_WIDTH = clip_width
        for injected_value, expected_value in zip(injected_values, expected_values):  
            injected_data = np.full(self.SAMPLES_PER_FRAME, injected_value, dtype=self.FG_DTYPE_SIGNED)
            expected_data = np.full(self.SAMPLES_PER_FRAME, expected_value, dtype=self.FG_DTYPE_SIGNED)
            self.board.set_channelizer(data_source='buffer', buffer=injected_data)
            data = self._set_capture_funcgen('buffer', buffer=injected_data, flatten=False)[0]
            self.plot_and_test(xr, 
                data, 
                expected_data, 
                title=f"Clip width saturation at bit {clip_width} and injected data {injected_value}",
                )

    @pytest.mark.funcgen_counters
    def test_overflow_ctr(self, xr, expected_overflows_per_frame, stats_frames, mode):
        """Test that overfow counter from funcgen works correctly by simulating ```expected_overflows_per_frame``` overflows per frame.
         Overflows can be simulated with frame8, frame4, and  word_ctr_buffer_flags by injecting '1's into the buffer.
         Each 1 injected in the buffer represents an overflow in the sample. The number of frames over which we count the number of overflows is
         specified in FUNCGEN.STATS_PERIOD which represents the desired frame period + 1. E.g. FUNCGEN.STATS_PERIOD = 11 => frame period of 10."""
        logger = self.get_logger()
        injected_data = np.zeros(self.SAMPLES_PER_FRAME)
        injected_data[:expected_overflows_per_frame] = 1 #inject ```expected_overflows_per_frame```` into buffer (sample with LSB = 1 -> overflow)
        self._set_capture_funcgen(mode, overflow_buffer=injected_data)
        for chan in self.board.chan.values():
            chan.FUNCGEN.reset()
            chan.FUNCGEN.STATS_PERIOD = stats_frames - 1
            chan.FUNCGEN.STATS_ENABLE = 1
        sleep(0.1)
        for chan in self.board.chan.values():
            assert chan.FUNCGEN.OVERFLOW_CTR == stats_frames * expected_overflows_per_frame          
