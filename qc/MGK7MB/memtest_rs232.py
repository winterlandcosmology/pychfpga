#!/usr/bin/python

#Important: File added to /etc/udev/rules.d/99-FTDI-pllprog.rules
#File contains:
#SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6015", MODE="666"

#Seem to need to run this first atm
#sudo chmod a+rw /dev/ttyUSB0
#code taken from https://github.com/psi46/elComandante/issues/14

import time
import re
import sys
import os
import subprocess

# Pypi packages
import pyftdi.serialext

import serial
# import serial.tools.list_ports

class MemTestRS232:

    # @staticmethod
    # def find_single_device(hwids):
    #     """ Find a device whose hardware ID that contains one of the strings isted in ``hwhids``. hwids typically contains the VendorID and ProductID in the form 'aaaa:bbbb' 
    #     """

    #     devs = []
    #     for dev in serial.tools.list_ports.comports():
    #         for hwid in hwids:
    #             if hwid in dev.hwid:
    #                 devs.append(dev.device) 

    #     # devs = device_by_id(id_vendor, id_product)
    #     if len(devs) == 0:
    #         raise ValueError(f"Unable to find device with id cointaining {hwids}")
    #     elif len(devs) > 1:
    #         raise ValueError(f"Unable to find device with id cointaining {hwids}")
    #     else:
    #         return devs[0]

    def find_dev_windows():
        import serial.tools.list_ports as listports
        ports = list(listports.comports())
        for p in ports:
            hwid = p.hwid
            if 'VID:PID=0403:6015' in hwid:
                port = p
        if port is not None:
            return port.device


    #initialization and open the port

    #possible timeout values:
    #    1. None: wait forever, block call
    #    2. 0: non-blocking mode, return immediately
    #    3. x, x is bigger than 0, float allowed, timeout block call
    def setup_serial(self, rs232dongle_dev):
        ser = serial.Serial()
        ser.port = rs232dongle_dev
        ser.baudrate = 115200
        ser.bytesize = serial.EIGHTBITS #number of bits per bytes
        ser.parity = serial.PARITY_NONE #set parity check: no papython serity
        ser.stopbits = serial.STOPBITS_ONE #number of stop bits
        #ser.timeout = None          #block read
        ser.timeout = 1            #non-block read
        #ser.timeout = 2              #timeout block read
        ser.xonxoff = False     #disable software flow control
        ser.rtscts = False     #disable hardware (RTS/CTS) flow control
        ser.dsrdtr = False       #disable hardware (DSR/DTR) flow control
        ser.writeTimeout = 2     #timeout for write

        try:
            ser.open()
        except Exception as e:
            print("error open serial port: " + str(e))
            raise

        ser.flushInput() #flush input buffer, discarding all its contents
        ser.flushOutput()#flush output buffer, aborting current output
        return ser

    def __init__(self, ftdi_url='ftdi://ftdi:232h/1'):
        if sys.platform == 'win32':
            dev = find_dev_windows()
            assert dev, "RS232 Dongle not found!"
            ser =  serial.Serial(dev, 115200, timeout=1)
        else:
            # dev = self.find_single_device(['0403:6015', '0403:6014'])

            #Modifying permisions of USB RS232 device if needed
            #Would request password
            # stat_command = ['stat', '-c', '%a', rs232dongle_dev]
            # x = int(subprocess.check_output(stat_command))
            # if x != 777:
            #     print("\nNeed to change the permissions of %s. Please enter password when asked." %rs232dongle_dev)
            #     os.system('sudo chmod 777 /dev/ttyUSB*')

            # ser =  serial.Serial(dev, 115200, timeout=1)
            ser = pyftdi.serialext.serial_for_url(ftdi_url, baudrate=115200)
            dev = ftdi_url
        # ser = setup_serial(rs232dongle_dev)
        self.ser = ser
        self.dev = dev

    def interupt_boot(self):

        #time.sleep(0.5)  #give the serial port sometime to receive the data
        numOfLines = 0
        response = None

        while True:
          print('.', end='')
          response = self.ser.readline()
          numOfLines = numOfLines +1
          m = re.search(b'stop', response)
          if (m != None ):
            print("\nBoard is booting and producing RS232 output")
            print("Found the stop spot, and halting boot process at TI-MIN# prompt")
            self.ser.write(b"stop\n")
            break
          if numOfLines>50:
            print("Didn't find the TI-MIN# prompt")
            raise Exception("Didn't find the TI-MIN prompt!")


    def start_memtest(self, iterations=5):

        print("Initiating memory test")
        self.ser.write(b"mtest 84000000 84800000 0 %i\n" %iterations)
        numOfLines = 0
        testpassed = 0
        test_finished = 0
        while True:
          response = self.ser.readline()
          numOfLines = numOfLines +1

          #Print anything that has visible characters
          m = re.search(b'[0-9A-Fa-f]', response)
          if m:
             print(response.decode('ascii', errors='ignore').strip())

          #Looking for test end condition
          endtest = re.search(b'Tested', response)

          if endtest:
            test_finished = 1

            #Looking to see if it passed
            if (re.search(b'with 0 errors', response)):
              testpassed = 1 

          #Time to exit this loop
          if numOfLines > 50*iterations or test_finished:
            break


        return testpassed


    def close(self):
        self.ser.close()


