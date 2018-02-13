Common errors
-------------


DONE did not raise.

	- invalid bitstream. Most likely because GIT LFS did not download the bistream and left the short ascii link in place of the real 15-Mbyte-ish bistream. Check your GIT LFS config.
	- power supply curren tlimit too low. The exess current caused by the FPGA being programmed caused this board (or other boards) to reboot. You still have Tuber comms with the board after programming, but the FPGA will not be programmed on some boatrds, which will be really confusing.
	-