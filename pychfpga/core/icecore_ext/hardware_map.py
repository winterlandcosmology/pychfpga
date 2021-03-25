""" mardware_map module: defines the HardwareMap base class to allow tracking and managing a collection of hardware resources.
"""
class HardwareMap:
    """
    """
    _base_hardware_class_registry = {} # {class_name, class_instance}
    _class_registry = {} # {class_name: class} A new dict will be assigned by the root class
    _instance_registry = {}  #  A new dict will be assigned by the root class
    part_number = None  # Must be defined by every hardware class
    _ipmi_part_numbers = []
    _instance_keys = []  # Must be defined by every class

    def __init_subclass__(cls, **kwargs):
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
        Return the class that corresponds to the specified IPMI part number.

        Searches for the  target par number in the _ipmi_part_numbers lists provided by each registered class.

        There shall be only one class that matches the target IPMI. If there are multiple matches, a RuntimeError will be raised.
        This method is not suited for selecting objects that are software or firmwre defined, like IceBoards.

        Parameters:

            part_number (str): target part number

        Used by discover_crate, discover_mezzanine
        """
        base_class = cls._base_hardware_class_registry[base_class_name or cls.__name__]
        matching_classes = [c for c in base_class._class_registry.values() if  part_number in (c._ipmi_part_numbers or [])]
        if not len(matching_classes):
            return None
        elif len(matching_classes) == 1:
            return matching_classes[0]
        else:
            raise RuntimeError('Multiple classes matched the target IPMI part number')

    @classmethod
    def get_unique_instance(cls, **kwargs):
        """
        Return an instance of a class that matches the class keys (which might
        not be of class `cls`), or create a new instance of class `cls`.

        instances are searched based on the _instance_keys. Keys that have the value 'None' are ignored.
        All keys must match
        The first key that matches
        What if two keys are provided and yield different instances


        Used in fpga_array, discover_crate, discover_mezzanine

        """
        raise NotImplemented('Must define an equality operator for the hardware map object')


    @classmethod
    def get_all_classes(cls, base_class_name=None):
        """
        Return a list of all registered classes

        Used in discover_mezzanine, fpga_array
        """
        base_class = cls._base_hardware_class_registry[base_class_name or cls.__name__]
        return list(base_class._class_registry.values())

    @classmethod
    def get_all_instances(cls, base_class_name=None):
        """
        """
        base_class = cls._base_hardware_class_registry[base_class_name or cls.__name__]
        return list(base_class._instance_registry.values())

    def delete_instance(self):
        """
        """
        self._instance_registry.pop(self)

