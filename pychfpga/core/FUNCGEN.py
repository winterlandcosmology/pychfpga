"""
FUNCGEN.py module
 Implements interface to the internal function generator

History:
    2012-10-01 JFC : Created from SRCSEL.py
    2012-10-17 JFC: Sets ramp as default function
"""

from Module import Module_base, BitField
import numpy as np

class FUNCGEN_base(Module_base):
    """ Implements interface to the function generator within a procecessor
    pipeline"""
    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Memory-mapped register definition
    RESET            = BitField(CONTROL, 0x00, 7, doc='Resets this module')
    # USE_OVERFLOW     = BitField(CONTROL, 0x00, 6, doc="when '1', overflow flags are generated when the outputs is 0x7F or 0x80")
    ENABLE           = BitField(CONTROL, 0x00, 6, doc="doc")
    RAM_PAGE         = BitField(CONTROL, 0x00, 3, width=3, doc="Selects which 512-byte page of RAM we are writing into")
    FUNCTION         = BitField(CONTROL, 0x00, 0, width=3, doc="Selects the source of the signal to be generated")
    BYTE_A           = BitField(CONTROL, 0x01, 0, width=8, doc="Byte A to be used by the function generator")
    BYTE_B           = BitField(CONTROL, 0x02, 0, width=8, doc="Byte B to be used by the function generator")
    NUMBER_OF_FRAMES = BitField(CONTROL, 0x03, 0, width=8, doc="Number of frames to send. If 0, send continuously.")

    RAMP_CTR   = BitField(STATUS, 0x00, 0, width=8, doc="Last 8 bits of the ramp counter (for debuging)")
    FRAME_CTR  = BitField(STATUS, 0x01, 0, width=8, doc="Frame counter")
    SEND_FRAME = BitField(STATUS, 0x02, 0, doc="debug")
    DELAY_CTR = BitField(STATUS, 0x04, 0, width=16, doc="Debug: Delay counter")


    FN_ADC = 0
    FN_BUFFER = 1
    FN_NOISE = 2
    FN_WORD_CTR = 3
    BUFFER_SIZE = 2048  # bytes
    FRAME_SIZE = 2048 # bytes

    DATA_SOURCE_NAMES = {
        'adc':                   FN_ADC,  # Sends the ADC data
        'noise':                 FN_NOISE,  # Uniform white noise generator
        'word_ctr_buffer_flags': FN_WORD_CTR,  # 32-bit Frame/word counter, with ADC overflow from bit 0 of buffer bytes
        'buffer':                FN_BUFFER,  # Sends the data stored in the buffer
        'funcgen':               FN_BUFFER,  # Sends the data stored in the buffer (for backwards compatibility)
        }

    FUNCTION_NAMES = {  # key : (function number, buffer generator fn)

        # The following are
        'arb':            (0, lambda data, N=BUFFER_SIZE: data),  # Arbitrary waveform stored in buffer
        'a':              (1, lambda a, N=BUFFER_SIZE: np.tile(np.uint8(a), N)),  # All bytes are Byte A
        'b':              (2, lambda b, N=BUFFER_SIZE: np.tile(np.uint8(b), N)),  # All bytes are Byte B
        'ab':             (3, lambda a, b, N=BUFFER_SIZE: np.tile(np.array([a, b], np.uint8), N / 2)),  # Bytes alternate between A and B.
        'ramp':           (4, lambda N=BUFFER_SIZE: np.arange(N, dtype=np.uint8)),  # Successive bytes generate a repeating ramp from 0 to 255.
        'real_ramp':      (5, lambda N=BUFFER_SIZE: (np.arange(N / 2) << 8).astype('>u2').view(np.uint8)),  # Generates the ramp: 0,0,0,1,0,2,0,3,0... If the data is read as (8+8)-bit complex value pairs, we obtain (0,0j), (1+0j)... (255+0j)
        '4bit_ramp':      (6, lambda N=BUFFER_SIZE: np.arange(N, dtype=np.uint8) << 4),  # Generates the ramp 0x00, 0x10, 0x20, ... 0xF0.
        '4bit_real_ramp': (7, lambda N=BUFFER_SIZE: (np.arange(N / 2) << 12).astype('>u2').view(np.uint8)),  # Generates the ramp: 0x00, 0x00, 0x10, 0x00, 0x20, 0x00 ... 0xF0, 0x00
         # '4bit_split_ramp': (0, FN_BUFFER, ),  # Generates 0x0000, 0x0010, 0x0020, .. 0x00F0, 0x1000, 0x1010 ...
        'sin':            (8, lambda freq=1, N=BUFFER_SIZE: (np.sin(np.arange(N)*2*np.pi/N*freq) * 127).astype(np.uint8)),  # Generates the ramp: 0x00, 0x00, 0x10, 0x00, 0x20, 0x00 ... 0xF0, 0x00
        }

    buffer_cache = None

    def __init__(self, fpga_instance, base_address, instance_number):
        # self.ant = ant_ch_instance
        # fpga = ant_ch_instance.fpga
        super(self.__class__, self).__init__(fpga_instance, base_address, instance_number)
        self._lock() # Prevent accidental addition of attributes (if, for example, a value is assigned to a wrongly-spelled property)
    # Specialized functions

    def reset(self):
        """ Resets the function generator"""
        self.pulse_bit('RESET')

    def set_data_source(self, source_name, data=None, seed=None):
        """
        Selects the source of the data outputed by the function generator: ADC signal, noise generator, frame counters, waveform buffer.

        If the noise generator is selected, the seed can be specified as as 15-bit value in ``seed``.

        If ``data`` is specified, its values are written in the buffer.

        You may need to sync after changing the data source between the 'adc'
        and the other internally-generated sources as packet transmission
        might be interrupted and might confuse the downstream logic (FFT,
        crossbars, packet aligner etc.)
        """
        if source_name not in self.DATA_SOURCE_NAMES:
            raise Exception('Invalid data source name')

        if seed is not None:
            self.BYTE_A = seed & 0xff
            self.BYTE_B = (seed >> 8) & 0xff | 0x80  # Set bit 7 to indicate unknown waveform

        if data is not None:
            self.set_function('arb', data=data)

        self.FUNCTION = self.DATA_SOURCE_NAMES[source_name]

    def get_data_source(self):
        """
        Gets the data source currently selected by the the SOURCE selector.
        """
        data_source_number = self.FUNCTION  # make sure we read this only once
        return [key for (key,value) in self.DATA_SOURCE_NAMES.items() if value == data_source_number][0]

    def set_function(self, function_name, **kwargs):
        """
        Sets the waveform buffer with a predetermined waveform.

        Keyword arguments are passed directly to the function that generate
        the buffer data. The argument 'a' and 'b' can be specified for
        waveforms that require them. ``data`` is used to specify an arbitrary
        waveform as 2048 bytes .
        """
        if function_name not in self.FUNCTION_NAMES:
            raise Exception("Invalid function name. Valid ones are '%s'" % ', '.join(self.FUNCTION_NAMES.keys()))
        (fn_number, buffer_gen) = self.FUNCTION_NAMES[function_name]
        buffer_info = '%s(%s)' % (function_name, ', '.join('%s=%.30r' % (arg, val) for (arg,val) in kwargs.items()))
        self.set_buffer(buffer_gen(**kwargs), function_number=fn_number, info=buffer_info)

    def get_function(self):
        """
        Return the name of the current waveform generated by the function generator.
        """
        (fn_number, info, crc) = self.get_buffer_info()
        fn_names = [key for (key, (number, _)) in self.FUNCTION_NAMES.items() if number == fn_number]
        if not fn_names:
            return 'Unknown'
        else:
            return fn_names[0]

    def set_buffer(self, data, function_number=0, info='Arbitrary data'):
        data = np.array(data, np.uint8)
        self.buffer_cache = data

        for page in range(4):
            self.RAM_PAGE = page
            self.write_ram(0, data[page * 512: (page + 1) * 512])
        self.RAM_PAGE = 4
        self.write_ram(0, function_number)
        self.write_ram(1, info + chr(0))

    def get_buffer(self, use_cache=True):

        if use_cache and self.buffer_cache is not None:
            return self.buffer_cache
        data = np.zeros(self.BUFFER_SIZE, np.uint8)
        for page in range(4):
            self.RAM_PAGE = page
            data[page * 512: (page + 1) * 512] = self.read_ram(0, length=512)
        return data

    def get_buffer_info(self):
        self.RAM_PAGE = 4
        data = self.read_ram(0, length=512)
        fn_number = data[0]
        data_str = data[1:].tostring()
        info = data_str[:data_str.index(chr(0))]
        return (fn_number, info, None) # Fn number, info string, CRC32

    def get_sim_output(self, adc_input=None, source=None, number_of_frames=4):
        """
        Return the simulated output of this module.

        Parameters:
            ``adc_input`` is a (adc_flags, adc_dat) tuple containing:

            adc_flags: Numpy array of (Nframes x 512) integers values
                indicating overflow condition for each sample of each word in bits
                3:0. Bit 3 is for earliest sample of the word.

            adc_data: Numpy array of (Nframes x 512) Big endian 64-bit words
                (dtype='>u4') contained the adc samples packed in words. Bits
                31-24 if for sample 0, bits 23-15 bit sample 1 etc.

        Returns:
            A tuple of two 512 elements vector (flags, data)
            flags:
                Bit 0 : Adc overflow flag for the word (set to 1 if any of the four samples in the word has overflown).
                Bits 1-3:'0'.
            data: Array of big-endian 64-bit words containing the data.
        """
        if self.RESET:
            raise RuntimeError('Module is in reset')

        if source is None:
            source = self.get_data_source()

        if source == 'adc':
            if adc_input is None:
                raise RuntimeError('Need to supply ADC input vector')
            (adc_flags, adc_data) = adc_input
            cumulative_adc_flags = np.cumsum(adc_flags.astype(bool), axis=-1).astype(bool)
            flags = (cumulative_adc_flags << 0).astype(np.int8)
            return (flags, adc_data)
        elif source == 'noise':
            flags = np.zeros((number_of_frames, self.FRAME_SIZE), np.int8)
            data = np.tile(np.random.random_integers(-128, 127, size=self.FRAME_SIZE).astype(np.uint8).view('>u4'), (number_of_frames, 1))
            return (flags, data)
        elif source == 'buffer':
            flags = np.zeros((number_of_frames, self.FRAME_SIZE), np.int8)
            data = np.tile(self.get_buffer(use_cache=True).view('>u4'), (number_of_frames, 1))
            return (flags, data)
        else:
            raise RuntimeError('Unknown or unsupported data source')


    def init(self):
        """ Initializes the function generator """
        if not self.fpga.is_fmc_present_for_channel(self.instance_number):
            self.set_data_source('buffer') # use the dunction generator if the ADC is not present
            self.set_function('ramp')
        else:
            self.set_data_source('adc') # use the ADC data

    def status(self):
        """ Displays the status of the function generator module """
        print '-------------- ANT[%i].FUNCGEN STATUS --------------' % self.instance_number
        print ' Function number: %i' % self.FUNCTION
        print ' Ramp counter status:'
        print '    RAMP_CTR: %i' % self.RAMP_CTR


