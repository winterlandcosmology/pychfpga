# Standard library packages
import logging

# Local packages
from .hardware_map import HardwareMap


class Crate(HardwareMap):
    """Base class for all HardwareMap-managed crates. 

    """
    # Define the class and instance registries that will be used by all subclasses.
    # _class_registry = {}  # {part_number:class}
    # _instance_registry = {}  # {(model,serial):instance}

    # Define the part number associated with this class. This *must* be defined in subclasses.
    part_number = None  # shall be a string in real classes
    _ipmi_part_numbers = None  # list of strings listing all models by which the board can be self-identified (via EEPROM, IPMI, mDNS etc)

    crate_number = None

    NUMBER_OF_SLOTS = 0

    def __init__(self, serial=None, crate_number=None, **kwargs):
        """ Create all the objects needed to interface the backplane hardware.

        __init__ should only passively create objects, which may be temporry. It must not attempt to
        access hardware.

        This class defines the attributes and methods that are available to all
        Crates (including those inherited from CrateHandler).

        Any attributes added by the user must be accessed after it has been
        ensured that the correct Crate has been instantiated.

        """
        if serial and not self.part_number:
            raise RuntimeError('Cannot create a generic Crate with a serial number')

        if isinstance(serial, int):  # make sure serial is a string
            serial = f'{serial:03d}'

        super().__init__()
        self.serial = serial
        self.slot = {}
        self.crate_number = crate_number

        self.logger = logging.getLogger(__name__)
        self.logger.debug(f'{self!r}: Instantiating Crate object')

        if kwargs:
            self.logger.warning(f'{self!r}: keyword arguments {kwargs} will be ignored')

        self.logger.debug(f"{self!r}: Created {self.__class__.__name__}(serial={serial}, crate_number={crate_number})")

    def __repr__(self):
        return f'{self.__class__.__name__}(serial={self.serial})'

    @classmethod
    def get_network_discovery_function(cls):
        """Return the default function to use for network crate discovery."""
        from pychfpga.common.mdns_discovery import mdns_discover

        return mdns_discover

    @classmethod
    def get_unique_instance(cls, new_class=None, serial=None, crate_number=None):
        """
        Creates a new Crate instance if one with matching crate_number or
        serial number does not exist, otherwise return an existing one
        augmented with the new serial or crate_number information.

        """
        matching_crates = [
            c for c in cls.get_all_registered_instances()
            if (crate_number is not None and c.crate_number == crate_number)
            or ((new_class or cls).part_number
                and serial and c.part_number == (new_class or cls).part_number
                and c.serial == serial
                )
        ]
        # print(f'{cls!r}: Found crates {matching_crates}')
        if not len(matching_crates):  # no matching crate, create one
            return (new_class or cls)(serial=serial, crate_number=crate_number)
        elif len(matching_crates) == 1:  # one match, update existing one
            return matching_crates[0].update_instance(new_class=new_class, serial=serial, crate_number=crate_number)
        else:
            raise RuntimeError('Multiple Crates with same keys (should never happen)')

    def update_instance(self, new_class=None,  serial=None, crate_number=None):
        """
        Update the class, serial or crate_number info of specified Crate
        subclass instance. If the class needs to be changed, a new class
        instance is created and the Motherboard references are updated to the new class.

        Parameters:

            new_class (Crate subclass)

            serial (str): new serial number

            crate_number (int): new crate number


        """
        new_args = dict(
            serial=serial or self.serial,
            crate_number=crate_number if crate_number is not None else self.crate_number)
        if new_class and self.__class__ is not new_class:
            other = new_class(**new_args)
            self.delete_instance()
            other.slot = self.slot  # copy over the slot info
            # Search all Motherboard instance that referred to the old
            # crate instance and update their crate references to the new instance
            for mb in self.get_all_registered_instances('Motherboard'):
                if mb.crate is self:
                    mb.crate = other
            return other
        else:  # otherwise update serial and crate_number
            if serial and not self.part_number:
                raise RuntimeError('Cannot assign a serial number to generic Crate')
            for k, v in new_args.items():
                setattr(self, k, v)
            return self
