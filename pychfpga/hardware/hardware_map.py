""" hardware_map module: defines the HardwareMap base class to allow tracking and managing a collection of hardware resources.

`HardwareMap` owns all hardware-class discovery and bookkeeping:

- In-memory registries of classes and instances (maintained automatically by
  `__init_subclass__` / `__new__`).
- Lazy loading of hardware platform classes listed in the manifest
  `pychfpga/hardware/supported_hardware.py:HARDWARE`. The manifest is read via
  function-local imports from the classmethods below, so it can refer to
  `Motherboard`/`Crate`/`Mezzanine` (which import `HardwareMap`) without
  creating a circular import.
- Lookups that transparently fall back to the manifest when a class has not
  yet been imported (`get_class_by_name`, `get_class_by_part_number`,
  `get_class_by_ipmi_part_number`).

External code must only use the public methods below; it must never access
the internal registries (``_class_registry``, ``_instance_registry``,
``_base_hardware_class_registry``) or the `supported_hardware` module
directly.

Naming conventions:

- ``registered`` = currently in the in-memory registry (the class has been
  imported and its ``__init_subclass__`` has run).
- ``supported``  = declared in the ``HARDWARE`` manifest (may or may not be
  loaded yet).
"""

import importlib


class HardwareMap:
    """

    A part_number = None signify that this is a generic class and does not
    represent real hardware. Generic classes can be instantiated as long as
    they have uniquely defining attributes, but there are generally temporary
    objects that are later updated with a part_number & serial number.

    """
    # dict of all hardware base classes that have been created. Defined only once here.
    _base_hardware_class_registry = {} # {class_name, class_instance}


    # Class and instance registry. New containers are redefined for each Hardware base class.
    _class_registry = {} # {class_name: class} A new dict will be assigned by the root class
    _instance_registry = []  # list of instances. A new list will be assigned by the root class.
    # The list identity is preserved across ``clear_hardware_map()`` (see
    # ``clear_hardware_map``) so external references (e.g.
    # ``FPGAArray.motherboards`` / ``FPGAArray.crates``) stay live across
    # resets.
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
            cls._instance_registry = []
        cls._class_registry[cls.__name__] = cls

    def __new__(cls, *args, **kwargs):
        """ Registers each new instance of the class"""
        i = super().__new__(cls)  # we discard all arguments
        cls._instance_registry.append(i)
        return i

    # ----------------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------------

    @classmethod
    def _get_base_class(cls, base_class_name=None):
        """Return the hardware base class (``Motherboard``/``Crate``/``Mezzanine``)
        for ``cls`` or the one matching ``base_class_name`` when provided.
        """
        return cls._base_hardware_class_registry[base_class_name or cls.__name__]

    @classmethod
    def _iter_manifest(cls, base_class_name=None, package=None):
        """Yield the manifest entries that belong to the hardware base class
        for ``cls``. Performs a function-local import of ``supported_hardware``
        to avoid a circular import at module load time.

        Parameters:

            base_class_name (str): restrict to a specific hardware base class
                (``'Motherboard'``/``'Crate'``/``'Mezzanine'``). Defaults to
                the caller's base class.
            package (str): restrict to manifest entries whose ``package`` field
                equals this value (e.g. ``'ice'``). Defaults to no filter.
        """
        base_class = cls._get_base_class(base_class_name)
        from . import supported_hardware
        for entry in supported_hardware.HARDWARE:
            if not issubclass(entry['type_'], base_class):
                continue
            if package is not None and entry['package'] != package:
                continue
            yield entry

    # ----------------------------------------------------------------------
    # Loading primitives (manifest -> class registry)
    # ----------------------------------------------------------------------

    @classmethod
    def load_class(cls, name=None, part_number=None, ipmi_part_number=None,
                   base_class_name=None):
        """Find the manifest entry matching any of the given keys and import
        its class (triggering registration in the class registry).

        Parameters:

            name (str): class name to match (e.g. ``'IceBoard'``).
            part_number (str): official part number to match, case-insensitive.
            ipmi_part_number (str): any IPMI part number alias to match,
                case-insensitive.
            base_class_name (str): restrict the search to a specific hardware
                base class (``'Motherboard'``/``'Crate'``/``'Mezzanine'``).
                Defaults to the caller's base class.

        Returns:
            The imported class, or ``None`` if no manifest entry matched.

        Raises:
            RuntimeError: if more than one manifest entry matches the query.
        """
        pn_up = part_number.upper() if part_number else None
        ipmi_up = ipmi_part_number.upper() if ipmi_part_number else None

        matches = []
        for entry in cls._iter_manifest(base_class_name):
            if name is not None and entry['name'] != name:
                continue
            if pn_up is not None and (entry.get('part_number') or '').upper() != pn_up:
                continue
            if ipmi_up is not None and ipmi_up not in tuple(
                    p.upper() for p in (entry.get('ipmi_part_numbers') or ())):
                continue
            matches.append(entry)

        if not matches:
            return None
        if len(matches) > 1:
            raise RuntimeError(
                f'HardwareMap.load_class: multiple manifest entries match '
                f'(name={name!r}, part_number={part_number!r}, '
                f'ipmi_part_number={ipmi_part_number!r}): '
                f"{[e['name'] for e in matches]}")

        entry = matches[0]
        module = importlib.import_module(f"pychfpga.hardware.{entry['package']}")
        return getattr(module, entry['name'])

    @classmethod
    def load_all_classes(cls, base_class_name=None, package=None):
        """Import every manifest entry whose ``type_`` is compatible with
        ``cls`` (or with the hardware base class named ``base_class_name``).
        When ``package`` is given, only entries from that subpackage under
        ``pychfpga.hardware`` are imported (e.g. ``package='ice'`` to load
        only the Ice platform classes).
        """
        for entry in cls._iter_manifest(base_class_name, package=package):
            importlib.import_module(f"pychfpga.hardware.{entry['package']}")

    # ----------------------------------------------------------------------
    # Single-class lookups (lazy-load on miss)
    # ----------------------------------------------------------------------

    @classmethod
    def get_class_by_name(cls, name, base_class_name=None):
        """Return the class registered under ``name`` within the hardware base
        class of ``cls`` (or the one named ``base_class_name``). Consults the
        manifest and imports the matching platform package on miss.

        Returns:
            The class object, or ``None`` if neither the registry nor the
            manifest has an entry for ``name``.
        """
        base_class = cls._get_base_class(base_class_name)
        registered = base_class._class_registry.get(name)
        if registered is not None:
            return registered
        return cls.load_class(name=name, base_class_name=base_class_name)

    @classmethod
    def get_class_by_part_number(cls, part_number, base_class_name=None):
        """Return the hardware class whose ``part_number`` ends with
        ``part_number`` (case-insensitive).

        Searches first the in-memory class registry for every hardware base
        class; if nothing matches, loads the full manifest (which may import
        previously-unseen platform packages) and retries. Used by the hardware
        map string parser to resolve user-supplied suffixes like ``MGK7MB`` or
        ``MGK7BP16``.

        Parameters:

            part_number (str): part-number suffix to match. Case-insensitive.
            base_class_name (str): if provided, restrict the search to the
                named hardware base class. Defaults to searching across every
                hardware base class (``Motherboard``, ``Crate``, ``Mezzanine``,
                ...), because a part-number string like ``MGK7BP16`` does not
                itself indicate which kind of hardware it describes.

        Returns:
            Matching class.

        Raises:
            RuntimeError: if zero or multiple classes match the suffix.
        """
        target = part_number.upper()

        def find_matches():
            if base_class_name is not None:
                bases = [cls._base_hardware_class_registry[base_class_name]]
            else:
                bases = list(cls._base_hardware_class_registry.values())
            matches = []
            for base in bases:
                for c in base._class_registry.values():
                    if c.part_number and c.part_number.upper().endswith(target):
                        matches.append(c)
            return matches

        matching = find_matches()

        if not matching:
            # Miss in the registry: import everything listed in the manifest and retry.
            if base_class_name is not None:
                cls.load_all_classes(base_class_name=base_class_name)
            else:
                for base_name in list(cls._base_hardware_class_registry):
                    cls.load_all_classes(base_class_name=base_name)
            matching = find_matches()

        if not matching:
            raise RuntimeError(f'Cannot find a part number that ends in {part_number}')
        if len(matching) > 1:
            raise RuntimeError(f'Found multiple part numbers that end in {part_number}')
        return matching[0]

    @classmethod
    def get_class_by_ipmi_part_number(cls, part_number, base_class_name=None):
        """
        Return the class whose official part number (cls.part_number) or aliases (cls._ipmi_part_numbers) matches the specified `part_number`.

        Searches for the target part number in the ``_ipmi_part_numbers``
        lists provided by each registered class. On miss, the manifest is
        consulted and the matching platform package is imported, then the
        lookup is retried — so callers never need to pre-load platforms.

        There shall be only one class that matches the target IPMI. If there
        are multiple matches, a ``RuntimeError`` will be raised. 

        Parameters:

            part_number (str): target part number

        Returns:

            matching class, None if there are no matches. Raises a Runtime
            exception if there are multiple matches.

        Used by discover_crate, discover_mezzanine, mdns_discover
        """
        base_class = cls._get_base_class(base_class_name)

        def find_matches():
            return [c for c in base_class._class_registry.values()
                    if c.part_number
                    and (part_number == c.part_number or part_number in (c._ipmi_part_numbers or []))]

        matching_classes = find_matches()
        if not matching_classes:
            # Try to lazy-load from the manifest.
            cls.load_class(ipmi_part_number=part_number, base_class_name=base_class_name)
            matching_classes = find_matches()

        if not len(matching_classes):
            return None
        elif len(matching_classes) == 1:
            return matching_classes[0]
        else:
            raise RuntimeError('Multiple classes matched the target IPMI part number')

    # ----------------------------------------------------------------------
    # Instance management (abstract, overridden by hardware base classes)
    # ----------------------------------------------------------------------

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

    # ----------------------------------------------------------------------
    # Bulk listing
    # ----------------------------------------------------------------------

    @classmethod
    def get_all_registered_classes(cls, base_class_name=None):
        """
        Return a list of all currently-registered classes that have the same
        common hardware base class as ``cls`` or ``base_class_name`` (e.g. all
        Motherboard subclasses).

        "Registered" means the class has already been imported and has
        declared itself via ``__init_subclass__``. Classes listed only in the
        manifest are not included — use :meth:`get_all_supported_classes` for
        that.

        Used in discover_mezzanine, fpga_array
        """
        base_class = cls._get_base_class(base_class_name)
        return list(base_class._class_registry.values())

    @classmethod
    def get_all_supported_classes(cls, base_class_name=None, package=None):
        """Import every class declared in the manifest for the hardware base
        class of ``cls`` (or ``base_class_name``) and return the resulting
        class list. Use this when a caller genuinely needs every possible
        class, not just the ones already loaded (e.g. EEPROM decoders that
        must try every mezzanine variant).

        When ``package`` is given, the result is narrowed to classes declared
        in that subpackage under ``pychfpga.hardware`` (e.g. ``package='ice'``
        to return only Ice-platform mezzanines). This is useful when a
        platform-specific routine must iterate over *its own* hardware
        variants, not every variant in the manifest.
        """
        cls.load_all_classes(base_class_name=base_class_name, package=package)
        if package is None:
            return cls.get_all_registered_classes(base_class_name=base_class_name)
        names = {entry['name'] for entry in cls._iter_manifest(base_class_name, package=package)}
        return [c for c in cls.get_all_registered_classes(base_class_name=base_class_name)
                if c.__name__ in names]

    @classmethod
    def get_all_registered_instances(cls, base_class_name=None):
        """ Returns the live list of all instances of objects that have the
        same common Hardware base class as `cls` (e.g. all Motherboards
        instances). If `base_class_name` is not None, the instance list of
        the base class named `base_class_name` is returned instead.

        The returned list is the actual internal registry, not a copy: it
        will reflect instances created or deleted after this call. This lets
        callers store e.g.
        ``self.motherboards = Motherboard.get_all_registered_instances()``
        once and iterate the up-to-date set of motherboards without going
        through a property each time. As a consequence, callers must not
        mutate the returned list directly; use the ``HardwareMap`` API
        (instance creation, ``delete_instance``, ``clear_hardware_map``)
        instead.

        Parameters:

            base_class_name (str or None)

        """
        if base_class_name:
            cls = cls._base_hardware_class_registry[base_class_name]
        return cls._instance_registry

    # ----------------------------------------------------------------------
    # Manifest views (callers must never read `supported_hardware.HARDWARE`
    # directly; add a view here instead.)
    # ----------------------------------------------------------------------

    @classmethod
    def get_supported_class_names(cls, base_class_name=None):
        """Return a tuple of class names declared in the manifest for the
        hardware base class of ``cls`` (or ``base_class_name``). Used to
        build helpful error messages without forcing the classes to be
        loaded.
        """
        return tuple(entry['name'] for entry in cls._iter_manifest(base_class_name))

    @classmethod
    def get_supported_ipmi_part_numbers(cls, base_class_name=None):
        """Return a flat tuple of all IPMI part numbers declared in the
        manifest for the hardware base class of ``cls`` (or
        ``base_class_name``). Used to build user-facing error messages that
        list every supported part number regardless of which platforms have
        been loaded yet.
        """
        return tuple(
            ipmi
            for entry in cls._iter_manifest(base_class_name)
            for ipmi in (entry.get('ipmi_part_numbers') or ())
        )

    # ----------------------------------------------------------------------
    # Debug / maintenance
    # ----------------------------------------------------------------------

    @classmethod
    def print_registered_classes(cls):
        print(cls._class_registry)

    @classmethod
    def clear_hardware_map(cls):
        """Drop every registered instance from every hardware base class.

        Uses ``list.clear()`` so the registry list identity is preserved and
        any external reference (notably ``FPGAArray.motherboards`` /
        ``FPGAArray.crates``) keeps pointing at the live, now-empty list.
        """
        for base_class in cls._base_hardware_class_registry.values():
            base_class._instance_registry.clear()

    def delete_instance(self):
        """Remove this instance from its base class instance registry."""
        self._instance_registry.remove(self)
