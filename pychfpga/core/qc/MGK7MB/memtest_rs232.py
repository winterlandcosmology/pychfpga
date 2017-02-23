#!/usr/bin/python

#Important: File added to /etc/udev/rules.d/99-FTDI-pllprog.rules
#File contains:
#SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6015", MODE="666"

#Seem to need to run this first atm
#sudo chmod a+rw /dev/ttyUSB0

import serial, time
import re
import sys


#code taken from https://github.com/psi46/elComandante/issues/14
#!/usr/bin/env python2
if sys.platform != 'win32':
    import pyudev
    context = pyudev.Context()

def device_by_id(id_vendor, id_product):
    match_devices = []
    for dev in context.list_devices(subsystem='tty'):
        try:
            ancestor = dev.parent.parent.parent
            attribs = ancestor.attributes
        except AttributeError:
                continue
        try:
            idVendor = attribs.get('idVendor')
            idProduct = attribs.get('idProduct')
        except KeyError:
            continue
        if idVendor == id_vendor and idProduct == id_product:
            match_devices.append(dev)
    return match_devices


def find_single_device(id_vendor, id_product):
    devs = device_by_id(id_vendor, id_product)
    if len(devs) == 0:
        msg = "Unable to find device with idVendor={} and idProduct={}"
        raise ValueError(msg.format(id_vendor, id_product))
    elif len(devs) > 1:
        msg = "Found multiple devices with idVendor={} and idProduct={}"
        raise ValueError(msg.format(id_vendor, id_product))
    else:
        return devs[0].device_node

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
def setup_serial(rs232dongle_dev):
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
    except Exception, e:
        print "error open serial port: " + str(e)
        exit()

    ser.flushInput() #flush input buffer, discarding all its contents
    ser.flushOutput()#flush output buffer, aborting current output 
    return ser

def init_rs232():
    if sys.platform == 'win32':
        rs232dongle_dev = find_dev_windows()
        assert rs232dongle_dev, "RS232 Dongle not found!"
    else:
        rs232dongle_dev = find_single_device('0403', '6015')
    ser = setup_serial(rs232dongle_dev)
    return ser , rs232dongle_dev

def interupt_boot(ser):

    time.sleep(0.5)  #give the serial port sometime to receive the data
    numOfLines = 0
    response = None

    while True:
      response = ser.readline()
      numOfLines = numOfLines +1
      m = re.search('stop', response)
      if (m != None ) or numOfLines>50:
        print("Board is booting and producing RS232 output")
        print("Found the stop spot, and halting boot process at TI-MIN# prompt")
        ser.write("stop\n")
        break

def start_memtest(ser, iterations=5):

    print("Initiating memory test")
    ser.write("mtest 84000000 84800000 0 %i\n" %iterations)
    numOfLines = 0
    testpassed = 0
    test_finished = 0
    while True:
      response = ser.readline()
      numOfLines = numOfLines +1

      #Print anything that has visible characters
      m = re.search('[0-9A-Fa-f]', response)
      if m != None:
         print response

      #Looking for test end condition
      endtest = re.search('Tested', response)

      if endtest!=None:
        test_finished = 1

        #Looking to see if it passed
        if (re.search('with 0 errors', response) != None):
          testpassed = 1 #

      #Time to exit this loop
      if numOfLines>50*iterations or test_finished:
        break


    return testpassed


def close_serial(ser):
    ser.close()