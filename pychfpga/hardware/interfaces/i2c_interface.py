""" Generic I2C interface """
import logging
from ..i2c_devices import tca9548a  # I2C switch


class I2CInterface(object):
    """
    This class wraps all that is needed to access an I2C device in
    a standardized way, whether the access is done through the
    FPGA or through the ARM.
    """

    I2CException = IOError  # Exception object to expect from I2C communication errors

    def __init__(self, write_read_fn, port_select_fn, bus_table, switch_addr, parent=None, verbose=None):
        self.parent = parent
        self.write_read_fn = write_read_fn
        self.set_port_fn = port_select_fn
        self._I2C_BUS_LIST = bus_table

        self._i2c_switch = tca9548a.tca9548a(self, switch_addr)
        self._logger = logging.getLogger(__name__)

    def __repr__(self):
        """ Return a string representation of this I2C interface.

        Returns:

            A string which include the parent object id.

        """
        return f"{self.__class__.__name__}({self.parent!r})"

    def select_bus(self, bus_names, *args, **kwargs):
        """
        Configure the I2C port and I2C switch so the following
        communications will access the desired I2C bus. 'bus_id'
        can be a bus name or bus number, or a list of those if
        multiple buses are to be accessed at the same time. An
        error will be provided if all the buses are not accessible
        through the same FPGA I2C port. This function assumes that
        each FPGA I2C port has an identical I2C switch.

        Parameters:

            bus_names (str, int, or list of str or int): Name or number of the
                I2C bus to enable on the I2C switch. Multiple buses can be
                enabled at one time.

            args, kwargs: passed to the bus select function

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors

        """
        if isinstance(bus_names, (str, int)):
            bus_names = [bus_names]
        selected_fpga_port_number = None
        selected_switch_port_numbers = []
        for bus_name in bus_names:
            if bus_name not in self._I2C_BUS_LIST:
                self._logger.error(
                    f"{self!r}: I2C bus '{bus_name}' is not part of the available buses. Valid values are {','.join(str(self._I2C_BUS_LIST.keys()))}")
            (fpga_port_number, switch_port_number) = self._I2C_BUS_LIST[bus_name]
            if selected_fpga_port_number is None:
                selected_fpga_port_number = fpga_port_number
            elif selected_fpga_port_number != fpga_port_number:
                self._logger.error(
                    f"{self!r}: I2C bus '{bus_name}' is not on the same FPGA port as the other buses")
            selected_switch_port_numbers.append(switch_port_number)
            # self._logger.debug("Enabling I2C bus %s" % bus_name)

        if selected_fpga_port_number is not None:
            self.set_port_fn(selected_fpga_port_number)

        self._i2c_switch.set_port(selected_switch_port_numbers, *args, **kwargs)

    def write_read(self, *args, **kwargs):
        """
        Writes up to 3 bytes to the addressed I2C device and/or
        reads up to 4 bytes from that device after a restart. See
        the FPGA I2C module for detailed method description.

        Parameters:
            See `I2C.write_read`

        Returns:
            See `I2C.write_read`

        Exceptions:

            IOError: Raised by an FPGA-based I2C controller in case of transaction errors

        """
        # self._logger.debug("Accessing I2C bus...")
        return self.write_read_fn(*args, **kwargs)

    def is_present(self, addr, bus_name=None):
        """ Test the presence of an I2C device at the specified address.

        Parameters:

            addr (int): I2C address of the device to query

            bus_name (str, int, or list of str or int): I2C bus(es) to activate

        """
        if bus_name:
            self.select_bus(bus_name, retry=3)
        try:
            self.write_read(addr, data=[], read_length=0, retry=0)  # dummy I2C acces
        except IOError:
            return False
        return True
