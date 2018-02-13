Quick start
===========

Accessing the servers
---------------------

Access to the various machines is usually made through ``ssh``.

You can use passwords to authenticate, but that quickly becomes tedious. Some SSH clients help a lot by remembering passwords automatically (like MobaXterm), but it won't necessarily work when you do a double ssh, which is usually the case when accessing from outside DRAO. The best approach is to use SSH keys.

use ``ssh-keygen`` to generate a pair of public and private keys. The super secret private key shall stay with you, on a trusted machine, and not liberally distributed across machines. This is the key to the kingdom. If it falls in wrong hands, intruders can access and wreak havoc to the CHIME machines.

To use the ssh key to their full convenience and secirity, you should use an ssh agent on you local, trusted machine, which will hold your private keys and will provide them on demand to any ssh clients when needed so they never have to be stored there. Also,  if you use agent-forwarding, all the ssh session and sub-sesssions (including git) that are started from another ssh session will have access to the agent that runs on your trusted machine. In other words, your private key files  never leave your trusted machine.

Many ssh clients software you use on you trusted work machine offer key management. On Windows, you can use Paegent to load and provide keys to Putty ssh sessions, or use MobaXterm which will also automatically load and offer your keys to any ssh sessions started with it. If you run from a linux machine,  you can start the agent with:

	eval `ssh-agent` # if not already running
	ssh-add ~/.ssh/myprivatekey # to add your key


If a server is not already set-up to recognize your key, you do:

	ssh-copy-id user@host

This will allow *all* the keys loaded in the ssh-agent (as listed by ssh-add -L) to authenticate on the target account. So if you use ``ssh-copy-id``, make sure you have loaded only the keys you want to propagate. And don't worry! Only the public key will actually be sent by ``ssh-copy-id``. The private key loaded in ssh-agent embeds an copy of the public key, and it is only the public part that will be sent over and appended to the target account's ``~/.ssh/authorized_keys``.

Then you can log directly to any client  with:

    ssh -A user@host

Repeat the ssh-copy-id step from there until you have  configured all the accounts on all the servers you use.

The `-A` indicated you want to forward (make available) your agent within your new ssh session. This means that other ssh sessions, ssh-copy-id  or even git will be able to use your keys automatically, even if you ar etwo or three ssh sessions deep.

If you use git on bitbucket, you will want to log in on their web site and install your public key so git will be able to authenticate to the server.

You can automate a double-ssh to get from the outside world:

ssh -A user@liberty ssh -A chime@carillon

te first -A allows the ssh to carillon to use the key from your trusted machine's agent, and the -A in the ssh to carillon ensures that git push and pulls on carillon will have their bitbucket keys from there as well.




Programming the ARM firmware
----------------------------


The ARM processor firmware includes the Linux operating system, a filesystem loaded with all the files and executables required to run the OS, the web server and and the IceBoard-specific software  that handles its hardware and provides the corresponding methods over a HTTP-based interface.

The ARM firmware resides in the SD card that is inerted in each IceBoard, and the board boots locally from there. (There is an experimental network booting mode, which is not used for CHIME).

The SD card image is built on a linux machine, and is then copied to the SD cards.

It takes about 50 seconds to program a card using a fast SD card reader/writer (or the one on your PC), but that is tedious for a large array. For this reason, an experimental (and very hacky) remote, parallel programming method has been devised.

To use this programming method, you first need an image compressed in bzip2 format (typically with a ``.bz2`` extension). Uncompressed images, or images compressed in another format are not currently accepted.

The programming is done  in an interactive ipython session.

  - cd to the ch_acq folder
  - Start ipython

Then create an array of boards you whish to reprogram by using the :mod:`fpga_array` command-line interface. For example, if you want to load all the boards that are specified in the configuration ``jfc.crate0`` (define din ``config.yaml``), you would do::

  run -i pychfpga/fpga_array -y jfc.crate0 --prog 0 --open 0

This will create an array of boards accessible through variable `ca`. `ca.ib` is the collection of all IceBoards in the array. As a convenience, just ``ib`` will give you this array directly.

The ``--prog 0`` ``--open 0`` arguments override the config so to prevent `fpga_array` from programming the FPGAs or attempt to open communication with them.

This is just one example on how you select a collection of boards. You could also specify them directly by IP address, or, if mDNS is working, through the IceBoard or backplane serial numbers.

Once you have an array, you can check the current firmware version with::

	ib.get_build_info().icecore_git_hash

This returns a tuple with many fields describing various build informatio elements. To get a specific field from that array::

	ib.get_build_info().icecore_git_hash

You can then program the SD cards by invoking the :meth:`FPGAController._update_arm_firmware()` method as follows::

	ib._update_arm_firmware(path_to_firmware_image_file)

