Take Primative Data from IceBoard
=================================

.. role:: bash(code)
   :language: bash

.. role:: python(code)
   :language: python


This tutorial assumes that you have run the :bash:`fpga_array.py` script and have a :python:`FPGAarray` object in Python named :python:`ca`.

Ramp ADC Data
-------------

For this step, the IceBoard must have been initialized **without** mezzanines.

Say you want to take data from a board :python:`x` in your hardware map. Begin by selecting that board:

:python:`b = ca.ib[x]`

Next, set the data capture mode to ADCs:

:python:`b.start_data_capture(source='adc',period=1)`

Now we define a data receiver:

:python:`r = b.get_data_receiver()`

And we can now capture data from the ramp into a variable called :python:`data`:

:python:`data = r.read_frames()`

CHIME ADC Data
--------------
