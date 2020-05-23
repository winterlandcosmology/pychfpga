:class:`~chFPGA_controller.chFPGA_controller`
=============================================

.. currentmodule:: pychfpga.core.chFPGA_controller

.. autoclass:: chFPGA_controller(...)
   :show-inheritance:


Subclasses
----------

.. autosummary::

      pychfpga.core.icecore_ext.IceBoardExtHandler
      pychfpga.core.icecore.IceBoardPlusHandler
      pychfpga.core.icecore.IceBoardHandler
      pychfpga.core.icecore.TuberObject
      pychfpga.core.icecore.Handler


Method Summary (in alphabetical order)
--------------------------------------

.. rubric:: Initialization
.. .................................
.. autosummary::

      ~chFPGA_controller.__init__
      ~chFPGA_controller.open
      ~chFPGA_controller.init
      ~chFPGA_controller.close
      ~chFPGA_controller.is_open


.. rubric:: UDP Networking

.. autosummary::

      ~chFPGA_controller.open_core
      ~chFPGA_controller.close_core
      ~chFPGA_controller.is_core_open
      ~chFPGA_controller.set_fpga_control_networking_parameters
      ~chFPGA_controller.set_data_target_address
      ~chFPGA_controller.get_local_data_port_number
      ~chFPGA_controller.set_local_data_port_number

.. rubric:: UDP MMI command system

.. autosummary::

      ~chFPGA_controller.mmi_read
      ~chFPGA_controller.mmi_write
      ~chFPGA_controller.check_command_count
      ~chFPGA_controller.pulse_bit
      ~chFPGA_controller.read_bit
      ~chFPGA_controller.fpga_mmi_read
      ~chFPGA_controller.fpga_mmi_write
      ~chFPGA_controller.write_bit
      ~chFPGA_controller.write_mask
      ~chFPGA_controller.test_speed
      ~chFPGA_controller.ping_fpga

.. rubric:: Tuber: local support methods

.. autosummary::

      ~chFPGA_controller.print_tuber_methods
      ~chFPGA_controller.check_tuber_version
      ~chFPGA_controller.arm_exec
      ~chFPGA_controller.arm_scp
      ~chFPGA_controller.tuber_context


.. currentmodule:: tuber_methods

.. rubric:: Tuber: system methods

.. autosummary::

      ~TuberMethods._cache_bp_frui
      ~TuberMethods._fpga_spi_peek
      ~TuberMethods._fpga_spi_poke
      ~TuberMethods._get_arm_ip
      ~TuberMethods._get_arm_mac
      ~TuberMethods._get_personality
      ~TuberMethods._get_syslog_buffer
      ~TuberMethods._get_syslog_mask
      ~TuberMethods._set_personality
      ~TuberMethods._set_syslog_mask
      ~TuberMethods._sleep
      ~TuberMethods._syslog_test
      ~TuberMethods.reboot


.. rubric:: Tuber: Backplane monitor and control

.. autosummary::

      ~TuberMethods.is_backplane_present
      ~TuberMethods.is_bp_qsfp_present
      ~TuberMethods.set_bp_qsfp_gpio
      ~TuberMethods.set_bp_qsfp_led
      ~TuberMethods._backplane_eeprom_write_base64
      ~TuberMethods._bp_qsfp_eeprom_read_base64
      ~TuberMethods._bp_qsfp_eeprom_write_base64
      ~TuberMethods._get_backplane_ipmi
      ~TuberMethods._get_backplane_serial
      ~TuberMethods._get_backplane_type
      ~TuberMethods._get_backplane_version
      ~TuberMethods._initialize_backplane
      ~TuberMethods._reset_all_fpgas
      ~TuberMethods._reset_all_power
      ~TuberMethods.get_power_on_slot
      ~TuberMethods.set_power_on_slot
      ~TuberMethods.reset_arm_on_slot

.. rubric:: Tuber: Mezzanine monitor and control

.. autosummary::

      ~TuberMethods.is_mezzanine_present
      ~TuberMethods.set_mezzanine_power
      ~TuberMethods.get_mezzanine_current
      ~TuberMethods.get_mezzanine_power
      ~TuberMethods.get_mezzanine_voltage
      ~TuberMethods._get_mezzanine_ipmi
      ~TuberMethods._get_mezzanine_serial
      ~TuberMethods._get_mezzanine_type
      ~TuberMethods._get_mezzanine_version
      ~TuberMethods._mezzanine_eeprom_read_base64
      ~TuberMethods._mezzanine_eeprom_write_base64

