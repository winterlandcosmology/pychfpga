""" mardware_map module: defines the HardwareMap base class to allow tracking and managing a collection of hardware resources.
"""
class HardwareMap:
    """

    A part_number = None signify that this is a generic class and does not
    represent real hardware. Generic classes can be instantiated as long as
    they have uniquely defining attributes, but there are generally temporary
    objects that are later updated with a part_number & serial number.

    """
    # dict of all hardware base classes that have been created. Defined only once here.
    _base_hardware_class_registry = {} # {class_name, class_instance}


    # Class and instance registry. New dicts are redefined for each Hardware base classes.
    _class_registry = {} # {class_name: class} A new dict will be assigned by the root class
    _instance_registry = {}  #  A new dict will be assigned by the root class
    # _instance_keys = []  # Must be defined by every class

    # Define the part number(s) associated with this class.
    part_number = None  # This is a Generic class. Can be redefined by real-hardware subclasses
    _ipmi_part_numbers = [] # Possible equivalent part number names found in IPMI records that correspond to this platform


    def __init_subclass__(cls, **kwargs):
        """Register each new subclass in the class registry. Start a new class and instance
        registry for each hardware base class, i.e. classes that inherit
        *directly* from Hardware map but not their subclasses.
        """
        super().__init_subclass__(**kwargs)
        # If this is the first subclass of HardwareMap, this class is
        # considered to be the base class for that hardware and shall have its
        # own class and instance list
        if HardwareMap in cls.__bases__:
            cls._base_hardware_class_registry[cls.__name__] = cls
            cls._class_registry = {}
            cls._instance_registry = {}
        cls._class_registry[cls.__name__] = cls

    def __new__(cls, *args, **kwargs):
        """ Registers each new instance of the class"""
        i = super().__new__(cls)  # we discard all arguments
        cls._instance_registry[i] = i
        return i

    @classmethod
    def print_classes(self):
        print(self._class_registry)

    @classmethod
    def clear_hardware_map(cls):
        for base_class in cls._base_hardware_class_registry.values():
            base_class._instance_registry = {}

    @classmethod
    def get_class_by_ipmi_part_number(cls, part_number, base_class_name=None):
        """
        Return the class whose official part number (cls.part_number) or aliases (cls._ipmi_part_numbers) matches the specified `part_number`.

        Searches for the  target par number in the _ipmi_part_numbers lists provided by each registered class.

        There shall be only one class that matches the target IPMI. If there are multiple matches, a RuntimeError will be raised.
        This method is not suited for selecting objects that are software or firmwre defined, like IceBoards.

        Parameters:

            part_number (str): target part number

        Returns:

            matching class, None if there are no matches. Raises a Runtime
            exception if there are multiple matches.

        Used by discover_crate, discover_mezzanine, mdns_discover
        """
        base_class = cls._base_hardware_class_registry[base_class_name or cls.__name__]
        matching_classes = [c for c in base_class._class_registry.values()
                            if c.part_number
                            and (part_number == c.part_number or part_number in (c._ipmi_part_numbers or []))
                            ]
        if not len(matching_classes):
            return None
        elif len(matching_classes) == 1:
            return matching_classes[0]
        else:
            raise RuntimeError('Multiple classes matched the target IPMI part number')

    @classmethod
    def get_unique_instance(cls, **kwargs):
        """
        Return an instance of an object that matches the uniquely identifying
        parameters passed as arguments (e.g. model/serial, hostnaaame,
        crate_number etc,), or create a new instance of class `cls` if it does
        not exist.

        This method must be defined once for each hardwre base classes
        (Motherboard, Crate, etc) and should update related objects that refer
        to this instance.
        """
        raise NotImplementedError(
            f'get_unique_instance() is not defined for generic hardware map objects of type {cls}. '
            f'Call this method from a hardware base class (Motherboard, Crate etc.) or its subclasses. ')

    def update_instance(self, new_class=None, **kwargs):
        """
        Update the instance with the specified parameters and returns the
        updated class, while ensuring all reference from other HardwareMap
        objects are updated accordingly. If new_class is not None and is
        different from the existing class, a instance of `new_class` is
        created and updated with the specified parameters,

        The `new_class` parameters allows a generic hardware object (no part
        number) to be upgrated to a specific one (with part number)

        This method must be defined once for each hardwre base classes
        (Motherboard, Crate, etc).

        Parameters:

            new_class (class): If not None, crete a new instance of class
                `new_class` updated with the specified parameters.

            kwargs: same parameters that can be provided on object creation using get_unique_instance()


        Returns:

            updated instance of the same class or of class `new_class` if specified.

        """
        raise NotImplementedError(
            f'update_instance() is not defined for generic hardware map objects of type {self}. '
            f'Call this method from a hardware base class (Motherboard, Crate etc.) or its subclasses. ')

    @classmethod
    def get_all_classes(cls, base_class_name=None):
        """
        Return a list of all registered classes objects that have the same
        common hardware base class as `cls` or `base_class_name`. (e.g all
        Motherboard subclasses).

        Used in discover_mezzanine, fpga_array
        """
        base_class = cls._base_hardware_class_registry[base_class_name or cls.__name__]
        return list(base_class._class_registry.values())

    @classmethod
    def get_all_instances(cls, base_class_name=None):
        """ Returns a list of all instances of objects that have the same
        common Hardware base class as `cls` (e.g. all Motherboards instances).
        If `base_class_name` is not None, all instances of the base class
        named `base_class_name` are returned instead.

        Parameters:

            base_class_name (str or None)

        """
        if base_class_name:
            cls = cls._base_hardware_class_registry[base_class_name]
        return list(cls._instance_registry.values())

    def delete_instance(self):
        """
        """
        self._instance_registry.pop(self)

