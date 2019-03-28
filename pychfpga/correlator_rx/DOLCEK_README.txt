# DOLCEK NOTES

--- How to import .pyx files----

In your terminal:

Requirements:
> sudo apt-get install python-dev
> sudo apt-get install libevent-dev
Install Cython: 
> pip install Cython
Install Gnu C++ Compiler: 
> sudo apt-get install gcc g++

Go to the directory where you have your .pyx file
*** Before running setup.py, you will need to adjust the path to your computer:

> python setup.py install

> cythonize -a -i iceboard_receiver.pyx


*** If you use Anaconda, the steps above may not be sufficient. Ubuntu default gcc and Anaconda (libstdc++ file in Anaconda/bin) may not be the same. In this case: 

> conda install -c anaconda gcc

Then try to import the compiled .pyx module in your Python code once again.


- Some references:

https://askubuntu.com/questions/30996/can-i-use-a-c-c-compiler

https://anaconda.org/anaconda/gcc

https://github.com/jupyterhub/jupyterhub/issues/1640