.. rubric:: Tuber: Motherboard monitor and control

.. autosummary::


      ~TuberMethods._get_motherboard_ipmi
      ~TuberMethods._motherboard_eeprom_write_base64
      ~TuberMethods._motherboard_spi_flash_write_base64
      ~TuberMethods._qsfp_eeprom_read_base64
      ~TuberMethods._qsfp_eeprom_write_base64
      ~TuberMethods._set_fpga_bitstream_base64
      ~TuberMethods.clear_fpga_bitstream
      ~TuberMethods.get_backplane_current
      ~TuberMethods.get_backplane_power
      ~TuberMethods.get_backplane_slot
      ~TuberMethods.get_backplane_temperature
      ~TuberMethods.get_backplane_voltage
      ~TuberMethods.get_bp_qsfp_gpio
      ~TuberMethods.get_bp_qsfp_led
      ~TuberMethods.get_build_info
      ~TuberMethods.get_clock_source
      ~TuberMethods.get_led
      ~TuberMethods.get_motherboard_current
      ~TuberMethods.get_motherboard_power
      ~TuberMethods.get_motherboard_serial
      ~TuberMethods.get_motherboard_temperature
      ~TuberMethods.get_motherboard_voltage
      ~TuberMethods.get_qsfp_gpio
      ~TuberMethods.is_fpga_programmed
      ~TuberMethods.is_qsfp_present
      ~TuberMethods.is_voltage_nominal
      ~TuberMethods.set_led
      ~TuberMethods.set_pci_switch_direction
      ~TuberMethods.set_qsfp_gpio

.. currentmodule:: pychfpga.core.chFPGA_controller


.. rubric:: System info

.. autosummary::

      ~chFPGA_controller.get_fpga_core_cookie
      ~chFPGA_controller.get_fpga_firmware_cookie
      ~chFPGA_controller.get_fpga_firmware_timestamp
      ~chFPGA_controller.get_fpga_firmware_version
      ~chFPGA_controller.get_fpga_serial_number
      ~chFPGA_controller.get_fpga_application_cookie
      ~chFPGA_controller.get_version
      ~chFPGA_controller.get_fpga_bitstream_crc
      ~chFPGA_controller.is_fmc_present
      ~chFPGA_controller.is_fmc_present_for_channel

.. rubric:: IceBoard and IceCrate Hardware

.. autosummary::

      ~chFPGA_controller.open_hw
      ~chFPGA_controller.close_hw

      ~chFPGA_controller.fpga_i2c_set_port
      ~chFPGA_controller.fpga_i2c_write_read

.. rubric:: Channelizer and ADC data acqusition

.. autosummary::

      ~chFPGA_controller.set_channelizer
      ~chFPGA_controller.get_next_gain_bank
      ~chFPGA_controller.get_scaler_bypass
      ~chFPGA_controller.get_gains
      ~chFPGA_controller.load_gains
      ~chFPGA_controller.set_ADCDAQ_mode
      ~chFPGA_controller.set_FFT_bypass
      ~chFPGA_controller.set_FFT_shift
      ~chFPGA_controller.set_adc_delays
      ~chFPGA_controller.set_adc_mask
      ~chFPGA_controller.set_adc_mode
      ~chFPGA_controller.set_adcdaq_mode
      ~chFPGA_controller.set_ant_reset
      ~chFPGA_controller.get_data_source
      ~chFPGA_controller.get_data_width
      ~chFPGA_controller.get_default_channels
      ~chFPGA_controller.get_fft_bypass
      ~chFPGA_controller.get_fft_shift
      ~chFPGA_controller.get_FFT_bypass
      ~chFPGA_controller.get_FFT_shift
      ~chFPGA_controller.capture_adc_eye_diagram
      ~chFPGA_controller.compute_adc_delay_offsets
      ~chFPGA_controller.compute_adc_delays
      ~chFPGA_controller.tune_adc_delays
      ~chFPGA_controller.check_ramp_errors
      ~chFPGA_controller.get_adc_board
      ~chFPGA_controller.get_adc_delays
      ~chFPGA_controller.get_adc_mode
      ~chFPGA_controller.set_channelizer_outputs
      ~chFPGA_controller.set_data_source
      ~chFPGA_controller.set_data_width
      ~chFPGA_controller.set_default_channels
      ~chFPGA_controller.set_fft_bypass
      ~chFPGA_controller.set_fft_shift
      ~chFPGA_controller.set_funcgen_function
      ~chFPGA_controller.set_gains
      ~chFPGA_controller.set_offset_binary_encoding
      ~chFPGA_controller.set_scaler_bypass
      ~chFPGA_controller.switch_gains



