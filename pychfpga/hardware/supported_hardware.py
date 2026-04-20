"""Declarative list of hardware platforms supported by pychfpga.

Edit this file to add, remove, or update a hardware platform. Each entry
tells ``HardwareMap`` how to locate and import the corresponding class on
demand:

- ``type_``            : hardware base class (``Motherboard``, ``Crate``,
                         ``Mezzanine``). Determines which in-memory class
                         registry the entry belongs to.
- ``name``             : class name to import from the ``package`` below.
- ``part_number``      : official part number for the class, or ``None`` for
                         generic / base classes that do not represent real
                         hardware by themselves.
- ``ipmi_part_numbers``: tuple of equivalent part numbers that may appear in
                         IPMI / mDNS records.
- ``package``          : subpackage under ``pychfpga.hardware``. The class is
                         imported as ``from pychfpga.hardware.<package> import
                         <name>``.

Do not import ``HARDWARE`` from outside the ``pychfpga.hardware`` package;
use :class:`HardwareMap` classmethods (``load_class``, ``get_class_by_*``,
``get_all_supported_classes``, ``get_supported_ipmi_part_numbers`` ...)
instead.
"""

from .motherboard import Motherboard
from .crate import Crate
from .mezzanine import Mezzanine


HARDWARE = (
    dict(type_=Motherboard, name='IceBoard',             part_number='MGK7MB',     ipmi_part_numbers=('MGK7MB',),            package='ice'),
    dict(type_=Crate,       name='IceCrate_MGK7BP16',    part_number='MGK7BP16',   ipmi_part_numbers=('MGK7BP16', 'MGK7BP'), package='ice'),
    dict(type_=Crate,       name='IceCrate_MGK7BP1',     part_number='MGK7BP1',    ipmi_part_numbers=('MGK7BP1',),           package='ice'),
    dict(type_=Motherboard, name='ZCU111',               part_number='ZCU111',     ipmi_part_numbers=('ZCU111',),            package='zcu111'),
    dict(type_=Motherboard, name='CRS',                  part_number='CRS',        ipmi_part_numbers=('CRS',),               package='crs'),
    dict(type_=Motherboard, name='ZUBoard',              part_number='ZUBOARD1GC', ipmi_part_numbers=('ZUBOARD1GC',),        package='zuboard'),
    dict(type_=Mezzanine,   name='FMCMezzanine',         part_number=None,         ipmi_part_numbers=(),                     package='ice'),
    dict(type_=Mezzanine,   name='FMCMezzanine_MGADC08', part_number=None,         ipmi_part_numbers=(),                     package='ice'),
)
