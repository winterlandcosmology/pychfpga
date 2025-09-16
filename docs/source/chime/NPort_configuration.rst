====================
Moxa NPort5110 Setup
====================

Note: This process is for Windows only. Feel free to edit this doc for Linux
or MacOS as needed.

This is a quick documentation for setting up the Moxa NPort 5110 Serial Device Server
used to communicate with the GPS. The NPort must be connected to the local network
(e.g. connected through an ethernet switch).


1) Once connected and powered, you should see a green LED turn on for "Ready" and
"Link" (it is ok if "Link" blinks a little).


2) Next, go to the following link:

	https://www.moxa.com/en/products/industrial-edge-connectivity/serial-device-servers/general-device-servers/nport-5100-series#resources

which will list all the relevant support software for the NPort 5100 series. Download
the driver titled:

	NPort Administration Suite for Windows 7 and Windows Server 2008 R2, or later

which supports up to Windows 10. Once downloaded, follow the instructions of the
installer and run the program.


3) Once open, you should see a table listing NPort devices. Make sure you're
under "Configuration" in the "Function" section on the left. If you don't see anything,
click "Search" in the upper left of the screen, and it should be able to find your device.


4) Once it has found your device, you'll see that its IP address is listed as the
default IP address assigned by the manufacturer. To get it on your own network,
right click on your device, and select "Unlock". This will prompt you to enter
the password that should be provided by the manufacturer somwhere in the device
manual. Once entered, it will unlock your device, and you can change it properties.

	For Moxa devices, the password will probably just be "moxa"

Right click on your device again and select "Configure". This will open a new
dialog. Navigate to the "Network" tab. You will see three boxes that say "Modify".
Select the second, which will allow you to change the IP configuration. Change this
from "Static" to "DHCP". Click "Ok" at the bottom to implement the changes.

Your device should now be assigned an IP address on your local network.


5) Now you'll want to assign your device a COM port. Go to "COM Mapping" under
"Function". Click "Add" in the upper left of the window. A new dialog should pop
up. If you don't see your device here, you can click Scan (Search), and it should
pop up. Click "Ok" to add it.

Right-click on your device and select "COM Settings". It may or may not automatically
assign a COM port to your device. If not, check your device manager and make sure to
select a COM port that is not currently in use. Go to the "Serial Parameters" tab and
set the baud rate to 9600. Click "Ok" to save your changes.

If all is well, you should see your device pop up in the device manager, and you're
now connected and ready to go!