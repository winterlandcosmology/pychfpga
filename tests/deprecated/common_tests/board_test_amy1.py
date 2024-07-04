import pylab
import subprocess
import sys
execfile("top_test.py")
c.ADC_BOARD.FMC_EEPROM.write(0,13)

sys.stdout = open("c:\\Users\\Winterland\\pychfpga\\ADC_SN0042_test_log.txt", "a")
#sys.stdout = open("c:\\Users\\Winterland\\pychfpga\\common\\tests\\ADC_SN0008\\ADC_SN0008_test_log.txt", "a")


execfile("top_test.py")



# = open("c:\\Users\\Winterland\\pychfpga\\common\\tests\\ADC_SN0008\\ADC_SN0008_test_log.txt", "a")
#c.ADC_BOARD.ADC[0].get_temperature()
#x = float(c.ADC_BOARD.ADC[0].get_temperature())
#f.write("the temperature of ADC 0 is  %f" %x + "\n")

#c.ADC_BOARD.ADC[1].get_temperature()

#y = float(c.ADC_BOARD.ADC[1].get_temperature())
#f.write("the temperature of ADC 1 is %f" %y + "\n")

#f.close()
#sys.exit()
