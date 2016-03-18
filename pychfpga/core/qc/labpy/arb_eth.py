import os.path
import sys
import array
import time
import socket


# Special characters to be escaped
LF   = 0x0A
CR   = 0x0D
ESC  = 0x1B
PLUS = 0x2B


#==============================================================================
def IsSpecial(data):
    return data in (LF, CR, ESC, PLUS)

    
#==============================================================================
def CheckError():
    sock.send("SYST:ERR?\n")
    sock.send("++read eoi\n")

    s = None

    try:
        s = sock.recv(100)
    except socket.timeout:
        s = ""

    print s


#==============================================================================
if __name__ == '__main__':

    if len( sys.argv ) != 4:
        print "Usage: ", os.path.basename( sys.argv[0] ), "<GPIB-ETHERNET IP address> <HP33120A GPIB address> <points>"
        sys.exit(1)

    # IP address of GPIB-ETHERNET
    ip = sys.argv[1]

    # HP33120A GPIB address
    addr = sys.argv[2]
    
    # Number of waveform data points
    points = int(sys.argv[3])
    
    # 8-16000 points required
    if points < 8:
        print "Too few points."
        exit(1)

    if points > 16000:
        print "Too many points."
        exit(1)

    try:
        # Waveform points are in short int (16-bit) array
        data = array.array('H');

        # Create waveform data. Simple ramp up and down.    
        for i in range(points/2):
            data.append((i * 2047)/(points/2))

        for i in range(points/2):
            data.append((((points/2)-i) * 2047)/(points/2))

        # Swap bytes so MSB is first. (Required on Windows)
        data.byteswap()

        # Output data is in byte array
        outdata = array.array('B');

        # Build output data, escaping all special characters        
        for byte in data.tostring():
            if IsSpecial(ord(byte)):
                outdata.append(ESC)
                
            outdata.append(ord(byte))                    

        # Open TCP connect to poet 1234 of GPIB-ETHERNET
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        sock.settimeout(0.1)
        sock.connect((ip, 1234))

        # Set mode as CONTROLLER
        sock.send("++mode 1\n")
        
        # Set HP33120A address
        sock.send("++addr " + addr + "\n")

        # Turn off read-after-write to avoid "Query Unterminated" errors
        sock.send("++auto 0\n")

        # Read timeout is 500 msec
        sock.send("++read_tmo_ms 500\n")

        # Do not append CR or LF to GPIB data
        sock.send("++eos 3\n")

        # Assert EOI with last byte to indicate end of data
        sock.send("++eoi 1\n")

        # Reset AWG 
        cmd = "*RST"
        print cmd
        sock.send(cmd + "\n")        

        time.sleep(1.0)
        CheckError()        

        # Format output data command. Use length of points array, NOT output array
        datalen = len(data) * 2
        
        cmd = "DATA:DAC VOLATILE, #" + str(len(str(datalen))) + str(datalen)
        print cmd
        
        # Increase host TCP timeout when sending large data
        sock.settimeout(10.0)

        # Write binary block data
        sock.send(cmd)
        sock.send(outdata.tostring())

        # Terminate ETHERNET data
        sock.send("\n")

        time.sleep(0.5)

        sock.settimeout(0.5)
        CheckError()        
        
        cmd = "DATA:COPY PULSE, VOLATILE"
        print cmd

        sock.send(cmd + "\n")
        time.sleep(2.0)     
        CheckError()        

        cmd = "FUNC:USER PULSE"
        print cmd

        sock.send(cmd + "\n")
        time.sleep(1)
        CheckError()        

        cmd = "FUNC:SHAP USER"
        print cmd

        sock.send(cmd + "\n")
        time.sleep(0.5)
        CheckError()        

        cmd = "OUTP:LOAD 50"
        print cmd

        sock.send(cmd + "\n")
        time.sleep(0.5)
        CheckError()        

        cmd = "FREQ 5000;VOLT 0.5"
        print cmd

        sock.send(cmd + "\n")
        time.sleep(0.5)
        CheckError()        

        sock.close()
        
    except socket.error, e:
        print e
        

    