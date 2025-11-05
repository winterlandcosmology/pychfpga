import os

from .fpga_bitstream import FPGABitstream

class FPGAFirmware():
    # FIRMWARE_URL = None
    # PLATFORM_MODEL = None
    # _bitstream_cache = {}
    _class_registry = {}  # {class_name:class}

    PLATFORM_SUPPORT = {} # Indicates the platform/config-specific bitstream filename and parameters. Overriden by subclasses

    def __init_subclass__(cls, **kwargs):
        """ Keep track of all created firmware subclasses in __class_registry__"""
        super().__init_subclass__(**kwargs)
        cls._class_registry[cls.__name__] = cls
        # Do not allow empty platform support tables
        if not cls.PLATFORM_SUPPORT:
            raise RuntimeError(f'Subclass {cls.__name__} must define a PLATFORM_SUPPORT table.')

    # @classmethod
    # def get_fpga_firmware_class_by_name(cls, name):
    #     """ Returns the firmware class that has the name `name`.
    #     """
    #     return cls._class_registry[name]

    # @classmethod
    # def get_bitstream_object(cls, url, folder=None):
    #     """ Returns the bitstream object for the specified platform and bitstream configuration.
    #     """
    #     if url not in cls._bitstream_cache:
    #         cls._bitstream_cache[url] = FPGABitstream(url, folder=folder)
    #     cls.bitstream = cls._bitstream_cache[url]
    #     cls.bitstream.load_bitstream()  # reload bitstream if it has changed
    #     return cls.bitstream

    # @classmethod
    # def get_modes_for_platform(cls, platform_name):
    #     """ Returns a list of valid firmware operational modes for the specified platform.
    #     """

    #     return [mode for (fw_cls_name, fw_cls) in cls._class_registry.items()
    #                 for (pf_name, fw_name, modes), pf_info in fw_cls.PLATFORM_SUPPORT.items()
    #                 for mode in modes
    #                 if pf_name == platform_name]

    @classmethod
    def get_firmware(cls, platform_name, mode, bitfile_override=None, folder_override=None):
        """ Get the FPGAFirmware subclass and FPGAFirmware instance that matches the specified platform and operational mode.

        The search is performed by looking at the entries in the PLATFORM_SUPPORT tables for all the
        FPGAFirmware subclasses listed in the class registry (see `__init_subclass__`).


        Parameters

            platform_name (str): name of the platform for which we want to fetch the bitstream. The
                string has to match the platform (motherboard) ``part_number`` exactly (match is
                case sensitive).

            mode (str): the desired operational mode. The string  has to match one of the modes
                listed in the PLATFORM_SUPPORT table of the target firmware class. The match is case
                sensitive.

            bitfile_override (str):

               - If `bitfile_override` is a path pointing to a file, that bitsteam file will will be loaded.

               - If `bitfile_override` is a path to a directory and `folder_override` is not
                 specified, `bitfile_override` will be used as the search folder for the default
                 firmware name.


               - If `None`, the default bitstream file (based on the platform/mode) will be loaded
                 from the search folder.

            folder_override (str): Sets the search folder that contains the bitstreams. It is ignored
                if `bitfile_override` contains an absolute path to a file. If `None`, the default
                search folder ``./bitstream`` (relative to this file) is used unless
                `bitfile_override` points to a folder, in which case `bitfile_override` will be used
                as a search folder.

        Returns:

            (fw_cls, bs_instance, fw_info) tuple where:

                fw_cls: The FPGAFirmware subclass that matches the target firmware

                bs_instance: An instance of the FPGABitstream loaded with the firmware corresponding with the target firmware

                fw_info: A dict containing any other additional firmware information specific to the firmware name and platform.

        Exceptions:

            RuntimeError will be raised if no match or more than one match is found.
        """

        # find all the firmware classes and platform info that match the specified platform name
        # We only need one, but having them all is useful to show them to the user in case of error.
        pf_modes = {mode: (fw_cls, pf_info)
                    for (fw_cls_name, fw_cls) in cls._class_registry.items()
                    for pf_info in fw_cls.PLATFORM_SUPPORT  # (pf_name, fw_name, modes)
                    for mode in pf_info['modes']
                    if pf_info['platform'] == platform_name}
        # find all the firmware class and platform info that matches the target mode
        # There should normally be only one match.
        fw = [(fw_cls, pf_info.copy())
                    for pf_mode, (fw_cls, pf_info) in pf_modes.items()
                    if pf_mode == mode]

        if not fw:
            raise RuntimeError(f'Could not find firmware for mode {mode} for platform {platform_name}. Available modes are {",".join(pf_modes)}')
        elif len(fw) > 1:
            raise RuntimeError(f'Found multiple matches for firmware mode {mode} and platform {platform_name}')
        fw_cls, pf_info = fw[0]
        # bs = cls.get_bitstream_object(bitfile_override or pf_info.pop('firmware_url'), folder=folder_override)
        if bitfile_override is not None and os.path.isdir(bitfile_override):
            if folder_override is None:
                folder_override =  bitfile_override
                bitfile_override = None
            else:
                raise RuntimeError(f'Search folder is already specified; bitstream_override must point to a file.')
        url = bitfile_override or pf_info.pop('firmware_url')
        bs = FPGABitstream.get_bitstream(url, folder=folder_override)
        return fw_cls, bs, pf_info