.. rubric:: Embedded Firmware Correlator (if implemented)

.. autosummary::

      ~chFPGA_controller.compute_corr_output
      ~chFPGA_controller.set_corr_reset
      ~chFPGA_controller.start_corr_capture
      ~chFPGA_controller.start_correlator
      ~chFPGA_controller.stop_correlator
      ~chFPGA_controller.test_correlator
      ~chFPGA_controller.test_correlator_output


.. rubric:: Corner Turn engine

.. autosummary::

      ~chFPGA_controller.configure_crossbar
      ~chFPGA_controller.init_crossbars
      ~chFPGA_controller.set_send_flags
      ~chFPGA_controller.get_shuffle_status

.. rubric:: Reset and Synchronization

.. autosummary::

      ~chFPGA_controller.set_sync_source
      ~chFPGA_controller.set_trig
      ~chFPGA_controller.sync
      ~chFPGA_controller.pulse_ant_reset
      ~chFPGA_controller.reset

.. rubric:: Time and synchronization

.. autosummary::

      ~chFPGA_controller.capture_frame_time
      ~chFPGA_controller.capture_refclk_time
      ~chFPGA_controller.get_frame_number
      ~chFPGA_controller.get_sync_source
      ~chFPGA_controller.get_irigb_source
      ~chFPGA_controller.get_irigb_time
      ~chFPGA_controller.set_irigb_trigger_time


.. rubric:: Raw data capture

.. autosummary::

      ~chFPGA_controller.get_data_receiver
      ~chFPGA_controller.start_data_capture
      ~chFPGA_controller.stop_data_capture
      ~chFPGA_controller.set_global_trigger


.. rubric:: Board information

.. autosummary::

      ~chFPGA_controller.get_crate_id
      ~chFPGA_controller.get_string_id
      ~chFPGA_controller.get_slot_number
      ~chFPGA_controller.get_handler_name
      ~chFPGA_controller.get_id
      ~chFPGA_controller.set_cache


.. rubric:: SMA outputs

.. autosummary::

      ~chFPGA_controller.get_user_output_source
      ~chFPGA_controller.set_pwm
      ~chFPGA_controller.set_user_output_source

.. rubric:: Status and Monitoring

.. autosummary::

      ~chFPGA_controller.get_status
      ~chFPGA_controller.get_total_power
      ~chFPGA_controller.get_temperatures
      ~chFPGA_controller.status
      ~chFPGA_controller.get_shuffle_status
      ~chFPGA_controller.update_config
      ~chFPGA_controller.get_config

.. rubric:: Bitstream management

.. autosummary::

      ~chFPGA_controller.set_fpga_bitstream_crc
      ~chFPGA_controller.get_fpga_bitstream
      ~chFPGA_controller.register_fpga_bitstream
      ~chFPGA_controller.set_fpga_bitstream
      ~chFPGA_controller.get_fpga_bitstream_crc


Class attributes (by category)
------------------------------

.. rubric:: Memory map addressing

The following define the chFPGA memory map fo the registers accessed through the direct FPGA UDP-Ethernet interface. This addresss space is separate from the ARM-FPGA SPI memory map. The addresses are hard-coded in the FPGA firmware and are essentially constants that must match with it.

More details on the UDP MMI protocol  can be found in the :class:`FpgaMmi` class, which implement the basic read/write commands on the Python side. In a nutshell, read/write commands consist in a 3 bit operation code, 2-bit encoded length field for read operations, and 19 bits of address, for a total if 24-bit (3 bytes) command word. The address space is divided hierarchically where address bits 19:17 select the top system (CHANNELIZER, CROSSBAR1,2 or 3, CORRELATOR etc.), with each of these system assigning address bits to sub- and sub-subsystem (each subsystems can subdivine memory differently). Within this single address space coexist three actual type of storage, which is determined by the operation code:

      - Control registers can be written and always read back. Masked writes are possible for efficient bitfield operations.
      - Status registers are read only
      - RAM/DRP access block RAMs or  Xilinx embedded blocks that offer their own memory map interface such as PLLs, GTXes etc.

