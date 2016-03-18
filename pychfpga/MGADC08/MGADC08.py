#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
MGADC08.py module
 Wrapper object for MGADC08 FMC ADC board

 History:
 2011-07-25 : JFC : Created
"""
# MGADC08 FMC ADC board device handlers
import logging
import numpy as np
import time
import struct
import zlib
import ast
from datetime import datetime

from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship, backref

from pychfpga.core.icecore import FMCMezzanine
from pychfpga.core.icecore import FMCMezzanineHandler
from pychfpga.core.icecore.hw.ipmi_fru import FRU, Board, Product, MultiDict

# Import mezzanine-specific modules
import ADC
import IOExpander
import ADC_PLL
import AmbTemp
import MGT_PLL

class MGADC08_base(FMCMezzanine):
    """ Implements the object that exposes the MGADC08 FMC ADC board hardware ressources"""
    handler_name = 'MGADC08_Handler'
    __ipmi_part_number__ = 'MGADC08'  # Must match part number in IPMI data. Used for auto-discovery.

    __tablename__ = 'mgadc08'
    __mapper_args__ = {'polymorphic_identity': 'MGADC08'}
    _pk = Column(Integer, ForeignKey('fmc_mezzanines._pk'), primary_key=True)

    @classmethod
    def decode_eeprom(cls, eeprom_data):
        """ Parses the mezzanine EEPROM data into a IPMI structure. Returns None if the EEPROM is not formatted is a recognized format"""
        logger = logging.getLogger()
        if ord(eeprom_data[0]) == 0x0d:  # if this is McGill format
            # # Read the eeprom block by block until we detect the end of the
            # # dictionary
            # block_size = 32
            # string = ''
            # for i in range(512 / block_size):  # read 32 blocks of 16 bytes
            #     data_block = self._mezzanine_eeprom_read(
            #         mezzanine, addr=i*block_size, length=block_size, retry=retry)
            #     string += data_block
            #     if ('}' in data_block) or (chr(255) in data_block):
            #         break

            last_char = eeprom_data.find('}')
            if last_char < 0:
                logger.error(
                    '%.32r: The Mezzanine EEPROM indicated McGill-style data but no valid dictionary found on EEPROM. Did the board pass '
                    'the quality control tests?' % cls)
                return None

            data_string = eeprom_data[1:last_char+1]  # keep only the dict definition data_string: remove first char (board ID) and stop at last '}'.

            # Read checksum
            crc_string = eeprom_data[last_char+1: last_char+1+4]
            crc = struct.unpack('i', crc_string)[0]
            computed_crc = zlib.crc32(data_string)
            if computed_crc != crc:
                raise RuntimeError(
                    'FMC EEPROM CRC is invalid. Read crc = %08X, '
                    'computed crc = %08X' % (crc, computed_crc))
            dict_out = ast.literal_eval(data_string)  # safer than using eval

            # Extract standard FRU information from McGill data structure
            part_number = dict_out.pop('Model', 'MGADC08')
            serial_number = dict_out.pop('Serial #', 'Unknown')
            product_version = dict_out.pop('Rev #', 'Unknown')
            mfg_date_str = dict_out.get('Date of last test', None)
            try:
                mfg_date = datetime.strptime(mfg_date_str, '%d/%m/%Y')
            except ValueError:
                mfg_date = None

            return FRU(
                board=Board(
                    mfg_date=mfg_date,
                    manufacturer="Winterland",
                    product_name="McGill Mezzanine",
                    part_number=part_number,
                    serial_number=serial_number,
                    fru_file="",
                ),
                product=Product(
                    manufacturer="Winterland",
                    product_name="McGill Mezzanine",
                    part_number=part_number,
                    product_version=product_version,
                    serial_number=serial_number,
                    asset_tag="",
                    fru_file="",
                ),
                multi=MultiDict(dict_out)
            )
        # if this not the McGill Format, we assume try the standard IPMI
        try:
            return FRU.decode(eeprom_data)
        except ValueError:
            return None

class MGADC08_Handler(FMCMezzanineHandler):

    __handler_for__ = MGADC08_base

    # SPI port numbers specific to this board
    SPI_ADC0_ADDR      = 0    # ADC. R/W device. 8 bit address+RW, 16 bit data.
    SPI_ADC1_ADDR      = 1  # ADC. R/W device. 8 bit address+RW, 16 bit data.
    SPI_ADC0_TEMP_ADDR = 2  # ADC temperature sensor chip. Read only
    SPI_ADC1_TEMP_ADDR = 3  # ADC temperature sensor chip. Read only
    SPI_AMB_TEMP_ADDR  = 4  # Board temperature sensor chip. Read/Write device
    SPI_PLL1_ADDR      = (5, 1)  # ADC PLL. The second element of the tuple indicates that we use the alternate timing
    #SPI_ADC_BIAS_ADDR =5 # Bias measurement ADC.  Read/Write device # Not present on Rev2 board
    SPI_IO_EXP_ADDR    = 6  # IO Expander. Read/Write device
    SPI_PLL2_ADDR      = 7  # MGT PLL. Write only.

    _board_is_present = False # Will be checked later

    def __init__(self, **kwargs):
        super(MGADC08_Handler, self).__init__(**kwargs)
        # self.type = 'mgadc08'
        self._board_is_present = None
        self.sampling_frequency = None
        self.reference_frequency = None
        # self._board_info = {}

        #self.check_FMC_presence()
        self.verbose = False
        self._board_is_present = True

        self.motherboard = self.iceboard
        self.fmc_number = self.mezzanine-1

        if self.is_present():
            # self._board_info = self.load_board_info()
            self.logger.debug('  - ADC')
            self.ADC = ADC.ADC_base(adc_board=self)
            self.logger.debug('  - IOExpander')
            self.IOExpander = IOExpander.IOExpander_base(adc_board=self)
            self.logger.debug('  - ADC_PLL')
            self.ADC_PLL = ADC_PLL.ADC_PLL_base(adc_board=self)
            self.logger.debug('  - AmbTemp')
            self.AmbTemp = AmbTemp.AmbTemp_base(adc_board=self)
            self.logger.debug('  - MGT_PLL')
            self.MGT_PLL = MGT_PLL.MGT_PLL_base(mezz=self)

    ############################################
    # Methods available to the board hardware
    #############################################

    def spi_read_write(self, device, data,  type=np.uint8, verbose=None):
        """
        Provides read/write function to access the SPI devices on this board.
        """
        if verbose is None:
            verbose = self.verbose
        return self.iceboard.SPI.read_write(device=device, data=data, type=type, port=self.mezzanine-1, verbose=verbose)

    def set_spi_reset(self, state):
        """ Resets the SPI device on *BOTH* mezzanines.
        """
        self.iceboard.SPI.RESET = state

    def spi_reset(self):
        """ Resets the SPI device on *BOTH* mezzanines.
        """
        self.iceboard.SPI.pulse_bit('RESET')

    def adc_reset(self):
        self.iceboard.GPIO.pulse_bit(('ADC0_RESET', 'ADC1_RESET')[self.mezzanine-1])

    REFCLK_SOURCES = {
        'fmc': 0,
        'sma': 1
        }


    def set_refclk_source(self, source):
        if source not in self.REFCLK_SOURCES:
            raise ValueError("Invalid Reference clock source name '%s'. Valid sources are: %s" % (source, ', '.join(self.REFCLK_SOURCES.keys())))
        self.IOExpander.REFCLK_INPUT_SEL = self.REFCLK_SOURCES[source]

    def get_refclk_source(self):
        source = self.IOExpander.REFCLK_INPUT_SEL
        return [k for k,v in self.REFCLK_SOURCES.items() if v == source][0]

    def is_adc_pll_locked(self):
        """ Check is the Mezzanine's ADC PLL is locked *By looking at the PLL_LOCK line on the FMC connector*.
        """
        gpio = self.iceboard.GPIO
        if self.mezzanine == 1:
            return gpio.ADC_PLL_LOCK0
        elif self.mezzanine == 2:
            return gpio.ADC_PLL_LOCK0
        raise RuntimeError('Invalid mezzanine number')

    def adc_sync(self):
        self.iceboard.REFCLK.local_sync()

    def set_power(self, state):
        self.iceboard.set_mezzanine_power(bool(state), self.mezzanine)

    def check_FMC_presence(self, verbose=0):
        """ Checks if the FMC is present"""
        # self.logger.debug("Attempting to read FMC eeprom to determine board presence")
        # data = self.eeprom.read(0, length=1, noerror=True, verbose=verbose)
        # self.logger.debug("FMC eeprom returned the value: %i", data[0])
        self._board_is_present = self.iceboard._get_mezzanine_type(self.mezzanine) == self.polymorphic_identity
        #self.logger.info("is the ADC board present: %i" % self._board_is_present)

    def is_present(self):
        """ returns a boolean indicating whether the ADC board is present"""
        return self._board_is_present

    def init(self, sampling_frequency=800e6, reference_frequency=10e6, verbose=0):
        """ Initializes the FMC board modules"""

        if self.is_present():
            self.sampling_frequency = sampling_frequency
            self.reference_frequency = reference_frequency
            self.logger.info('Initializing MGADC08 on Mezzanine %i' % self.mezzanine)
            self.logger.debug('  - AmbTemp')
            self.AmbTemp.init()

            self.logger.debug('  - IOExpander')
            self.IOExpander.init()

            self.logger.debug('  - ADC_PLL')
            self.ADC_PLL.init(fout=2*self.sampling_frequency/1e6, fref=self.reference_frequency/1e6, verbose=verbose)

            self.logger.debug('  - ADC')
            self.ADC.init()

    def status(self):
        """ Displays the status of the ADC board"""
        self.logger.info('Status of MGADC08 ADC board on Mezzanine %i' % self.mezzanine)
        self.logger.info('  ADC board is %s' % (('not present', 'present')[bool(self.is_present())]))
        if self.is_present():
            self.AmbTemp.status()
            self.IOExpander.status()
            self.ADC_PLL.status()
            self.ADC.status()

    # Old method used to QC the original boards
    def load_board_info(self):
        """ loads the info data block from the ADC board EEPROM into memory for future access. """
        i = 0
        string = ''
        keep_reading = True
        number_of_tries = 0
        while(keep_reading):
             try:
                  ascii = self.eeprom.read(i)
                  keep_reading = False
             except:
                  number_of_tries += 1
                  if number_of_tries > 100:
                      print "something wrong with eeprom reading"
                      raise
                  print 'e',
                  time.sleep(0.01)
        #125 is the ASCII character for the } which is used in the dictionary. The 1000 characters is used to make sure this doesn't go indefinitely
        #Converts each address in EEPROM to a character and put it together in a string
        dictionary_is_present = False
        for i in range(500):
            if (ascii != 125) and (ascii != 255):
                keep_trying = True
                number_of_tries = 0
                while(keep_trying):
                    try:
                        ascii = self.eeprom.read(i)
                        keep_trying = False
                        print '.',
                    except:
                        number_of_tries +=1
                        if number_of_tries > 100:
                            print "something is wrong with eeprom read"
                            raise
                        time.sleep(0.01)
                        print 'e',
                char = chr(ascii)
                string = string + char
            elif ascii == 125:
                dictionary_is_present = True
                break
        dictbyte = ''
        if dictionary_is_present == True:
            print "Now reading checksum"
            for i in range(4): #reads 4 bytes after dictionary
                keep_trying = True
                number_of_tries = 0
                while(keep_trying):
                    try:
                        asciibyte = self.eeprom.read(len(string)+1+i)
                        keep_trying = False
                        print '.',
                    except:
                        number_of_tries +=1
                        if number_of_tries > 100:
                            print 'Something is wrong with eeprom read'
                            raise
                        time.sleep(0.01)
                        print 'e',
                byte_char = chr(asciibyte)
                dictbyte = dictbyte + byte_char
            crccheck = struct.unpack('i',dictbyte)
            # Doesn't actually do the crc check yet.....
            #print 'The dictionary stored on EEPROM is:      ' + str(string)
            #print 'The CRC library check is:     ' + str(crccheck)
            exec_string = "dict_out = " + string[1:]
            exec exec_string
        elif dictionary_is_present == False:
            print 'No dictionary found on EEPROM. Did the board pass the quality control test?'
        self._board_info = dict_out


    # Old method used to QC the original boards
    def write_board_info(self, dict = None ):
            """
            Makes a dictionary or accepts dictionnary as input. Writes a dictionary to the EEPROM and also a CRCheck.
            """
            # Get dicitonary from user if none supplied
            if dict == None:
                ser_num = raw_input("Enter the serial number of the board (e.g. 0001):      ")
                rev_num = raw_input("Enter the revision number of the board (e.g. 0): ")
                fab_run = raw_input("Enter the fabrication run of the board (e.g. 1): ")
                head_ver = raw_input("Enter the header version (e.g. 1): ")
                delay_tab = raw_input("Enter the delay table of the board: ")
                model = raw_input("Enter the model of the ADC (e.g. MGADC08):      ")
                stat = raw_input("Enter the status of the board (0 = Working, 1 = In QC , 2 = Has problems but works, 3 = Failed): ")
                board_date = raw_input("Enter the date the last test was done (DD/MM/YYYY): ")
                site = raw_input("Enter the URL to find all the tests associated with this board: ")
                comments = raw_input("Enter any additional comments you may have about the board. If none, please put 'None': ")
                dict = {'Serial #': ser_num, \
                        'Rev #': rev_num, \
                        'Fabrication Run': fab_run, \
                        'Header Version': head_ver, \
                        'Model': model, \
                        'Delay Table': delay_tab, \
                        'Status': stat, \
                        'Date of last test': board_date, \
                        'Website': site, \
                        'Comments': comments}

            # Parse dictionnary into ASCII char list
            nstring = str(dict)
            chars = list(nstring)
            # Write dictionnary to EEPROM, checking each byte after write
            for i in range(len(chars)):
                correct = False
                while not correct:
                    try:
                        check = self.eeprom.read(i+1)
                        if (check == ord(chars[i])):
                            correct = True
                        else:
                            try:
                                self.eeprom.write(i+1, ord(chars[i]))
                            except Exception as e:
                                print self.logger.info('error writing to EEPROM, will retry: ' + e.message)
                                pass
                    except Exception as e:
                        print self.logger.info('error reading from EEPROM, will retry: ' + e.message)
                        pass
            # Write 13 at the end of EEPROM
            thirteen = False
            while (thirteen == False):
                try:
                    self.eeprom.write(0,13)
                    thirteen = True
                except Exception as e:
                    print self.logger.info('error writing 13 to EEPROM, will retry: ' + e.message)
                    pass
            # Figure out the CRC and write to EEPROM
            crcheck = zlib.crc32(nstring)
            dictbyte = struct.pack('l', crcheck)
            asciibyte = struct.unpack('BBBB', dictbyte)
            print 'the crcheck is:' + str(crcheck)
            #for i in range(len(asciibyte)):
            i=0
            while (i < len(asciibyte)):
                try:
                    self.eeprom.write(len(chars)+1+i, asciibyte[i])
                    i+=1
                except Exception as e:
                    self.logger.info("error writing CRC, will retry: " + e.message)
                    pass

            # Read full EEPROM dictionary and print to log
            self.load_board_info()
            self.logger.info("Done. Read back EEPROM: \n" + str(self._board_info))
