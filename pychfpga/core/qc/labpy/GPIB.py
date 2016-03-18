#!/Library/Frameworks/EPD64.framework/Versions/Current/bin/python
import socket
import time
import __main__
#import serial # need to install pySerial package
#import numpy as np

class GPIBException(Exception):
    pass

class GPIB(object):
        """
        A class to communicate with instruments over LAN or over GPIB using a Prologix ethernet-GPIB converter.

        All commands to the prologix controller and to the GPIB instrument end by CR or LF.
        Commands destined to the Prologix controller start with '++'.
        Other commands are sent to the instrument through GPIB.
        In this case, the CR or LF is removed from the command and the terminator set with the set_terminator() method is appended to the string.

        interface:
            'lan': direct lan interface at specified 'ip_addr' and 'ip_port'
            'prologix_eth': GPIB connection through an ethernet-based Prologix adapter on 'ip_port' at GPIB address 'gpib_addr'
            'prologix_usb': GPIB connection through an USB-based Prologix adapter on serial port 'usb_port' at GPIB address 'gpib_addr'
        """
        GPIBException = GPIBException
        def __init__(self, interface=None, gpib_addr=None, ip_addr=None,  ip_port = 1234, usb_port=None, timeout=0.5):
                #self.interface = interface
                self.ip_addr = None
                self.ip_port = None
                self.sock = None # IP socket, if used
                self.ser = None # USB serial link, if used
                self.address = gpib_addr
                self.use_eoi = True
                self.adapter_timeout = timeout
                self.instrument_timeout = 8 # This has to be long enough to let the instrument respond even when it is busy sweeping etc.
                self.default_timeout = 0.5
                self.use_prologix = False
                self.is_gpib = False
                self.use_wai_after_query = False  # Issue a *WAI command after each query
                self.is_dummy = False
                # Make sure there is a session-wide list of opened sockets so
                # we can close sessions that were previously opened in
                # previous runs in the same interactive session.
                if not hasattr(__main__,'_opened_sockets'):
                    __main__._opened_sockets = {}

                print '--------------------'
                if interface == 'dummy':
                        self.is_dummy = True
                        print "Using dummy instrument"
                elif interface == 'lan':
                        self.use_prologix = False
                        self.is_gpib = False
                        self.ip_addr = ip_addr
                        self.ip_port = ip_port

                        print "Initializing direct LAN Connection at IP address %s..." % (self.ip_addr)
                        old_sock = __main__._opened_sockets.get((self.ip_addr, self.ip_port), None)
                        if old_sock:
                            print 'Reusing previously-opened socket'
                            self.sock = old_sock
                        else:
                            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                            self.sock.settimeout(timeout)
                            self.sock.connect((self.ip_addr, self.ip_port))
                            __main__._opened_sockets[(self.ip_addr, self.ip_port)] = self.sock

                elif interface in ('eth', 'prologix_eth'):
                        self.use_prologix = True
                        self.is_gpib = True
                        self.ip_addr = ip_addr
                        self.ip_port = 1234
                        print "Initializing Connection to Prologix interface at %s:%i and GPIB address %i..." % (self.ip_addr, self.ip_port, self.address)
                        old_sock = __main__._opened_sockets.get((self.ip_addr, self.ip_port), None)
                        if old_sock:
                            print 'Reusing previously-opened socket'
                            self.sock = old_sock
                        else:
                            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                            self.sock.settimeout(timeout)
                            self.sock.connect((self.ip_addr, self.ip_port))
                            __main__._opened_sockets[(self.ip_addr, self.ip_port)] = self.sock

                elif interface in ('usb', 'prologix_usb'):
                        print "Initializing Connection to Prologix interface over USB port %s..." % (usb_port)
                        self.use_prologix = True
                        self.is_gpib = True
                        self.ser = serial.Serial(usb_port, 921600, timeout=timeout) ##GPIB usb converter 57600
                        #serPort could be 'COM3' or '/dev/tty***' or '/dev/cu***'depending on windows/linux/osx
                        self.ser.rtscts  = False
                        self.ser.xonxoff = False
                        self.ser.flushOutput()
                        self.ser.flushInput()
                else:
                        raise Exception('Unknown interface \'%s\' ' % interface)

                try:
                        self.flush_interface()
                        if self.use_prologix:
                            self.write("++mode 1\n") #switch prologix to controller mode
                            self.write("++auto 0\n") #disable automatic switching of the instrument into talk mode after a command is sent. We will send ++read commands manually.
                            self.write("++eoi 1\n") #send EOI signal at end of command
                            self.write("++eot_enable 0\n") #make sure the prologix does not add anything to the strings coming from the GPIB when the EOI is received. The USB interface had this enabled by default, which causes failure of the recognition of end-of-line
                            self.write("++read_tmo_ms %i\n" % (self.adapter_timeout*1000)) #read timeout in milliseconds
                            #self.write("++clr\r")
                            self.change_address(self.address)
                            print "Prologix adapter initialized.  Version ", self.read_version()
                            pass
                        #print 'Flushing the buffers...'
                        #self.flush(timeout=0.5)
                except:
                        # self.close()
                        raise

        def __enter__(self):
                return self

        def __exit__(self, etype, einst, etraceback):
                self.close()

        # Low-level data interface to the prologix adapter
        def close(self):
            """
            Closes the Ethernet or USB communication ports to the Prologix adapter.
            """
            if self.sock:
                    print 'Closing IP socket'
                    self.sock.shutdown(socket.SHUT_RDWR);
                    self.sock.close()
                    self.sock = None
            if self.ser:
                    print 'Closing USB serial socket'
                    self.ser.close()
                    self.ser = None

        def set_timeout(self, timeout):
            if self.sock:
                self.sock.settimeout(timeout)

        def get_timeout(self):
                if self.sock:
                        return self.sock.gettimeout()
                else:
                        return 2


        def flush_interface(self, timeout=0.1):
            """
            Flushes the buffers to and from the Prologix interface
            """
            if self.sock:
                self.sock.settimeout(timeout)
                while True:
                        try:
                                self.sock.recv(16384)
                        except socket.timeout:
                                break
            if self.ser:
                self.ser.flushOutput()
                self.ser.flushInput()

        def send(self, string):
                """
                Low-level command that sends a string to Ethernet or USB port to which the Prologix interface is attached.
                 The string is sent as-is
                """
                if self.sock:
                    self.sock.send(string)
                elif self.ser:
                    self.ser.write(string)

        def recv(self):
                """
                Low-level command that receives a string from the interface (Ethernet or USB) to which the Prologix interface is connected.
                """
                if self.sock:
                        return self.sock.recv(16384)
                elif self.ser:
                        return ''.join(self.ser.readlines())
                else:
                        raise GPIBException('Tried to read from a dummy interface')

        def write(self, string):
                """
                Sends a string to the prologix interface.
                The string is sent as-is (no terminator is added or removed by this function, although the Prologix controller will replace CR and/or LF bu the specified GPIB terminator.)
                """
                self.send(string)

        def read(self, wait_for_eoi=None, terminator='\n', timeout=None, verbose=0):
            """
            Reads data from the GPIB instrument until the terminator character is encounter.
            'terminator' determines when the buffer is stopped being read
                terminator ='' stops reading as soon as the first ethernat packet is Received
                terminator = char stops when 'char' is encountered *at the end of the received ethernet packet*
                terminator = 'timeout' stops when the timeout period has expired and rises no timeout Exception

            """
            if self.use_prologix:
                    #print '      Reading data...'
                    if wait_for_eoi is None:
                            wait_for_eoi = self.use_eoi

                    # Request read from the Prologic adapter
                    if wait_for_eoi:
                         self.write("++read eoi\r")
                    else:
                         self.write("++read\r")

            if timeout is None:
                    timeout = self.default_timeout

            timeout = max(timeout, self.adapter_timeout+0.1) # make sure we wait at least long enough for the adapter to give up

            old_timeout=self.get_timeout()
            self.set_timeout(timeout)

            data=[]

            while True:
                 try:
                        data1 = self.recv()
                        data.append(data1)
                        if verbose:
                                print '      Received %i bytes: %s...' % (len(data1), ''.join(data1[:128]))
                        if terminator == '':
                                break
                        elif not len(data1):
                                raise socket.timeout # the serial port returns
                        elif data1[-1]==terminator:
                                break
                        print '.',
                 except socket.timeout:
                        if verbose:
                                print '      Reached the timeout period'
                        if terminator != 'timeout':
                                raise
                        break
            # restore timeout value
            #if timeout is not None:
            self.set_timeout(old_timeout)
            return ''.join(data)


        def local(self):
                """ Goes back to local mode """
                if self.use_prologix:
                    self.write('++loc\n')
                else:
                    raise RuntimeError("The 'local' command is supported only when using a GPIB bus")

        def device_clear(self):
                """ Asserts the device clear GPIB line"""
                if self.use_prologix:
                    self.write('++clr\n')


        def read_serial_poll(self, timeout = 8):
                """ Goes back to local mode """
                if self.use_prologix:
                    self.write('++spoll\n')
                    self.set_timeout(timeout)
                    status_string = self.recv()
                    return int(status_string)

        def read_version(self, timeout = None):
                if self.use_prologix:
                    self.write('++ver\n')
                    if timeout is not None:
                            self.set_timeout(timeout)
                    self.set_timeout(timeout)
                    return self.recv()



        def change_address(self, address):
             if self.use_prologix:
                 # print 'Using address %i' % self.address
                 self.address = address
                 self.write("++addr %i\r" % self.address)

        def set_gpib_terminator(self, terminator):
                """
                Sets the terminator that is appended at the end of strings sent to the GPIB instrument.
                The original CR or LF received by the Prologix interface are stripped replaced by the specified terminator character.
                """
                if self.use_prologix:
                    terminator_codes = {
                            '\r\n' : 0, # CR + LF
                            '\r' : 1, # CR only
                            '\n': 2, # LF only
                            '': 3, # No terminator
                            }

                    if terminator not in terminator_codes:
                            raise Exception('Invalid termination code')
                    self.write('++eos %i\n' % terminator_codes[terminator])

        ###################################
        # Basic GPIB commands
        ###################################

        def command(self, comstr):
                """
                Sends a command to the GPIB instrument. The terminator is added automatically.
                """
                if self.use_prologix:
                    self.change_address(self.address)
                self.write(comstr + "\n")

        def flush(self, timeout=0.5):
                """
                Flushes the GPIB Instrument output buffers. The instrument is asked to talk until no data is sent and there is a timeout.
                """
                # Flush the socket buffer first
                self.flush_interface()
                # Now read the instrument until no more data comes
                while self.read(terminator='timeout', timeout=self.adapter_timeout+0.1, verbose=0):
                        pass

        def query(self, command, **kwargs):
                """
                Sends a command to the instrument and returns the reply string without the terminator or trailing spaces.
                """

                self.command(command)
                if self.use_wai_after_query:
                    self.command('*WAI')
                #time.sleep(0.01)
                reply_string = self.read(**kwargs)
                return reply_string.rstrip() # remove trailing spaces or CR or LF

        def query_float(self, *args,  **kwargs):
                reply_string = self.query(*args, **kwargs)
                return float(reply_string)

        def query_int(self, *args,  **kwargs):
                return int(self.query_float(*args, **kwargs))



        TimeoutException = socket.timeout
