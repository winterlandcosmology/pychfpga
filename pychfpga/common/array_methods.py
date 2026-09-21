"""Shared support for :class:`pychfpga.fpga_array.FPGAArray`-wide methods.

Array-wide methods live on explicit :class:`ArrayMethods` provider classes.
Motherboard classes may assign ``array_method_class``. Firmware subclasses
register providers in ``_array_method_classes`` (see
:meth:`pychfpga.fpga_firmware.fpga_firmware.FPGAFirmware.register_array_methods`).
``FPGAArray`` instantiates the providers shared by every motherboard and
exposes their public methods as array methods.
"""

import logging

class ArrayMethods:
    """Base class for explicit :class:`FPGAArray` method providers.

    Provider methods should be regular instance methods. The provider receives
    the calling :class:`FPGAArray` at construction and exposes ``ib`` and
    ``ic`` attributes so methods can keep the old array-method shape.
    """

    array_method_names = None

    def __init__(self, fpga_array):
        self.fpga_array = fpga_array
        self.ib = fpga_array.ib
        self.ic = getattr(fpga_array, 'ic', None)
        self.logger = logging.getLogger(__name__)

    # def __repr__(self):
    #     return repr(self.fpga_array)

    # def __getattr__(self, name):
    #     """Delegate old array-method attribute reads to the FPGAArray."""
    #     return getattr(self.fpga_array, name)

    # def __setattr__(self, name, value):
    #     """Keep provider-local state local and delegate array state writes."""
    #     if name.startswith('_') or 'fpga_array' not in self.__dict__:
    #         object.__setattr__(self, name, value)
    #     else:
    #         setattr(self.fpga_array, name, value)

    def get_array_methods(self):
        """Return public callables defined on this provider class."""
        if self.array_method_names is not None:
            return {
                name: method for name in self.array_method_names
                if callable(method := getattr(self, name))
            }
        skip = dir(ArrayMethods)
        return {
            name: method for name in dir(type(self))
            if not name.startswith('_')
            and name not in skip
            and callable(method := getattr(self, name))
        }


