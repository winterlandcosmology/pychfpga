# standard Python packages
import logging
import asyncio


# private packages

from wtl.metrics import Metrics

# local Packages
from .hardware_map import HardwareMap
from .crate import Crate


class Motherboard(HardwareMap):
    """
    Hardware base class that defines a generic motherboard (Iceboard or others) with hardware map
    management methods. All motherboards across all Motherboard subclasses are uniquely identified by its model
    number and serial number, and has a network hostname.

    A motherboard can optinally be connected to one crate and have multiple mezzanines. This class defines the HardWare
    map methods that  create and update Motherboooards while ensuring related Crate and Mezzanines are kept up to date.

    Since this class inherits directly from HardwareMap, it is treated as a hardware base class and will create a
    class and instance registry to track all subsequent Motherboard subclasses.

    A Motherboard class is a generic class that has no part number. It cannot be instantiated with only a serial number,
    but it *can*  be instantiated with a hostname since that uniquely identifies it. The application can resolve and
    upadte the model and serial number later with update_instance().
    """

    # Define the class and instance registry that will be used by HardwareMap
    # to track all Motherboard subclass instances. This should *not* be
    # defined in further subclasses.
    # _class_registry = {}  # {part_number:class}
    # _instance_registry = {}  # {(model,serial):instance}

    # This is a generic class and has no part number. This *must* be defined in subclasses.
    part_number = None  # shall be a string in real classes
    _ipmi_part_numbers = None  # list of strings listing all models by which the board can be self-identified (via EEPROM, IPMI, mDNS etc)

    SERIAL_NUMBER_LENGTH = 4  # number of digits in the serial number. Used to convert integers to a valid serial number.

    # _cached_repr = None  # Stores a pre-processed string representation of the board repr() for efficiency

    NUMBER_OF_FMC_SLOTS = 0  # Number of supported mezzanines
    FMC_MEZZ_NUMBERS = () # Logical mezzanine numbers

    port = None  # port number on which to access the platform `hostname`. Must be defined by subclasses. Is used by fpga_master.

    cls_logger = logging.getLogger(__name__)

    def __init__(self, hostname=None, serial=None, slot=None, subarray=None, **kwargs):
        """ Create a Motherboard object.

        One does not usually instantiate hardware map objects directly. Instead use the
        get_unique_instance(...) class method to ensure that objects with
        matching hostname or serial numbers will be reused if available.


        Parameters:

            hostname (str):  host name or IP address of the board.  If `hostname` is 'None' but the
                instance has a serial number, then the board hostname can
                potentially be resolved using mDNS discovery.

            serial (str or int): Serial number of the board, in the exact
                format that is published by the board. For
                convenience, if `serial` is an integer, it is converted into a
                properly formatted string serial.

            slot (int):  virtual slot number for use of multiple boards in an
                array. A value of  0 or None (i.e. bool(slot) == False)
                indicates that there is no slot information.

            subarray: Arbitrary value that is used to group boards in logical
                categories.

            kwargs (dict): extra arguments stored in the instance to be used
                in future initialization steps.

        Notes:

            - __init__() only sets the object key parameters and manages the
                hardware map and does not initiate connection with the hardware it
                represents. Indeed, Hardware map objects (and their subclasses) can be
                created and destroyed freely  during the process of hardware map
                creation. __init__() therefore shall not initiate communication with
                the hardware or do complex set-up; this is done by init(), which will
                be called when the hardware map is completed and stable.


            - The Board initialization is done in 4 steps:

                - open_platform_async() establishes communication with the
                  motherboard's on-board processor. This enables access to the
                  methods provided by the motherboard.

                - set_fpga_bitstream_async(): programs the FPGA with the target
                  firmware. The Firmware instance is created in self.fpga, but is not yet
                  usable.

                - open_fpga_async() establishes the communication link with
                  the FPGA, crete the objects that handle the firmware, and
                  provides basic management interfaces and methods.

                - init_fpga_async() initializes the firmware in the desired
                  operational state.


        """
        # Initialize a cached value for the `repr()` string, which is used to
        # improve efficiency.  `None` means the cache is invalidated and a new
        # string shall be recomputed. We need to define this as early as
        # possible to avoid infinite recursions if repr() is called directly
        # or indirectly. The cached is clear on updates to account for the new
        # parameters.
        self._cached_repr = None

        self.logger = logging.getLogger(__name__)


        # Normalize the serial number
        if isinstance(serial, int):  # make sure serial is a string
            serial = f'{{:0{self.SERIAL_NUMBER_LENGTH}d}}'.format(serial)  # Convert integer serial number to N digits with 0 prefixes

        if not (serial or hostname):
            raise ValueError(f'Must specify either a serial number or hostname for {self.__class__.__name__}')



        self.logger.debug(f"Motherboard: Creating {self.__class__.__name__}(serial={serial}, hostname={hostname}, slot={slot}, subarray={subarray}, kwargs={kwargs})")

        self.hostname = hostname
        self.serial = serial
        self.slot = slot
        self.subarray = subarray
        self.extra_args = kwargs

        self.crate = None  # Crate (or backplane) object on which the motherboard is mounted
        self.mezzanine = {}  # Mezzanines attached to this motherboard ({slot:mezz_object, ...})

        super().__init__()

        self.fpga = None   # No firmware loaded by default

        self.logger.debug(
            f"{self!r}: Created {self.__class__.__name__}(hostname={hostname}, "
            f"serial={serial}, slot={slot}, subarray={subarray}), "
            f"crate={self.crate}")

    def __repr__(self):
        """ Provides a concise string representation of this Motherboard that is
        informative enough to be used for logs.
        """

        if self._cached_repr:
            return self._cached_repr
        else:
            self._cached_repr = f"{self.__class__.__name__}({self.get_string_id()})"
            return self._cached_repr

    def __getattr__(self, name):
        """ Look for attributes in the firmware object if not found in the motherboard.
        """
        if self.fpga:
            return getattr(self.fpga, name)
        else:
            raise AttributeError(f'Unknown attribute {name}')

    def __dir__(self):
        """ Returns bothe the Motherboard's and FPGA Firmware's attributes"""
        return list(set(super().__dir__()
                    + (self.fpga.__dir__() if self.fpga else [])))

    # *************************
    # Motherboard-specific hardware map management methods
    # *************************
    # Overrides the HardwareMap methods

    @classmethod
    def get_unique_instance(cls,
                            new_class=None,
                            serial=None,
                            hostname=None,
                            slot=None,
                            subarray=None,
                            crate_number=None,
                            **kwargs):
        """
        Creates a new Motherboard instance if one with matching hostname or
        serial number does not exist, otherwise return an existing one
        augmented with the new serial or hostname information.

        Existing instances are those who match either the specified hostname
        or part_number/serial number. If no board is found, a new instance of
        class `new_class` is created. Otherwise, the existing instance is
        updated with the parameters  that are not `None`.


        All creation and update operations maintain the integrity of the
        references between Motherboards, Crates and Mezzanines.

        Use this method to create Motherboard objects instead of instantiating
        them directly from the target class in order to maintain the hardware
        map integrity.

        Parameters:

            new_class (Motherboard or subclass): class desired for the
                returned instance. If None, the class of an existing object is
                not changed, and a new object is created with the class `cls`

            serial (str): serial number of the Motherboard to look for, and to
                assign to a new instance or existing matching instance.

            hostname (str): hostname of the Motherboard to look for, and to
                assign to a new instance or existing matching instance.

            slot (int): slot number in which the board is located in a crate
                or backplane, or virtual slot number if the board is not in a
                crate. Is assigned to the new or existing matching Motherboard.

                If the slot number is changed, the associated Crate slot mapping is updated.

            subarray: Arbitrary value used to group Motherboards in logical
                arrays. Is assigned to new instance or existing matching instance.

            crate_number: For convenience, if `crate_number` is specified, the
                new or existing board is associated with the Crate instance
                that matches the specified crate number, or one is created
                with that crate number to hold the desired crate number value.

            **kwargs: Any other argument is stored in the extra_args
                dictionary, and will be transferred if this instance is
                converted into a new class.
        """
        # print(f"In et_unique_instance")

        matching_boards = [
            c for c in cls.get_all_instances() if (
                (hostname is not None and c.hostname == hostname)
                or ((new_class or cls).part_number
                    and serial
                    and c.part_number == (new_class or cls).part_number
                    and c.serial == serial))]

        # print(f"Matches: {matching_boards}")
        if not len(matching_boards):  # no matching crate, create one
            cls.cls_logger.debug( # cannot use logger?
                f"get_unique_instance: No matching instance: creating new class {(new_class or cls).__name__}(serial={serial}, hostname={hostname}, "
                f"slot={slot}, subarray={subarray}, kwargs={kwargs})")
            ib = (new_class or cls)(serial=serial, hostname=hostname, slot=slot, subarray=subarray, **kwargs)
            cls.cls_logger.debug(f"get_unique_instance: New instance created. Now updating Motherboard with crate_number={crate_number}")
            return ib.update_instance(crate_number=crate_number)

        elif len(matching_boards) == 1:  # one match, update existing one
            cls.cls_logger.debug(f"get_unique_instance: Found matching instance. Updating it with new parameters")
            return matching_boards[0].update_instance(
                new_class=new_class,
                serial=serial,
                hostname=hostname,
                slot=slot,
                subarray=subarray,
                crate_number=crate_number,
                **kwargs)
        else:
            raise RuntimeError('Multiple Motherboards with same keys (should never happen)')

    def update_instance(self,
                        new_class=None,
                        serial=None,
                        hostname=None,
                        slot=None,
                        subarray=None,
                        crate_number=None,
                        crate=None,
                        **kwargs):
        """
        Selectively change the class and/or update the parameters of this Motherboard
        subclass instance.

        If the class is changed, the parameters of the old class are copied
        into the new class, the crate and mezzanines references are updated to
        the new instance, and the old instance is deleted.

        Parameters:

            new_class: Motherboard subclass into which the current instance should be moved. If `None`, the class is unchanged.

            serial (str): New serial number for the instance. Unchanged if `None`.

            hostname (str): New hostname for the instance. Unchanged if `None`.

            slot (int): New slot number for this Motherboard instance in its Crate. Unchanged if `None`.
               If changed, all references from Crate instances to this Motherboards are deleted and a new reference to the attached crate object for the new slot is created.

            subarray (str or int): New subarray for the instance. Unchanged if `None`.

            crate (Crate subclass instance): New crate object to which this Motherboard subclass instance belongs.
                Unchanged if `None`. If changed, the Motherboard reference of the old crate is deleted.

            crate_number (int): Alternate way of assigning a new crate to the
                the Motherboard instance. If there is no crate with matching crate
                number, a new generic crate is created.

            kwargs (dict): Any extra arguments to be stored in the instance for future use in downstream initializations.

        Returns:

            The new and/or updated Motherboard subclass instance.

        """
        serial = serial or self.serial
        hostname = hostname or self.hostname
        slot = slot if slot is not None else self.slot
        subarray = subarray if subarray is not None else self.subarray
        crate_number = (crate_number if crate_number is not None
                        else self.crate.crate_number if self.crate else None)
        extra_args = {**self.extra_args, **kwargs}
        if new_class and self.__class__ is not new_class:
            self.logger.debug(f"update_instance: Creating new instance with {new_class.__name__}(serial={serial}, hostname={hostname}, slot={slot}, subarray={subarray}, **{extra_args})")
            other = new_class(serial=serial, hostname=hostname, slot=slot, subarray=subarray, **extra_args)
            self.logger.debug(f"{self!r}: Updating newly created instance...")
            other.update_instance(crate_number=crate_number, crate=self.crate)  # update crate and backrefs
            # Update Mezzanine references to the new instance
            for fmc, mezz in self.mezzanine.items():
                other.mezzanine[fmc] = mezz
                mezz.iceboard = other
            self.delete_instance()
            return other
        else:  # otherwise update serial and hostname
            if kwargs:
                raise NotImplementedError(
                    f'Cannot update existing {self.__class__.__name__} instance '
                    f'with additional keyword arguments {kwargs}')
            self.hostname = hostname
            self.serial = serial
            self.subarray = subarray
            self.extra_args = extra_args
            # remove previous crate backref if it exists
            if self.crate and self.slot:
                self.crate.slot.pop(self.slot, None)
            # Assign new slot
            self.slot = slot
            # reattach crate by crate number if specified
            self.logger.debug(f"{self!r}: Updating with with crate_number={crate_number}...")
            if crate_number is not None:
                self.crate = Crate.get_unique_instance(crate_number=crate_number)
            elif crate:
                self.crate = crate
            # Create new crate backref
            if self.crate and self.slot:
                self.crate.slot[self.slot] = self

            self._cached_repr = None  # Clear on updates to account for the new parameters.
            return self

    # *************************
    # Board identification
    # *************************

    def get_id(self, lane=None, default_crate=None, default_slot=None, numeric_only=False):
        """ Returns a (crate, slot) tuple representing a unique IceBoard ID,
        using numeric values whenever possible. A `lane` field can be
        optionally appended to the tuple to create channel/lane IDs.

        Parameters:

            lane (int): caller-provided lane number to be appended to the returned tuple. Used to
                create channel or lane ID tuples.

            default_crate: Default values to return in the crate field if
                there is no crate, or there is a crate but there is no
                crate_number. If None, either the crate number or crate string
                id is used.

            default slot: Default values to return in the slot field if there
                is no slot number.

            numeric_only (bool): If true, an exception will be raised if there
                is no valid crate number and slot number. `default_crate` and
                `default_crate` are ignored.

        Returns:
            A (crate_id, slot_or_board_id) tuple, where:

            - crate_id is the first of the following:
                - ``numeric_crate_number`` (int) if there is a crate and the crate number is known
                - `default_crate` if `default_crate` is not `None`
                - ``crate_model_serial_string`` (str) model and serial number string if those exist
                - `None` if none  of the above is true

            - slot_or_board_id is the first of the following:
                - ``zero_based_slot_number`` (int) zero-based slot number if there is a valid slot
                  number (i.e. bool(self.slot) is True), whether or not there is a crate;
                - `default_slot` if  `default_slot` is not None;
                - ``board_model_serial_string`` (str) if the board has a valid model and serial number;
                - ``hostname`` (str) hostname if the board has a known hostname;
                - ``None`` if none of the above is true.

        Notes:

            - A board is always represented by a 2-element tuple. A crate is always represented by a
              one-element tuple, and a channel/lane is a 3-element tuple.
            - The user is responsible for handling all possible types of crate (int, str, None) or
              slot (int, str) tuple elements
            - crate can be None, but slot can never be None: it will be replaced by the string ID of
              the board so the tuple always refer to a specific board.
            - If the crate provides a numeric slot number, the user must rely on external
              information to infer which board serial number correspond to the specified ID
            - If the crate provides a numeric crate number, the user must rely on external
              information to infer which crate serial number correspond to the specified ID
            - the id must be unique, even if we have multiple stand-alone boards. There should at
              least a non-None crate or slot field (i.e. no (None, None) tuple) Examples:

        Examples:

            Board in a crate/backplane:

            - (2, 3): board on 4th slot of backplane with crate number 2
            - (2, 0): board on crate number 2 without slot number, and default_slot=0
            - (2, None): board on crate number 2 without slot information, and default_slot=None
            - ('MGK7BP16_SN023', 3): board on 4th slot of backplane without crate number and
              default_crate=None
            - (0, 3): board on 4th slot of backplane without crate number and default_crate=0
            - ('MGK7BP1_SN001', None): board on crate without crate_number,  without default_crate,
              without slot number, without default_slot (e.g. unconfigured single-slot test
              backplane)

            Stand-alone board (no backplane/crate):

            - (0, 0): No backplane, crate number nor slot_number, with default_crate=0 and
              default_slot=0
            - (None, 3): No backplane, but the board slot number was manually set to  self.slot=4
              (not a typical case)
            - (None, 'MGK7MB_SN0372'): No crate nor slot information, and no default_crate nor
              default_slot

        """
        if self.crate:  # if there is a crate/backplane, do not allow empty crate field but allow empty slot.
            crate_number = self.crate.crate_number
            if crate_number is None:  # if there is no valid crate number
                if numeric_only:
                    raise RuntimeError('The crate %s does not have a valid crate number' % self.crate)
                crate_number = default_crate if default_crate is not None else self.crate.get_string_id()

            if self.slot:  # if there is a valid slot (not 0 or None)
                slot = self.slot - 1
            else:
                if numeric_only:
                    raise RuntimeError('The crate %s does not have a valid slot number' % self.crate)
                slot = default_slot

        else:  # if there is no crate, allow empty crate but not an empty slot
            if numeric_only:
                raise RuntimeError('There is no crate nor numeric crate number')
            crate_number = default_crate
            # If there is no backplane AND no slot info (None or 0), we need to use the board
            # model/serial in the slot field to make the tuple unique.
            slot = self.slot - 1 if self.slot else default_slot if default_slot is not None else self.get_string_id()
        return (crate_number, slot) if lane is None else (crate_number, slot, lane)
        # if not self.crate
        #     crate or self.slot is None:
        #     return (self.get_string_id(), ) if lane is None else (self.get_string_id(), lane)
        # else:
        #     return self.get_crate_id(self.slot - 1) + (tuple() if lane is None else (lane,) )

    def get_crate_id(self, slot=None):
        """ Return the crate ID tuple optionally appended by the specified slot number.slot

        Parameters:

            slot (int): slot number to append to the tuple. Should be zero-based.slot

        Returns:

            (crate_id, ) if `slot` is `None`, else (crate_id, slot).
            ``crate_id`` is the crate number if it exists, otherwise it is a
            string that uniquely defined the crate.
        """
        return self.crate.get_id(slot=slot)

    def get_string_id(self):
        """ Return a string that identifies uniquely the Motherboard in the most convenient representation.

        Preference order:

            - (0,3)  # crate 0 , 4th slot (tuples always use zero-based indices)
            - (MGK7BP16_SN025, 3)  # Same, but without crate number info
            - MGK7MB_SN0234 # no crate info at all (even with virtual slot)
            - 10.10.10.244 # motherboard serial number not discovered
            - id=140529531550992 # nothing, last resort

        """
        if self.crate and self.crate.crate_number is not None and self.slot:
            return f"({self.crate.crate_number},{self.slot-1})"
        if self.crate and self.crate.part_number and self.crate.serial and self.slot:
            return f"({self.crate.part_number}_SN{self.crate.serial},{self.slot-1})"
        elif self.serial:
            return f"{self.part_number}_SN{self.serial}"
        elif self.hostname:
            return self.hostname
        else:
            return f"id={id(self)}"

    def set_cache(self):
        """ Caches key values to accelerate the code.

        The cache is actually cleared, and the next invocatio nof repr() will
        set it to the new value.
        """
        self._cached_repr = None

    # *************************
    # Motherboard info
    # *************************

    async def get_platform_ip_address(self):
        """ Return the IP address of the motherboard """
        raise NotImplementedError('This method must be implemented by a subclass')

    # *************************
    # FPGA Programming methods
    # *************************

    async def set_fpga_bitsream_async(self, firmware_class, force=True):
        """ Programs the FPGA with the specified bitstream
        """
        raise NotImplementedError('This method must be implemented by a subclass')

    async def is_fpga_programmed_async(self):
        raise NotImplementedError('This method must be implemented by a subclass')

    # *************************
    # Board pinging methods
    # *************************

    async def ping_async(self, timeout=0.1):
        """
        Returns a boolean indicating whether the motherboard is responding at
        its hostname/address.
        """
        raise NotImplementedError('This method must be implemented by a subclass')

    # *************************
    # Discovery methods
    # *************************

    async def discover_serial_async(self, update=True):
        """
        Discover the serial number of this board, and update the hardware map accordingly if `update=True`
        """
        raise NotImplementedError('This method must be implemented by a subclass')

    async def discover_slot_async(self, update=True):
        """
        Discover the slot number of this board, and update the hardware map accordingly if `update=True`
        """
        raise NotImplementedError('This method must be implemented by a subclass')

    async def discover_crate_async(self, update=True):
        """
        Discover the crate for this board, and update the hardware map accordingly if `update=True`
        """
        raise NotImplementedError('This method must be implemented by a subclass')

    # ****************************
    # Open/Close methods
    # ****************************

    async def open_platform_async(self):
        """ Establish a communication link with the platform, which enables
        access to functions provided by the local processor
        """
        raise NotImplementedError('This method must be implemented by a subclass')

    async def open_fpga_async(self):
        """Establish a communication link with the FPGA."""
        raise NotImplementedError('This method must be implemented by a subclass')

    async def init_fpga_async(self, **kwargs):
        """ Initializes the FPGA firmware.
        """
        await self.fpga.init_async(**kwargs)

    # ****************************
    # Metrics
    # ****************************

    async def get_metrics_async(self):
        """ Get the motherboard hardware monitoring information.

        Should be overriden to populate the Metrics object.

        Returns:
            a :class:`Metrics` object.
        """

        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)

        return metrics

    async def get_backplane_metrics_async(self):
        """ Get the backplane hardware monitoring information.

        Returns:
            a :class:`Metrics` object.
        """
        metrics = Metrics(
            type='GAUGE',
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)

        return metrics

    async def get_total_power(self):
        """ Return the total power used by this board.


        Returns:
            Total power in watts as a float.
        """
        raise NotImplementedError('This method must be implemented by a subclass')
