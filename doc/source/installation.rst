.. _detailed_installation:

Installation
============

This page describes how to install and configure the software required to run ch_master and all the other software included in :mod:`ch_acq` package.

Requirements
------------
   * system files
       * git (>1.8.2 for git-lfs)
       * git-lfs (needed to pull fpga firmware)
       * Python 2.7.X (Python 3 **not** supported)
       * python pip
       * python-devel (needed on some system to install ipython, matplotlib)
       * tkinter (needed on Centos for matplotlib)
       * mdns/avahi/bonjour services with development libraries

   * Python packages:

       - virtualenv (pip)
       - ipython
       - numpy
       - matplotlib
       - sqlalchemy
       - pyyaml
       - tornado
       - lxml
       - h5py
       - nose
       - docutils
       - futures
       - requests
       - netifaces
       - psutil
       - pybonjour (requires avahi/mdns/bonjour system files to be installed)::

            pip install -e git+https://github.com/Eichhoernchen/pybonjour.git#egg=pybonjour

            or

            wget https://storage.googleapis.com/google-code-archive-downloads/v2/code.google.com/pybonjour/pybonjour-1.1.1.tar.gz
            tar zxf pybonjour-1.1.1.tar.gz
            cd pybonjour-1.1.1
            python setup.py install


Centos 7 installation
---------------------