-------

.. autosummary::

   ~chFPGA_controller._SYSTEM_BASE_ADDR
   ~chFPGA_controller._CHAN_BASE_ADDR
   ~chFPGA_controller._CROSSBAR1_BASE_ADDR
   ~chFPGA_controller._GPU_LINK_BASE_ADDR
   ~chFPGA_controller._CROSSBAR3_BASE_ADDR
   ~chFPGA_controller._CORR_BASE_ADDR
   ~chFPGA_controller._BP_SHUFFLE_BASE_ADDR
   ~chFPGA_controller._CROSSBAR2_BASE_ADDR
   ~chFPGA_controller._CHAN_ADDR_INCREMENT
   ~chFPGA_controller._CROSSBAR_ADDR_INCREMENT
   ~chFPGA_controller._GPU_LINK_ADDR_INCREMENT
   ~chFPGA_controller._CORR_ADDR_INCREMENT
   ~chFPGA_controller._BP_SHUFFLE_ADDR_INCREMENT
   ~chFPGA_controller._CHAN_SUBMODULE_ADDR_INCREMENT
   ~chFPGA_controller._SYSTEM_GPIO_BASE_ADDR
   ~chFPGA_controller._SYSTEM_SYSMON_BASE_ADDR
   ~chFPGA_controller._SYSTEM_FREQ_CTR_BASE_ADDR
   ~chFPGA_controller._SYSTEM_SPI_BASE_ADDR
   ~chFPGA_controller._SYSTEM_REFCLK_BASE_ADDR

The `_SYSTEM_I2C_BASE_ADDR` is defined elsewhere.


.. rubric:: Memory map addressing

.. autosummary::

      ~chFPGA_controller.ADC_MODE_NAMES
      ~chFPGA_controller.FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR
      ~chFPGA_controller.FPGA_APPLICATION_FLAGS_ADDR
      ~chFPGA_controller.FPGA_CORE_FIRMWARE_COOKIE_ADDR
      ~chFPGA_controller.FPGA_FIRMWARE_CRC32_ADDR
      ~chFPGA_controller.FPGA_FIRMWARE_TIMESTAMP_ADDR
      ~chFPGA_controller.FPGA_SERIAL_NUMBER_LSW_ADDR
      ~chFPGA_controller.FPGA_SERIAL_NUMBER_MSW_ADDR
      ~chFPGA_controller.NUMBER_OF_FMC_SLOTS
      ~chFPGA_controller.crate
      ~chFPGA_controller.detect_irigb_source
      .. ~chFPGA_controller.fpga_ip_addr
      ~chFPGA_controller.hostname
      ~chFPGA_controller.init
      ~chFPGA_controller.interface_ip_addr
      ~chFPGA_controller.is_irigb_before_trigger_time
      ~chFPGA_controller.mezzanine
      ~chFPGA_controller.open
      ~chFPGA_controller.parent
      ~chFPGA_controller.part_number
      ~chFPGA_controller.ping
      ~chFPGA_controller.serial
      ~chFPGA_controller.slot
      ~chFPGA_controller.tuber_objname
      ~chFPGA_controller.tuber_uri
      ~chFPGA_controller.zero_target_irigb_year_and_day


chFPGA_controller Class
-----------------------

Initialization
**************

The initialization of an IceBoard and its firmware is done in 3 stages:
      1) `__init__` creates `chFPGA_controller` intance but does not attempt to communicate with the IceBoard (neitherthe ARM nor the FPGA). This allows the hardware map to be populated with `chFPGA_controller` instances even if the boards are not present or are not turned on yet. After `init`, the user can issue commands that require communication with the ARM processor only, and the connection will be established at that time.
      2) `open` establish UDP communications with the FPGA, and the FPGA configuration is read only. Python objecs are created.
      3) `init` Initializes the python objects and the chFPGA firmare registers.

