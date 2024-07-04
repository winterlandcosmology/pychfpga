import pylab
import sys
execfile("top_test.py")
c.ADC_BOARD.FMC_EEPROM.write(0,13)


execfile("top_test.py")



f = open("c:\\Users\\Winterland\\pychfpga\\common\\tests\\ADC_SN0037\\ADC_SN0037_test_log.txt", "a")
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
c.ADC_BOARD.ADC[0].get_temperature()
x = float(c.ADC_BOARD.ADC[0].get_temperature())
f.write("\n the temperature of ADC 0 is  %f" %x + "\n")
x = float(c.ADC_BOARD.ADC[0].get_temperature())
f.write("the temperature of ADC 1 is %f" %x + "\n")
f.close()
