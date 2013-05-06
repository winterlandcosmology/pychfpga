#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301 

"""
ML605_PMBus.py module 
 Implements the interface to the ML605 PMBus devices 
 History:
    2012-04-11 JFC : Created from test code in FMC_EEPROM 
    2012-11-19 JFC: Fixed tabs. Overrode errors in PMBus access to allow the use of the TI Digital Fusion pod on the ML605.
"""

import numpy as np


class ML605_PMBus_base(object):
    def __init__(self,fpga,verbose=1):
        self.fpga_instance=fpga
        self.verbose=verbose

    def init(self):
        pass

    PMBus_commands={
        'READ_VOUT': (0x8b,'LINEAR16U'),
        'READ_VIN': (0x88,'LINEAR11'),
        'READ_IIN': (0x89,'LINEAR11'),        
        'READ_IOUT': (0x8C,'LINEAR11'),        
        'READ_PIN': (0x97,'LINEAR11'),        
        'READ_POUT': (0x96,'LINEAR11'),        
        #'READ_FREQUENCY': (0x95,'LINEAR11'),    # not supported    
        'READ_FAN_SPEED_1': (0x90,'LINEAR11'),        
        'FAN_CONFIG_1_2': (0x3A,'BYTE'),        
        'FAN_COMMAND_1': (0x3B,'LINEAR11'),        
        'READ_TEMPERATURE_1': (0x8D,'LINEAR11'),        
        'READ_TEMPERATURE_2': (0x8E,'LINEAR11'),        
        'IOUT_CAL': (0x38,'LINEAR11'), # mV/A        
        'TEMPERATURE_CAL_GAIN': (0xE4, 'LINEAR11'),
        'TEMPERATURE_CAL_OFFSET': (0xE5, 'LINEAR11'),
        'STATUS_WORD': (0x79, 'WORD'),
        'STATUS_CML': (0x7E, 'BYTE'),
        }

    def word_to_linear11(x):
        return float(((x&0x7ff)^0x400)-0x400)*(2**(((x>>11)^0b10000)-16))

    def linear11_to_word(f):
        if f==0:
            return 0
        mantissa_max=float((2**(11-1))-1)
        exp_max=(2**(5-1))-1
        e=int(np.ceil(np.log2(abs(f/mantissa_max))))
        e=min(exp_max,e);
        e=max(-exp_max-1,e);
        m=int(float(f)/(2**e));
        if m > mantissa_max or m < (-mantissa_max - 1):
            error()
        v=((e&0x1F)<<11) | (m & 0x7FF)
        return v

    PMBus_formats={
        'LINEAR16U': (2,lambda x: float(x)*(2**-12)),
        'LINEAR11': (2,word_to_linear11, linear11_to_word),
        'LINEAR11U': (2,lambda x: float((x&0x7ff)*(2**(((x>>11)^0b10000)-16)))),
        'WORD': (2,lambda x: x),
        'BYTE': (1,lambda x: x),
    }



    # ML605_rails={
        #Rail name, (nominal voltage, max current, controller, page, phase)
        # 'VCCINT_FPGA' :     (1.00, 20, 0, 0, 0),
        # 'VCC2V5_FPGA' :     (2.50, 20, 0, 1, 0),
        # 'VCCAUX     ' :     (2.50, 10, 0, 2, 0),
        # 'MGT_AVCC   ' :     (1.00, 6, 1, 0, 0),
        # 'MGT_AVTT   ' :     (1.20, 6, 1, 1, 0),
        # 'VCC_1V5    ' :     (1.50, 10, 1, 2, 0),
        # 'VCC_3V3    ' :     (3.30, 10, 1, 3, 0),
    # };
        
    def read_PMBus(self, controller, command, page=None, phase=None, raw=0, noerror = True, verbose = 0):
        """ Reads from the SMBus"""
        i2c = self.fpga_instance.I2C
        i2c_addr = 52 + controller
        i2c_port = 1

        f = self.PMBus_commands[command]
        command_code = f[0]
        format = f[1]
        conversion_fn = self.PMBus_formats[format][1];
        length = self.PMBus_formats[format][0];

        if page is not None:
            i2c.i2c_write(i2c_port, i2c_addr, [0x00, page], noerror=noerror) # sets the page
        if phase is not None:
            i2c.i2c_write(i2c_port, i2c_addr, [0x04, phase], noerror=noerror) # sets the page
        
        data = i2c.i2c_write_read(i2c_port, i2c_addr, [command_code], read_length=length, verbose=verbose, noerror=noerror) # sets the command
        #data=i2c.i2c_read(1,i2c_addr,length=2) # reads two bytes
        #print 'Raw value=',hex(data)
        if raw:
            return data
        if length == 2:
            data.dtype = np.dtype('<u2'); # lsb is sent first
        data = data[0];
        val = conversion_fn(data)
        return val

    def write_PMBus(self, controller, command, page, phase, value, noerror = True):
        """ Writes to the SMBus """
        i2c=self.fpga_instance.I2C
        i2c_addr=52+controller
        i2c_port=1

        f = self.PMBus_commands[command]
        command_code = f[0]
        format = f[1]
        conversion_fn = self.PMBus_formats[format][2];
        length = self.PMBus_formats[format][0];

        i2c.i2c_write(i2c_port, i2c_addr, [0x00, page], noerror=noerror) # sets the page
        i2c.i2c_write(i2c_port, i2c_addr, [0x04, phase], noerror=noerror) # sets the phase
        
        data=conversion_fn(value)
        if length==1:
            data=[data]
        elif length==2:
            data=[ (data & 0xff), (data>>8)] 
        print 'writing bytes:',data
        data=i2c.i2c_write(i2c_port, i2c_addr, [command_code] + data, noerror=noerror) # sets the command
        return

    def init_PMBus(self):
        # Not useful - Xilinx forgot to connect the temperature sense line to the power controller of the ML605
        self.write_PMBus(1,'TEMPERATURE_CAL_GAIN',page=0,phase=0, data=[32,235]) #100C/V
        self.write_PMBus(1,'TEMPERATURE_CAL_GAIN',page=1,phase=0, data=[32,235]) #100C/V
        self.write_PMBus(1,'TEMPERATURE_CAL_OFFSET',page=0,phase=0, data=[224,228]) #-50C
        self.write_PMBus(1,'TEMPERATURE_CAL_OFFSET',page=1,phase=0, data=[224,228]) #-50C


    def status(self):
        """ Reads power status from ML605 PMBus"""
        Ptotal=0
        noerror = 1
        print '--------------- ML605 Power Controller statistics ---------------'
    
        print '- Controller 0 (Core, Addr=52, U24) -'    
        print "      Controller temperature : %.2f C" %  (self.read_PMBus(0,'READ_TEMPERATURE_1', noerror=noerror))
        print "      Input voltage : %.2f V" %  (self.read_PMBus(0,'READ_VIN', noerror=noerror))

        vout=self.read_PMBus(0,'READ_VOUT',page=0,phase=0, noerror=noerror)
        iout=self.read_PMBus(0,'READ_IOUT', noerror=noerror)
        temp=self.read_PMBus(0,'READ_TEMPERATURE_2', noerror=noerror);
        Ptotal+=vout*iout    
        print "      VCCINT_FPGA (1.00V @ 20 A): %.2fV @ %.3fA = %.3fW, power stage temperature: %.2f C" % (vout,iout,vout*iout,temp)

        vout=self.read_PMBus(0,'READ_VOUT',page=1,phase=0, noerror=noerror)
        iout=self.read_PMBus(0,'READ_IOUT', noerror=noerror)
        temp=self.read_PMBus(0,'READ_TEMPERATURE_2', noerror=noerror);
        Ptotal+=vout*iout    
        print "      VCC2V5_FPGA (2.50V @ 20 A): %.2fV @ %.3fA = %.3fW, power stage temperature: %.2f C" % (vout,iout,vout*iout,temp)

        vout=self.read_PMBus(0,'READ_VOUT',page=2,phase=0, noerror=noerror)
        iout=self.read_PMBus(0,'READ_IOUT', noerror=noerror)
        temp=self.read_PMBus(0,'READ_TEMPERATURE_2', noerror=noerror);
        Ptotal+=vout*iout    
        print "      VCCAUX      (2.50V @ 10 A): %.2fV @ %.3fA = %.3fW, power stage temperature: %.2f C" % (vout,iout,vout*iout,temp)

        print '- Controller 1 (Aux, Addr=53, U25) -'    
        print "      Controller temperature : %.2f C" %  (self.read_PMBus(1,'READ_TEMPERATURE_1', noerror=noerror))
        print "      Input voltage : %.2f V" %  (self.read_PMBus(1,'READ_VIN', noerror=noerror))

        vout=self.read_PMBus(1,'READ_VOUT',page=0,phase=0, noerror=noerror)
        iout=self.read_PMBus(1,'READ_IOUT', noerror=noerror)
        Ptotal+=vout*iout    
        print "      MGT_AVCC    (1.00V @ 6 A): %.2fV @ %.3fA = %.3fW, power stage temperature: N/A" % (vout,iout,vout*iout)

        vout=self.read_PMBus(1,'READ_VOUT',page=1,phase=0, noerror=noerror)
        iout=self.read_PMBus(1,'READ_IOUT', noerror=noerror)
        Ptotal+=vout*iout    
        print "      MGT_AVTT    (1.20V @ 6 A): %.2fV @ %.3fA = %.3fW, power stage temperature: N/A" % (vout,iout,vout*iout)

        vout=self.read_PMBus(1,'READ_VOUT',page=2,phase=0, noerror=noerror)
        iout=self.read_PMBus(1,'READ_IOUT', noerror=noerror)
        Ptotal+=vout*iout    
        print "      VCC_1V5     (1.50V @ 10 A): %.2fV @ %.3fA = %.3fW, power stage temperature: N/A" % (vout,iout,vout*iout)

        vout=self.read_PMBus(1,'READ_VOUT',page=3,phase=0, noerror=noerror)
        print "      VCC_3V3     (3.30V @ 10 A): %.2fV @  N/A A = N/A W, power stage temperature: N/A" % (vout)

        print '- Total power: %.3f W' % Ptotal    
        print '-----------------------------------------------------------------'

