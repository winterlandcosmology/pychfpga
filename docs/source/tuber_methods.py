class TuberMethods(object):

      def _backplane_eeprom_write_base64(self):
            """
            Write the I2C EEPROM associated with a backplane.
            """
      def _bp_qsfp_eeprom_read_base64(self):
            """
            Reads the motherboard QSFP EEPROM contents
            """
      def _bp_qsfp_eeprom_write_base64(self):
            """
            Writes the motherboard QSFP EEPROM contents
            """
      def _cache_bp_frui(self):
            """
            Reads the eeprom on the backplane and updates the FRUI cache.
            """
      def _fpga_spi_peek(self):
            """
            Peek.
            """
      def _fpga_spi_poke(self):
            """
            Poke.
            """
      def _get_arm_ip(self):
            """
            Retrieves the IP address associated with the ARM.
            """
      def _get_arm_mac(self):
            """
            Retrieves the MAC address associated with the ARM.
            """
      def _get_backplane_ipmi(self):
            """
            Read the I2C EEPROM, and interpret it as an IPMI descriptor.
            """
      def _get_backplane_serial(self):
            """
            Get mezzanine serial number.
            """
      def _get_backplane_type(self):
            """
            Get backplane type.
            """
      def _get_backplane_version(self):
            """
            Get backplane version.
            """
      def _get_mezzanine_ipmi(self):
            """
            Read the I2C EEPROM, and interpret it as an IPMI descriptor.
            """
      def _get_mezzanine_serial(self):
            """
            Get mezzanine serial number.
            """
      def _get_mezzanine_type(self):
            """
            Get mezzanine type.
            """
      def _get_mezzanine_version(self):
            """
            Get mezzanine version.
            """
      def _get_motherboard_ipmi(self):
            """
            Read the FRUI, and interpret it as an IPMI descriptor.
            """
      def _get_personality(self):
            """
            Retrieves the IceBoard's 'personality'.
            """
      def _get_syslog_buffer(self):
            """
            Dumps the contents of the log buffer.
            """
      def _get_syslog_mask(self):
            """
            Retrieves the syslog level for the IceBoard.
            """
      def _initialize_backplane(self):
            """
            Initialize this IceBoard's cache of backplane data.
            """
      def _mezzanine_eeprom_read_base64(self):
            """
            Raw read of mezzanine eeprom.
            """
      def _mezzanine_eeprom_write_base64(self):
            """
            Write the I2C EEPROM associated with a mezzanine.
            """
      def _motherboard_eeprom_write_base64(self):
            """
            Write the I2C EEPROM associated with a motherboard.
            """
      def _motherboard_spi_flash_write_base64(self):
            """
            Write the SPI flash associated with the motherboard.
            """
      def _qsfp_eeprom_read_base64(self):
            """
            Reads the motherboard QSFP EEPROM contents
            """
      def _qsfp_eeprom_write_base64(self):
            """
            Writes the motherboard QSFP EEPROM contents
            """
      def _reset_all_fpgas(self):
            """
            Resets all the FPGAs in a rack.
            """
      def _reset_all_power(self):
            """
            Resets all the boards (power) in a rack.
            """
      def _set_fpga_bitstream_base64(self):
            """
            Loads the FPGA's bitstream
            """
      def _set_personality(self):
            """
            Sets the IceBoard's 'personality'.
            """
      def _set_syslog_mask(self):
            """
            Sets the syslog level for the IceBoard.
            """
      def _sleep(self):
            """
            Do something, slowly.
            """
      def _syslog_test(self):
            """
            Sends a test message to syslog.
            """
      def clear_fpga_bitstream(self):
            """
            Disable the FPGA
            """
      def get_backplane_current(self):
            """
            Retrieve the current from the backplane's sensor.
            """
      def get_backplane_power(self):
            """
            Retrieve the power measurement from the backplane's sensor.
            """
      def get_backplane_slot(self):
            """
            Retrieves the slot number this iceboard occupies.
            """
      def get_backplane_temperature(self):
            """
            Retrieve the backplane temperature.
            """
      def get_backplane_voltage(self):
            """
            Retrieve the voltage from the backplane's sensor.
            """
      def get_bp_qsfp_gpio(self):
            """
            Retrieve the state of an QSFP control pin.
            """
      def get_bp_qsfp_led(self):
            """
            Sets the states of the motherboard QSFP control pins
            """
      def get_build_info(self):
            """
            Returns the commit hashes and compile times for both icecore and iceboard-linux.
            """
      def get_clock_source(self):
            """
            Returns the type of clock input selected by iceboard jumpers
            """
      def get_led(self):
            """
            Get the status of an LED on the IceBoard
            """
      def get_mezzanine_current(self):
            """
            Retrieve the voltage from one of the mezzanine's sensors.
            """
      def get_mezzanine_power(self):
            """
            Retrieve the current power status of an FMC mezzanine
            """
      def get_mezzanine_voltage(self):
            """
            Retrieve the voltage from one of the mezzanine's sensors.
            """
      def get_motherboard_current(self):
            """
            Retrieve the voltage from one of the motherboard's sensors.
            """
      def get_motherboard_power(self):
            """
            Retrieve the power reading from one of the motherboard's sensors.
            """
      def get_motherboard_serial(self):
            """
            Retrieve the serial number from the iceboard's SPI flash.
            """
      def get_motherboard_temperature(self):
            """
            Retrieve the temperature from one of the motherboard's sensors.
            """
      def get_motherboard_voltage(self):
            """
            Retrieve the voltage from one of the motherboard's sensors.
            """
      def get_power_on_slot(self):
            """
            Reads power state on a given slot.
            """
      def get_qsfp_gpio(self):
            """
            Retrieve the state of an QSFP control pin.
            """
      def is_backplane_present(self):
            """
            Determines if a backplane is present.
            """
      def is_bp_qsfp_present(self):
            """
            Returns true if a QSFP is present on the specified module
            """
      def is_fpga_programmed(self):
            """
            Returns the state of the FPGA's DONE signal
            """
      def is_mezzanine_present(self):
            """
            Determines if a mezzanine is present.
            """
      def is_qsfp_present(self):
            """
            Returns true if a QSFP is present on the specified module
            """
      def is_voltage_nominal(self):
            """
            Check if all buck converters are within +/-5% of nominal.
            """
      def reboot(self):
            """
            Reboots an iceboard.
            """
      def reset_arm_on_slot(self):
            """
            Resets the ARM on the slot specified.
            """
      def set_bp_qsfp_gpio(self):
            """
            Sets the states of the motherboard QSFP control pins
            """
      def set_bp_qsfp_led(self):
            """
            Sets the states of the motherboard QSFP control pins
            """
      def set_led(self):
            """
            Turn on and off the motherboard LEDs.
            """
      def set_mezzanine_power(self):
            """
            Turn on/off an FMC mezzanine
            """
      def set_pci_switch_direction(self):
            """
            Controls the direction of the PCI switch (U40) on the motherboard
            """
      def set_power_on_slot(self):
            """
            Turns on and off the power to a given slot.
            """
      def set_qsfp_gpio(self):
            """
            Sets the states of the motherboard QSFP control pins
            """
