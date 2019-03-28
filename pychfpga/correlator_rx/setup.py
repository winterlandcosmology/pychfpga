from distutils.core import setup
from distutils.extension import Extension
from Cython.Distutils import build_ext
import numpy as np
import platform

if(platform.system() != 'Windows'):
	ext_modules=[
	    Extension("iceboard_receiver",
	              ["iceboard_receiver.pyx", "ciceboard_receiver.c" ],
	              libraries=['m', 'pthread'],
	              library_dirs = ['/home/sean/work/anaconda3/lib/'],
	              include_dirs = [np.get_include(),'/home/sean/work/anaconda3/include/'],
	              extra_compile_args = ['-std=c99']) # Unix-like specific
	]

	setup(
	  name = "iceboard_receiver",
	  cmdclass = {"build_ext": build_ext},
	  ext_modules = ext_modules
	)

# WINDOWS VERSION
#
# You will to have setup the proper build environment to install this module on windows.
# Use the same visual c++ compiler that your python was compiled with, in my case for python 2.7 it was the 64 bit vc++ 2008.
# Run the vc++ 2008 console with admin priviliges (if installing to Program Files), run 'SET DISTUTILS_USE_SDK=1' and 'SET MSSdk=1'.
# Then run this script with 'python setup.py install'.
# Be sure sure to adapt any paths below to your system.

else:
	ext_modules=[
    Extension("iceboard_receiver",
              ["iceboard_receiver_win.pyx", "ciceboard_receiver_win.c" ],
              libraries=['ws2_32'],
              library_dirs = ['C:/Program Files/Anaconda2/Lib/'],
              include_dirs = [np.get_include(),'C:/Program Files/Anaconda2/include/'])
	]

	setup(
	  name = "iceboard_receiver",
	  cmdclass = {"build_ext": build_ext},
	  ext_modules = ext_modules
	)