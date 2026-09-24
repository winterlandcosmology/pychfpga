Manual operation with ``fpga_array``
====================================

:mod:`pychfpga.fpga_array` builds an object that represents any number of
IceBoards and IceCrates and initializes them. When it finishes, it returns
control to you. For a stand-alone automated server, see
:doc:`server_operation`.

Interactive operations use IPython. Activate the environment, then::

    source my_env/bin/activate
    cd pychfpga
    ipython

Power up the boards, then initialize them. The examples below go from a
single board to a 128-board array.

Single IceBoard by IP (external correlator, ``shuffle16``)::

    run -i fpga_array 1.2.3.4 --prog --open --sync_method local_soft_trigger --mode shuffle16

If Avahi/Bonjour (mDNS) is running, you can address a board by model and
serial number. IceBoard MGK7MB serial 0123 with the on-board correlator
(``corr16``), streaming over the 1G SFP+ link::

    run -i fpga_array mb 123 --prog --open --sync_method local_soft_trigger --mode corr16

You can also select boards by crate model and serial. Two MGK7BP16 crates
(serials 12 and 13 become crate numbers 0 and 1), 512 analog inputs through
the corner-turn engine to an external correlator (``shuffle512``),
synchronized with IRIG-B::

    run -i fpga_array bp16 12:0 13:1 --prog --open --sync_method distributed_time --mode shuffle512

See also :doc:`quick_start` and :doc:`takeData`.
