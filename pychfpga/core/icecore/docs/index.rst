IceBoard User Manual
====================

Welcome to the IceBoard user manual. In these pages, you will find
**hardware** and **developer** documentation for the IceBoard. For
**software** documentation, you should probably look for experiment-specific
content elsewhere (e.g. `Pydfmux Documentation
<http://kingspeak.physics.mcgill.ca/pydfmux_sphinx>`_)

.. figure:: images/banner.jpg
   :align:  center

.. tip:: This documentation is hosted in ``pydfmux/docs`` and can be rebuilt
   using the "Sphinx" tool. **To rebuild documentation** from scratch, run::

      $ cd icecore/docs
      $ make

   You can then find HTML documentation in ``icecore/docs/build/html``. You
   can also find a compiled copy `on Kingspeak
   <http://kingspeak.physics.mcgill.ca/icecore_sphinx>`_, although this
   snapshot may be out-of-date. (It can be updated by running ``make publish``
   from the ``icecore/docs`` directory.)

Hardware
--------

.. toctree::
   :maxdepth: 1

   hardware/index
   hardware/setup
   hardware/clocking
   hardware/fpga_config
   hardware/gtx

Firmware
--------

.. toctree::
   :maxdepth: 1

   firmware/index
   firmware/bootloader
   firmware/kernel
   firmware/rootfs
   firmware/app_server

Software
--------

.. toctree::
   :maxdepth: 1

   software/dependencies
   software/introduction
   software/object_model
   software/algorithms
   software/yaml
   software/troubleshooting
   software/profiling

How-To Guides
-------------

.. toctree::
   :maxdepth: 1

   howto/building_a_flash_card
   howto/nfs_root
   howto/web_development

.. Firmware
.. --------
.. 
.. .. toctree::
..    :maxdepth: 1
.. 
..    firmware/firmware
..    firmware/release_notes

.. Integration
.. -----------
.. 
.. .. toctree::
..    :maxdepth: 1
.. 
..    integration/unpacking
..    integration/networking
..    integration/streaming

.. Indices and tables
.. ------------------
..
.. * :ref:`genindex`
.. * :ref:`modindex`
.. * :ref:`search`

References
----------

.. [UG470] `7 Series FPGAs Configuration User Guide <http://www.xilinx.com/support/documentation/user_guides/ug470_7Series_Config.pdf>`_

.. vim: sts=3 ts=3 sw=3 tw=78 smarttab expandtab