Since ``ib`` is a `Ccoll`-type collection, and the ``_update_arm_firmware`` is designed to be asynchronous, the method will be run in parallel (concurrently) for all of the boards in the array.

The method will:

   - open a ssh shell to copy the image to a temporary RAM disk on the arm
   - copy the image to the SD card (while the OS is still running from that OS -- that is the hacky part. But apparently, it works).
   - wait for 120 seconds to make sure the write buffers are flushed and writing to the SD card is completed.

Once this is done, you need to power-cycle the boards, and thhey should boot with the new firmware.

Warning: Do not interrupt this process. This will corrupt the SD card and the boards won't boot again. Always have an evergency SD card image and a SD card programmer handy on site in case things go wrong.



Configuring and operating the Agilent5700/8700 power supplies
-------------------------------------------------------------

We use the Agilent 5764A (20V, 76A) to power the FPGA crates and the Agilent 8731A (8V, 400A) to power the FLAs (low noise pre-amp, and filtered post-amplifiers).

When a new power supply unit is installed, it should be configured with a default voltage and current limit that will persist even after power failures. We need to set these important parameters manually, using the front panel or remotely using the Python object. For safety reasons, `ch_master` does not set these parameters automatically, as a simple configuration error could destroy hundred of thousands of dollars of equipment.


.. warning:: Make sure you connect to the right power supply!

Open an interactive ipython session, from the ``ch_acq`` folder::

	from ps import AgilentN5700 # load the class that can handle both the 5700 and 8700 series
	ps = AgilentN5700('10.0.0.40') # yoy can also use the hostname.In any case, make sure you connect to the right unit!
	ps.unlock()  # disable software protection (This protection is implemented by the instance; it does not affect the instrument in any way)
	ps.configure_power_on_state(voltage=6.2, current=360) # Sets the voltage setpoint and current limit.

The `configure_power_on_state` first disables the power supply output and sets the voltage setpoint and maximum current, and also ensures that those parameters will be reloaded when the power to the unit is re-established. The power supply output is left disabled  after the command.

Before enabling the supply, you can check the voltage and current limit settings with::

	ps.get_voltage_setting()
	ps.get_current_limit()

The power supply can be powered on and off with::

	ps.unlock() # if was not done before on this instance
	ps.power_on()
	ps.power_off()

The status of the supply and the current voltage and current can be displayed with::

	ps.status()

Once you enable the supply, the status() method can be used to confirm that the output voltage is as desired. The current will be whatever the load uses, not the limit (hopefully).


Running the array
-----------------

This section describe how to run the CHIME Array.

Before you begin, make sure all the system and Python packages have been installed, as per the instruction in :doc:`installation`.



Resetting a single board
------------------------

Sometimes, when you initialize an array, a FPGA board (more precisely, its ARM processor) will fail to respond to pings. Unless there is a bad networking conneciton, this is because the board has not booted properly. This board needs to be rebooted. To solve this issue, the whole crate can be powercycled, but it is also possible to reboot a single board in the crate. This is explained here.

``ch_master`` or ``fpga_array`` will report the list of missing boards with as much information as it has on the identity of that board (hostname/IP address of the board,  serial number,  and (crate_number, slot_number) tuple). You can use the network map spreadsheed to find out in which crate and slot the board is located if that information is not preseted. Note that the (crate,slot) tuple use zero-based slot numbers.

To reset the board, launch an ``ipython`` session and use fpga_array.py to create an array consisting of the crate that has the problematic board. There are various methods to select the boards (the IP address of a known working board in the crate, and if mDNS is working on that machine, the serial numbe rof the board or the crate). Here, we'll use the boards defines in a pre-existing array in the config file::

	run -i pychfpga/fpga_array.py -y jfc.crate7 --open 0 --prog 0 --ignore_missing_boards

We use the ``--open 0 --prog 0`` to override the config parameters and make sure the FPGAs are not initialized. We just want to talk to the ARM processors. The option ``--ignore_missing_boards`` is needed to prevent from fpga_array from raisong an exception if the problematic board is covered by the list.

When the array is created, select a working board in the same crate::

	i = ib[0]

``ib`` was defined by ``fpga_array.py`` script as a list of all selected boards. We just take the first one in the list.
To reset the board, on slot N (one-based slot number), we do::

	i.set_power_on_slot(N,False) # power off slot N
	i.set_power_on_slot(N,True) # power on slot N

After some delay (up to 60 seconds), the board will boot up and will respond to pings.


Accessing the Weather server
----------------------------

In CHIME, the ``wview`` service that gathers DRAO weather station data runs on ``marimba``. Since `weather.py` needs to access the database file produced by this service, our server mush run on the same machine.