.. automethod:: chFPGA_controller.__init__(*see below*)
.. automethod:: chFPGA_controller.open(*see below*)
.. automethod:: chFPGA_controller.close
.. automethod:: chFPGA_controller.init(*see below*)
.. automethod:: chFPGA_controller.is_open


.. automethod:: chFPGA_controller.get_config (*see below*)


UDP Networking
**************

.. automethod:: chFPGA_controller.open_core
.. automethod:: chFPGA_controller.close_core
.. automethod:: chFPGA_controller.is_core_open
.. automethod:: chFPGA_controller.set_fpga_control_networking_parameters
.. automethod:: chFPGA_controller.set_data_target_address
.. automethod:: chFPGA_controller.get_local_data_port_number
.. automethod:: chFPGA_controller.set_local_data_port_number

UDP MMI command system
**********************

.. automethod:: chFPGA_controller.mmi_read
.. automethod:: chFPGA_controller.mmi_write
.. automethod:: chFPGA_controller.check_command_count
.. automethod:: chFPGA_controller.pulse_bit
.. automethod:: chFPGA_controller.read_bit
.. automethod:: chFPGA_controller.fpga_mmi_read
.. automethod:: chFPGA_controller.fpga_mmi_write
.. automethod:: chFPGA_controller.write_bit
.. automethod:: chFPGA_controller.write_mask
.. automethod:: chFPGA_controller.test_speed
.. automethod:: chFPGA_controller.ping_fpga


ADC management
**************

.. automethod:: chFPGA_controller.set_adc_delays


Tuber methods
*************


Channelizer and ADC data acqusition
***********************************

.. automethod:: chFPGA_controller.set_channelizer
.. automethod:: chFPGA_controller.get_next_gain_bank
.. automethod:: chFPGA_controller.get_scaler_bypass
.. automethod:: chFPGA_controller.get_gains
.. automethod:: chFPGA_controller.load_gains
.. automethod:: chFPGA_controller.set_ADCDAQ_mode
.. automethod:: chFPGA_controller.set_FFT_bypass
.. automethod:: chFPGA_controller.set_FFT_shift
.. automethod:: chFPGA_controller.set_adc_delays
.. automethod:: chFPGA_controller.set_adc_mask
.. automethod:: chFPGA_controller.set_adc_mode
.. automethod:: chFPGA_controller.set_adcdaq_mode
.. automethod:: chFPGA_controller.set_ant_reset
.. automethod:: chFPGA_controller.get_data_source
.. automethod:: chFPGA_controller.get_data_width
.. automethod:: chFPGA_controller.get_default_channels
.. automethod:: chFPGA_controller.get_fft_bypass
.. automethod:: chFPGA_controller.get_fft_shift
.. automethod:: chFPGA_controller.get_FFT_bypass
.. automethod:: chFPGA_controller.get_FFT_shift
.. automethod:: chFPGA_controller.capture_adc_eye_diagram
.. automethod:: chFPGA_controller.compute_adc_delay_offsets
.. automethod:: chFPGA_controller.compute_adc_delays
.. automethod:: chFPGA_controller.tune_adc_delays
.. automethod:: chFPGA_controller.check_ramp_errors
.. automethod:: chFPGA_controller.get_adc_board
.. automethod:: chFPGA_controller.get_adc_delays
.. automethod:: chFPGA_controller.get_adc_mode
.. automethod:: chFPGA_controller.set_channelizer_outputs
.. automethod:: chFPGA_controller.set_data_source
.. automethod:: chFPGA_controller.set_data_width
.. automethod:: chFPGA_controller.set_default_channels
.. automethod:: chFPGA_controller.set_fft_bypass
.. automethod:: chFPGA_controller.set_fft_shift
.. automethod:: chFPGA_controller.set_funcgen_function
.. automethod:: chFPGA_controller.set_gains
.. automethod:: chFPGA_controller.set_offset_binary_encoding
.. automethod:: chFPGA_controller.set_scaler_bypass
.. automethod:: chFPGA_controller.switch_gains

Corner-turn Engine configuration
********************************


.. automethod:: chFPGA_controller.configure_crossbar
.. automethod:: chFPGA_controller.init_crossbars
.. automethod:: chFPGA_controller.set_send_flags
.. automethod:: chFPGA_controller.get_shuffle_status
