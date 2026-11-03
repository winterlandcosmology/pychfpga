Setup Python for pychfpga
=========================

.. role:: bash(code)
   :language: bash

.. role:: python(code)
   :language: python

Linux
-----

This tutorial has been tested on Ubuntu 14.04 LTS and assumes that you are using :bash:`bash`.

Setting up your :bash:`$PYTHONPATH`
+++++++++++++++++++++++++++++++++++

At the end of your :bash:`.bashrc` file, append the following line:

:bash:`export PYTHONPATH="${PYTHONPATH}:<SOMEPATH>/ch_acq"`

where :bash:`<SOMEPATH>` is the directory containing :bash:`ch_acq`. This is required for later on when we try to run the Chime FPGA scripts later.

Setting up Anaconda
+++++++++++++++++++

Anaconda is a useful python tool that allows multiple Python environments
to be installed and used at the same time.

Instructions on how to install Anaconda can be found at http://docs.continuum.io/anaconda/install

Setting up Python
+++++++++++++++++

#. Define a new conda environment. For this example, the environment is called "tardis". pychfpga requires Python 2.7, so we will specify this, too.

:bash:`#: conda create --name tardis python=2.7`

This will produce some output to the screen asking if you want to install some default packages. 
Agree, and conda will create a new environment for you called :bash:`tardis`.

To activate the environment (i.e. turn it into your working environment), type

:bash:`#: source activate tardis`

Now we are working within the new :bash:`tardis` Python environment.

The next step is to install some libraries:

:bash:`(tardis)#: conda install anaconda-client ipython numpy matplotlib tornado sqlalchemy yaml nose lxml docutils futures`

There's another library not available on conda, that we need to manually download and install using :bash:`pip`:

:bash:`pip install <path-to-file>/pybonjour-1.1.1.tar.gz`

where pybonjour was downloaded from https://code.google.com/p/pybonjour/


Testing :bash:`fpga_array.py`
+++++++++++++++++++++++++++++

If you've followed these steps correctly, then you should be able to test the :bash:`fpga_array.py` script that runs the IceBoards. Start by navigating to the correct directory so we can run the script:

:bash:`(tardis)#: cd <your path to ch_acq>/pychfpga/`

:bash:`(tardis)#: ipython`

:python:`In [1]: run -i fpga_array.py`

You should see an output similar to: ::

   icecrates= []
   No Iceboards matching the selection criteria were found
   There are no IceCrates in the hardware map!
   The following IceBoards are in the hardware map:

   Updated hardware map, with mezzanine info:

   Done creating FPGAArray

If you so, you've successfully set up your Python environment to use :bash:`fpga_array.py`.