``marimba``'s Python has been configured to all users can run the server. However, it is common practice to run all experiment process as the chime user, and within a GNU ``screen``. This allows multiple users to connect and re-connect simulatenously and to ensure the process continues in case of disconnections (which happens regularly if you are connecting from the outside). The default ``screen`` name is ``ch_acq``.

To access an existing (or create a new) weather ``screen`` from ``liberty``/``tubular`` ::

   ssh -A chime@marimba -t "screen -c ~/.screenrc_ch_acq -xRS ch_acq"

Or from the outside world via ``tubular`` (192.139.21.135)::

   ssh -A jfcliche@192.139.21.135 -t "ssh -A chime@marimba -t \"screen -c ~/.screenrc_ch_acq -xRS ch_acq\""

This assumes you have ssh-agent running on your work terminal and which is loaded with a key that match one of those listed in ``~/.ssh/authorized_keys`` on ``tubular`` and ``marimba``, or there are ``~/.ssh/config``  that indicate how to authenticate to these machines. The ``ssh -A`` option allows the ssh-agent keys running on your work computer to be accessible from the ssh sessions, so your super-secret private key does not have to be on any of the servers.


The ``screen`` options specify: ``-R``: resume an existing screen, ``-x``, allow commection to an already attached screen (multi-user); and ``-S ch_acq``: if no screen exist, create a new screen named ``ch_acq``, using the configuration specified by ``-c ~/.screenrc_ch_acq``.


The server is normally running in the weather.py tab (:kbd:`Ctrl-A 1`).  The ``.screenrc_ch_acq`` config creates this tab by default if a new screen is created.


Starting the Weather server
---------------------------

If the server is not already running, it can be started with::

	cd ~/git/ch_acq
	./weather.py jfc.drao

Tip: ``.inputrc`` is configured so you can just conveniently type "cd [up-arrow]" or "./ [up arrow]" to search for these  commands from the history


Manually interacting with the weather server
--------------------------------------------

If you need to confirm you are getting weather, you can manually print the latest weather data from an ``ipython`` session::

	cd ~/git/ch_acq
	ipython
	import weather
	print weather.get_wview_metrics()

Checking the GPS Serial to Ethernet adapters
--------------------------------------------

These are Startech NETRS232 units. You can check if they work properly by pinging them, e.g.::
	ping 10.0.0.10

or by opening a browser page at http://10.0.0.10

Sometimes, a NETRS232 stops working and needs to be power-cycled.

You can check if a unit has open ports with::

	nmap -sV -Pn 10.0.0.10  # -Pn says to ignore pings and test the ports anyway

Which will yield a result such as::

	Nmap scan report for 10.0.0.11
	Host is up (0.0097s latency).
	Not shown: 995 closed ports
	PORT     STATE SERVICE    VERSION
	80/tcp   open  tcpwrapped
	99/tcp   open  metagram?
	1001/tcp open  tcpwrapped
	5000/tcp open  upnp?
	6000/tcp open  X11?

We expect port 80 (web interface), TCP 1001 (data port), 6000 (Control port)

Configuring the GPS Ethernet adapters
-------------------------------------

Use manufacturer procedure to configure the networking of the Startech NETRS323 device to DHCP. That might require the unit to be connected directly to an ethernet port configured with the default networking parameters, or use the Windows program (?).

Once you have an IP address, over the web interface, set the following:

	- System Status:

		- Nickname: gps-east or gps-west

	- TCP mode:

		- Telnet Server/client: Server (Necessary, otherwise even simple tcp connection to the data port does not work)
		- reverse telnet: off
		- CLI Mode: NOT enabled
		- Data port Number: 1001 (important. This is what the config file assumes)
		- Control protocol:
		- Remote IP server address: not used
		- Client mod einactive timeout: (unused)
		- Server mode protect timeout: 1 minute (default 60 minutes)

	- UDP Mode: disabled, since TCP mode is active

	- UART:

		- RS232, 9600, 8 , N ,1
		- no hardware flow control
		- no delimiter selected

Manually interacting with the GPS unit
--------------------------------------

In ``ipython``, from the ch_acq folder::

	import gps
	g=gps.SpectrumInstrumentsTM4D('10.0.0.10')
	g.set_polling_mode(1)  # stop the GPS from broacdasting continuously
	g.get_date_time() # just for testing, ask the time and date
	g.configure_gps() # setup the GPS parameters for CHIME

You can set the unit in survey mode, meaning it will average 10000 positions and then switch back into static mode (time only)::

	g.set_polling_mode(1)  # stop the GPS from broacdasting continuously
	g.set_timing_mode(3)  # start the survey mode

You can check the setting with:

	g.get_timing_mode()  #

You might have to repeat the command to flush the previous responses that were put in the buffer by pervious commands (or the broadcast mode)

