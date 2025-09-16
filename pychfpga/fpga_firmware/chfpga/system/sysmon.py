#!/usr/bin/python

"""
SYSMON.py module

Implements the System Monitor/XADC interface

History:
    2011-07-08 : JFC : Created from test code in chFPGA.py
    2011-09-08 JFC : Improve display of status screen

Todo:
    2013-02-05: This code should be adapted to support the Kintex 7 XADC
"""
import logging

from ..mmi import MMI, BitField, CONTROL, STATUS, DRP


class SYSMON(MMI):

    ADDRESS_WIDTH = 12

    # DRP Registers
    TEMP_ADDR = 0x00
    TEMP_MIN_ADDR = 0x24
    TEMP_MAX_ADDR = 0x20

    VCCINT_ADDR = 0x01
    VCCINT_MIN_ADDR = 0x25
    VCCINT_MAX_ADDR = 0x21

    VCCAUX_ADDR = 0x02
    VCCAUX_MIN_ADDR = 0x26
    VCCAUX_MAX_ADDR = 0x22

    VAUX_VPVN_ADDR = 0x03
    VAUX_VREFP_ADDR = 0x04
    VAUX_VREFN_ADDR = 0x05

    VAUX_VOLT_ADDR = 0x1C
    VAUX_CURR_ADDR = 0x1D

    CONFIG1_ADDR = 0x40
    CONFIG2_ADDR = 0x41
    CONFIG3_ADDR = 0x42
    SEQ_ADC_SEL1_ADDR = 0x48
    SEQ_ADC_SEL2_ADDR = 0x49
    SEQ_ADC_AVG1_ADDR = 0x4A
    SEQ_ADC_AVG2_ADDR = 0x4B
    SEQ_ADC_MODE1_ADDR = 0x4C
    SEQ_ADC_MODE2_ADDR = 0x4D
    SEQ_ADC_ACQTIME1_ADDR = 0x4E
    SEQ_ADC_ACQTIME2_ADDR = 0x4F

    def __init__(self, *, router, router_port, verbose=1):
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)
        super().__init__(router=router, router_port=router_port)
        fpga = self.fpga
        self.supported_by_platform = fpga.PLATFORM_ID in (
            fpga._PLATFORM_ID_ML605,
            fpga._PLATFORM_ID_KC705,
            fpga._PLATFORM_ID_MGK7MB_REV0,
            fpga._PLATFORM_ID_MGK7MB_REV2,
            fpga._PLATFORM_ID_ZCU111,
            fpga._PLATFORM_ID_CRS,
            )

        self._lock()  # Prevent accidental addition of attributes

    def init(self):
        if self.supported_by_platform:
            # JFC: Fix until the CRS firmware has proper SYSMON RESET
            # self.fpga.GPIO.SYSMON_RESET=1
            # self.fpga.GPIO.SYSMON_RESET=0
            # self.fpga.GPIO.CTRL_RESET_TRIG=1
            # self.fpga.GPIO.CTRL_RESET_TRIG=0

            self.write_drp(self.CONFIG1_ADDR, 0x0000)
            self.write_drp(self.CONFIG2_ADDR, 0x0000)
            self.write_drp(self.SEQ_ADC_SEL1_ADDR, 0x3F01)  # Enable all ADC channels
            self.write_drp(self.SEQ_ADC_SEL2_ADDR, 0xFFFF)
            self.write_drp(self.SEQ_ADC_AVG1_ADDR, 0x3F01)  # All averaging
            self.write_drp(self.SEQ_ADC_AVG2_ADDR, 0xFFFF)
            self.write_drp(self.SEQ_ADC_MODE1_ADDR, 0x0000)  # All external channels set to single-ended
            self.write_drp(self.SEQ_ADC_MODE2_ADDR, 0x0000)
            self.write_drp(self.SEQ_ADC_ACQTIME1_ADDR, 0x0000)  # all set to normal acq time
            self.write_drp(self.SEQ_ADC_ACQTIME2_ADDR, 0x0000)
            self.write_drp(self.CONFIG1_ADDR, 0x3000)  # 256 averages
            self.write_drp(self.CONFIG2_ADDR, 0x2000)  # Enable ADC channel auto sequencing

    def temperature(self, addr=TEMP_ADDR):
        """ Reads a registers of the FPGA system monitor and convert the result in Celsius """
        lsb = self.read_drp(addr)
        temp = lsb / 64 * 503.975 / 1024. - 273.15
        return temp

    def voltage(self, addr, vref=3.0):
        """ Reads a registers of the FPGA system monitor and convert the result in Volts """
        lsb = self.read_drp(addr)
        volt = lsb / 64 * vref / 1024
        return volt

    def status(self):
        """
        Displays the  System Monitor (Virtex 6 / ML605) or XADC (Kintex 7) statistics

        Notes:
            - ADC measurement is always differential (P-N).
            - All external ADC signals acquired in unipolar mode since the
              differential voltages  are never negative. 0-1 V (differential)
              corresponds to full range 0-0x3FF.
            - VccINT and VccAUX voltages are measured internally. They have a
              gain of 1/3 before being fed to the ADC.
            - VccINT Current: Measures current sense resistor (0.005 ohm) on
              VP/VN
            - 12V Current:  Current sense resistor: 0.002 ohm (schematic is
              wrong, Hardware manual section 22 is right) , Amplifier gain
              (INA213): 50, Measured on Vaux<12>
            - 12V Voltage: Measured through a resistor divider (1/24) on Vaux<13>
        """
        if self.supported_by_platform:
            Vin = self.voltage(self.VAUX_VOLT_ADDR, vref=1.0) * 24
            Iin = self.voltage(self.VAUX_CURR_ADDR, vref=1.0) / (0.002 * 50)

            self.logger.info('%r: --- System Monitor statistics' % self.fpga)

            self.logger.info('%r:   Core Temperature:   %5.1f C (%.2f C min, %.1f C max)' % (
                self.fpga,
                self.temperature(self.TEMP_ADDR),
                self.temperature(self.TEMP_MIN_ADDR),
                self.temperature(self.TEMP_MAX_ADDR)))

            self.logger.info('%r:   VccINT Voltage:     %5.2f V (%.2f V min, %.2f V max)' % (
                self.fpga, self.voltage(self.VCCINT_ADDR),
                self.voltage(self.VCCINT_MIN_ADDR),
                self.voltage(self.VCCINT_MAX_ADDR)))

            self.logger.info('%r:   VccAUX Voltage:     %5.2f V (%.2f V min, %.2f V max)' % (
                self.fpga,
                self.voltage(self.VCCAUX_ADDR),
                self.voltage(self.VCCAUX_MIN_ADDR),
                self.voltage(self.VCCAUX_MAX_ADDR)))

            if self.fpga.PLATFORM_ID == self.fpga._PLATFORM_ID_ML605:
                self.logger.info('%r:   VccINT Current:     %5.2f A, (ADC input= %.2f mV' % (
                    self.fpga,
                    self.voltage(self.VAUX_VPVN_ADDR, vref=1.0) / 0.005,
                    self.voltage(self.VAUX_VPVN_ADDR, vref=1.0) * 1000))

                self.logger.info('%r:   12V Supply Voltage: %5.2f V (ADC input=%.2f V )' % (
                    self.fpga,
                    Vin,
                    self.voltage(self.VAUX_VOLT_ADDR, vref=1.0)))

                self.logger.info('%r:   12V Supply Current: %5.2f A (ADC input=%.2f V )' % (
                    self.fpga,
                    Iin,
                    self.voltage(self.VAUX_CURR_ADDR, vref=1.0)))
                self.logger.info('%r:   12V Power         : %5.2f W ' % (self.fpga, Vin * Iin))

            self.logger.info('%r:   VREFP Voltage:      %5.2f V' % (
                self.fpga,
                self.voltage(self.VAUX_VREFP_ADDR)))
            self.logger.info('%r:   VREFN Voltage:      %5.2f V' % (
                self.fpga,
                self.voltage(self.VAUX_VREFN_ADDR)))

        else:
            self.logger.debug('%r: SYSMON is not supported on this platform' % self.fpga)
