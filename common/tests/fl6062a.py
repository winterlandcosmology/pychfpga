#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
import time, socket
import numpy as np

class GPIB:
    """A class to communicate with instruments over GPIB using a Prologix
       ethernet-GPIB converter.  """

    def __init__(self, address=2, to=5, ip='192.168.0.37'):
      print "Initializing Connection..."
      self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 
                        socket.IPPROTO_TCP)
      self.sock.settimeout(to)
      self.sock.connect((ip, 1234))
      self.address = address
      self.sock.send("++mode 1\r") #switch prologix to controller mode
      self.sock.send("++auto 0\r") #set instrument to listen (1 is talk)
      self.sock.send("++eoi 1\r") #send EOI signal at end of command
      self.sock.send("++read_tmo_ms 3000\r") #read timeout in milliseconds
      self.change_address(self.address)
      print "Device Initialized"
  
    def change_address(self, address):
        self.address=address
        self.sock.send("++addr "+str(self.address)+"\r")
    
    
    def command(self, comstr):
        self.change_address(self.address)
        self.sock.send(comstr + ";\r")
    
    def read(self):
      self.sock.send("++read eoi\r")
      data=[]
      while True:
         try:
            data1 = self.sock.recv(1024)
            data.append(data1)
         except socket.timeout:
            break
      return ''.join(data)

    def read_all(self):
      self.sock.send("++read\r")
      data=[]
      while True:
         try:
            data1 = self.sock.recv(1024)
            data.append(data1)
         except socket.timeout:
            break
      return ''.join(data)

    def query(self, comstr):
        self.command(comstr)
        value = self.read()
        return value



def freq_sweep(start=10e6, stop=1600e6, numpts=1601, amplitude=-10, delay = 0, loops=1, address=2, ip='192.168.0.37'):
    """Perform an S parameter sweep on the HP8753E vector network analyzer if setParams is True,
     will actually change setting (start stop npoints power level).  Otherwise just gets data as system is setup"""
    signal_generator = GPIB(address, to=5, ip=ip)
    set_amplitude(amplitude, signal_generator)
    freqs = np.linspace(start,stop,num=numpts)
    for loop in xrange(loops):
      for freq in freqs:
        set_freq(freq,signal_generator)
        time.sleep(delay)
    return

def set_freq(freq=400e6, signal_generator=None, ip='192.168.0.37', address=2):
  if signal_generator is None:
    signal_generator = GPIB(address, to=5, ip=ip)
  signal_generator.command('FR'+str(freq)+'HZ')

def set_amplitude(amplitude=-10, signal_generator=None, ip='192.168.0.37', address=2):
  if signal_generator is None:
    signal_generator = GPIB(address, to=5, ip=ip)
  signal_generator.command('AP'+str(amplitude)+'DB')

if __name__ == "__main__":
   import sys
   freq_sweep()
   
   