OS version check
****************
::

    cat /etc/*-release


Git
***

Check the Git and git-lfs version with::

    git --version
    git-lfs --version

If git is lower than 1.8.2, we need to update it for git-lfs. Git and git-lfs are installed following the instructions from https://github.com/git-lfs/git-lfs/wiki/Installation::

    sudo yum install epel-release
    sudo yum install git # if needed
    curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.rpm.sh | sudo bash
    sudo yum install git-lfs
    git lfs version


Activate the git-lfs install (needs to be done only once by this specific user)::

    git lfs install


Python
******

Check version::

    python --version

Hopefully this will will be one of the 2.7 releases. Any one should do (we operated successfully with 2.7.5 - 2.7.11). We cannot operate with python 3. It is not trivial to install another python version: Centos uses it for its system and is very picky on which verison is installed, and it is hard to get and compile all the dependencies. We tried to install 2.7.13 instead of 2.7.5 and fai;led because we could not find tkinter libraries for that specific version.

Pip
***
If there is no pip,  install with::

    curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
    sudo python get-pip.py



Virtualenv
**********
If the user want to use his/her own package configuration, or don;t have the right to install packages at the system level, it is recommendd to install those package within a Python virtual environment. This is not necessary to run the basic modules in the :mod:`ch_acq` since all the necessary packages have been installed at the system level.

If the virtualenv package is not installed::

    sudo pip install virtualenv

We don't want to mess up Centos or other user Python install, so we'll install our packages in out own local user environment.
We create a virtualenv with the default system python version with:

    virtualenv ~/py275

And we activate the environment with::

    source ~/py275/bin/activate

.. Note:: If packages are installed within the virtualenv environemnt, the environment will needs to be activated on every new user session

Python packages
***************

Some packages have system library dependencies. Let's install them first:

ipython and matplotlib need python development libs::

    sudo yum install python-devel
    sudo yum install tkinter


We will use pybonjour, which requires avahi system libraries::

    sudo yum install avahi avahi-compat-libdns_sd avahi-compat-libdns_sd-devel
    sudo yum install avahi-tools avahi-ui-tools # to get the command-line tools like avahi-browse, avahi-discover

Now, install python packages, including pybonjour::

    pip install ipython
    pip install numpy matplotlib sqlalchemy pyyaml tornado lxml h5py
    pip install nose docutils futures requests netifaces psutil
    pip install -e git+https://github.com/Eichhoernchen/pybonjour.git#egg=pybonjour


.. note:: Pybonjour can also be installed manually
   with ::

        wget https://storage.googleapis.com/google-code-archive-downloads/v2/code.google.com/pybonjour/pybonjour-1.1.1.tar.gz
        tar zxf pybonjour-1.1.1.tar.gz
        cd pybonjour-1.1.1
        python setup.py install

.. _getting_source_code:

Getting the source code
***********************

We need ch_acq (python code) and chfpgalite (FPGA firmware) repositories, which are located on bitbucket.

if you use ssh keys to access these repos, setup ssh keys in .ssh/config or start ssh-agent and provide it with your keys::

    eval `ssh-agent`
    ssh-add path_to_bitbucket_key

If the key requires a password, enter it. Alternatively, a special hostname can be defined in your ``~/.ssh/config`` file with the appropriate associated key, and that hostname can be used in the git configuration.

Create a you own user folder to put the repos::

    mkdir ~/git
    cd ~/git

Get the repos::

    git clone  git@bitbucket.org:chime/ch_acq.git ch_acq
    git-lfs clone  git@bitbucket.org:winterlandcosmology/chfpgalite.git chfpga

Checkout the proper branches. In this example, we use jfc_dev for ch_acq and jfc/dev for chfpgalite::

    cd ch_acq
    checkout jfc_dev
    cd ../chfpga
    checkout jfc/dev

Testing ch_acq
--------------

If you installed the Python packages in a virtual environment, make sure the virtualenv is enabled. Otherwise, skip this step::

    source ~/py275/bin/activate

Then launch ipython

    cd ~/git/ch_acq
    ipython


in ipython, create a fpga_array with no boards in it, just to see if there are no missing packages::

    run -i pychfpga/fpga_array.py



Networking Configuration, Tricks & Tips
***************************************

To make the system work, we need to
    1) allow mDNS and UDP packets from the FPGA to be allowed in, and
    2) accept jumbo frames for raw data acquisition.


Opening ports
-------------

To temporarily allow Avahi to work and accept all UDP packets for the FPGA commands and raw data (which might also allow mDNS)

    sudo iptables -I INPUT -p udp -j ACCEPT

Opeen port to allow clients to connect to servers

    sudo iptables -I INPUT 1 -p tcp  --dport 54320-54329 -j ACCEPT  # all servers

On Centos 7, this could be made permanent by usingthe equivalent firewalld commands::

    sudo firewall-cmd --permanent --add-port=54320-54329/tcp
    sudo firewall-cmd --permanent --add-port 0-65535/udp

Enabling Jumbo Frames
---------------------

Raw data packets are large and require the interface to accept JUMBO frames. Enable JUMBO frames temporarily with::

    sudo ifconfig enp0s31f6 mtu 9000

To make the change permanent on Centos 7::

    sudo vim /etc/sysconfig/network-scripts/ifcfg-enp0s31f6

and add the line::

    MTU=9000


Checking the traffic on the interface
-------------------------------------

Tip: you can check incoming trafic with::

    ip -s  link show enp0s31f6

Checking mDNS/Avahi
-------------------

To check if mDNS works, you can query the FPGA boards using the mdns client Avahi:

    avahi-browse _tuber-jsonrpc._tcp --resolve

If resolve timeouts after 10 seconds, there is a problem. Sould kill the avahi server:

    sudo avahi-daemon -k

a new server will apparently be started when needed.


Finding boards using mDNS
-------------------------

To list new boards that are not in the static DHCP table and ended up with dynamic addresses (10.0.1.x)::

    sudo avahi-daemon -k
    avahi-browse  _tuber-jsonrpc._tcp --resolve -t | grep -B2 -A2 '10.0.1.'

Checking crates visible to mDNS:
--------------------------------

You can get a list of all active FPGA crates in the network with::

    sudo avahi-daemon -k
    avahi-browse  _tuber-jsonrpc._tcp --resolve -t | grep -o 'backplane-serial=[0-9]*' | sort -u

Editing static DHCP entries
---------------------------

The following files contain the static DHCP assignemetes that are based on MAC addresses. The entries can be cut and pasted from the receiver hut address table Google spreadsheet at https://bao.phas.ubc.ca/wiki/index.php/Receiver_Hut_Networking#IP_Address_map. To edit the files::

    sudo vim /etc/dhcp/dhcpd.conf # general config
    sudo vim /etc/dhcp/fpga.network # fpga equipment assignments

Once the files are edited:
    sudo service dhcpd restart


DHCP leases
-----------

To check the Ip addresses that are dynamically allocated to hardware over DHCP, you can do on ``carillon``::

    cat /var/lib/dhcpd/dhcpd.leases

This, however, will not show the statically assigned IPs (the IPs bound to specific MAC addresses). To see those::

    sudo cat /etc/dhcp/dhcpd.conf # general config
    sudo cat /etc/dhcp/fpga.network # fpga equipment assignments


If you need to find the IP address of equipment with specific hardware address from the dynamic or static lease lists, you can do::

    cat /var/lib/dhcpd/dhcpd.leases | grep -B 7 '00:18'
    sudo cat /etc/dhcp/dhcpd.conf | grep '00:18'


Update prometheus
-----------------

Log on hk-east as chime
sudo vim /etc/prometheus/prometheus.yml
curl -X POST localhost:9090/-/reload