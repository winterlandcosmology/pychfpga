import logging
import numpy as np
from pychfpga.fpga_array import FPGAArray
import pytest
from pathlib import Path
from .caspersimtools import load_sim, load_input


class TestFFT:
    ICESIMDIR = Path(__file__).parent / "fft_sim_data" / "ICE"

    def get_logger(self):
        return self.logger
    
    def test_example(self):
        """
        Example script to read inputs and corresponding simulation data

        Currently I have generated frou different inputs:
        1) Constant - const_input.mat
        2) Sine wave - sine_100mhz_input.mat
        3) Low amplitude white noise - low_noise_input.mat
        4) High amplitude white noise - high_noise_input.mat
        """
        adc_input = load_input(self.ICESIMDIR / "sine_100mhz_input.mat")
        """
        This loads an HDF file into memory. It contains a group called 'data'
        with a @D matrix of dimensions (5, ...). The five arrays are the time axis
        (just a sequence of integers) and four parallel ADC inputs.
        
        adc_input.adc0 will returns data for the first ADC
        ...
        adc_input.adc3 will returns data for the fourth and last ADC
        
        The input data was generated for several frames in a row. If you want to
        write this input data to funcgen buffer for testing purposes, you should slice
        first 512 samples from each adc channel to get 2048 samples in total.

        Example:
        """
        fungen_input = np.empty(2048) # probably needs a proper dtype specification, like np.int8
        fungen_input[0::4] = adc_input.adc0[:512]
        fungen_input[1::4] = adc_input.adc1[:512]
        fungen_input[2::4] = adc_input.adc2[:512]
        fungen_input[3::4] = adc_input.adc3[:512]

        """
        The output simulation data is saved accordingly to the FFT block
        output. Well, sort of. FFt block has two outputs - out00 and out01.
        Both have a real and an imag numbers concantenated in each sample.
        In the simulation output they are saved separately: out00_real,
        out00_imag, out01_real, out01_imag. The 'load_sim' function automatically
        takes care of finding the first legit output sample by syncronizing
        with the output 'sync' signal. Optionaly, it can extract a specific
        number of simulation frames and unscramble the output. But it requires
        some extra parameters specified regarding FFT size.
        
        Example:
        """

        fft_sim = load_sim(
            path=self.ICESIMDIR / "sine_100mhz_fft29.mat",
            nframes=1,
            n_bit_fft=11,
            n_bit_parallel=2,
            unscramble=True,
            )

        """
        There are simulations for three different models: a full
        bitgrowth fft (called 'fft29'), a partial bitgrowth fft only
        until 25 bits with shift at every remaining stage (called 'fft25_1111')
        and a partial bitgrowth with no shifts ('fft25_0000').

        A file name for simulation output can be derived by combining the input name
        with the fft name, for example:
            - const_fft25_1111.mat
            - low_noise_fft29.mat
        etc.

        The fft output from the real board is already mixed together and goes like:
        [real0, imag0, real1, imag1, real2, imag2, ...]
        So odd indices hold real numbers and even indices hold imaginary numbers.
        You can get full real and imaginary data from simulation by running the following:
        """
        real_fft_sim, imag_fft_sim = remix_real_imag(fft_sim)

        """
        And then either compare 'real_fft_sim' to fft_frame[0::2] and 
        'imag_fft_sim' to fft_frame[1::2] (assuming 'fft_frame' is what 
        you got from the real board), or shuffle fft_simulation data
        together into as single array following the real ICE board indexing.
        """

