# USP / FPGA UDP stack and UDP-based BSB (MGK7MB today). Not IceBoard-specific:
# any platform with UltraScale+ FPGA UDP MMI would use this mixin.

import asyncio
import socket

from wtl.metrics import Metrics

from pychfpga.hardware.interfaces import FPGAMmi


class chFPGA_UDP:
    """Mixin: FPGA UDP command stack, SGMII bring-up, and UDP/IP networking helpers."""

    UDP_STATUS_VECT_BITS = [
        ('tx_fifo_overflow', 17, 1),
        ('rx_fifo_overflow', 16, 1),
        ('sfp_remote_fault', 13, 1),
        ('sfp_duplex_mode', 12, 1),
        ('sfp_speed', 10, 2),
        ('rxnotintable', 6, 1),
        ('rxdisperr', 5, 1),
        ('link_sync', 1, 1),
        ('link_status', 0, 1)]


    _FPGA_CONTROL_BASE_PORT = 41000
    _BROADCAST_BASE_PORT = 41000

    async def open_udp_mmi_async(self):
        """Bring up direct FPGA UDP MMI (USP UDP stack). Requires ``BSB_TRANSPORT_UDP``."""
        assert self.BSB_TRANSPORT == type(self).BSB_TRANSPORT_UDP, (
            f'{self!r}: open_udp_mmi_async requires BSB_TRANSPORT_UDP')

        self.logger.debug(f'{self!r}: Opening UDP connection to the FPGA')

        if not self.fpga_ip_addr:
            ip_packed = socket.inet_aton(await self.mb.get_motherboard_ip_address())
            ip_tuple = tuple(c for c in ip_packed)
            ip_packed = bytes(self.fpga_ip_addr_fn(*ip_tuple))
            self.fpga_ip_addr = socket.inet_ntoa(ip_packed)

        # None is an explicit request for the crate/slot port. open_async
        # defaults to 0, which asks the OS for a free reply port.
        if self.local_control_port_number is None:
            if not self.slot or self.mb.get_crate_number() is None:
                self.local_control_port_number = 0
                self.logger.debug(
                    f'{self!r}: Cannot use slot/crate_number-based UDP port number '
                    f'for UDP control channel. There is no slot or crate_number'
                    f' info. Using OS-assigned random port')
            else:
                self.local_control_port_number = (
                    self._FPGA_CONTROL_BASE_PORT
                    + 16 * self.mb.get_crate_number(0)
                    + (self.slot - 1))

        self.logger.info(
            f'{self!r}: Opening FPGA MMI with FPGA=({self.fpga_ip_addr}:{self.fpga_control_port_number}),  '
            f'local=({self.interface_ip_addr}:{self.local_control_port_number})')

        self.mmi = FPGAMmi(
            fpga_ip_addr=self.fpga_ip_addr,
            fpga_port_number=self.fpga_control_port_number,
            interface_ip_addr=self.interface_ip_addr,
            local_port_number=self.local_control_port_number,
            udp_retries=self.udp_retries,
            parent=self)

        self.mmi.open()
        self.local_control_port_number = self.mmi.local_port_number
        self.fpga_control_port_number = self.mmi.fpga_port_number

        self.fpga_mac_addr = await self.set_fpga_udp_control_networking_parameters_async(
            fpga_ip_addr=self.fpga_ip_addr,
            fpga_port_number=self.fpga_control_port_number)

        for trial in range(10):
            self.logger.debug(
                f"{self!r}: Initializing SFP in SGMII mode with autonegociation (trial {trial+1})")
            await self.set_sgmii_config_vector(an=True, reset=1)
            await self.set_sgmii_config_vector(an=True, reset=0)
            await asyncio.sleep(0.150)
            status = await self.get_sgmii_status_vector()
            if status & 1:
                self.logger.info(
                    f"{self!r}: SFP successfully established link with autonegociation (1000BASE-X mode)")
                break
            self.logger.debug(
                f"{self!r}: SFP did not respond to autonegociation on trial {trial+1}. "
                f"It might not support it. Retrying without autonegociation.")
            await self.set_sgmii_config_vector(an=False, reset=1)
            await self.set_sgmii_config_vector(an=False, reset=0)
            await asyncio.sleep(0.150)
            status = await self.get_sgmii_status_vector()
            if status & 1:
                self.logger.info(
                    f"{self!r}: SFP successfully established link without autonegociation (SGMII mode)")
                break
            raise IOError(f"{self!r}: Cannot get the SFP module to establish a link")

        self.logger.debug(f"{self!r}: Clearing FPGAs UDP communication stack")
        await self.reset_fpga_udp_stack()

        self.logger.info(
            f"{self!r}: UDP communication link with the FPGA established "
            f"at {self.fpga_ip_addr}:{self.fpga_control_port_number}")

        return self.mmi

    async def get_fpga_udp_metrics_async(self):
        """UDP metrics from FPGA core registers via MMI (USP UDP path only)."""
        if not self.is_open():
            return

        if self.BSB_TRANSPORT != type(self).BSB_TRANSPORT_UDP:
            return

        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.mb.get_crate_number())

        try:
            metrics.add('fpga_udp_error_current_count', value=self.mmi.error_counter)
            vect = await self.fpga_core_reg_read_async(self._SFP_STATUS_ADDR)
            for name, pos, width in self.UDP_STATUS_VECT_BITS:
                metrics.add('fpga_udp_' + name, value=(vect >> pos) & (2**width - 1))
        except IOError as e:
            self.logger.error(f'{self!r}: Error getting FPGA udp metrics. Error is {e!r}')
            self.udp_err_ctr += 1
            self.udp_err_msgs.append(f'{self!r}: Error getting FPGA udp metrics. Error is {e!r}')
        return metrics

    async def get_udp_status_async(self):
        """Return status bits for the SGMII/1000BASE-X interface."""
        vect = await self.fpga_core_reg_read_async(self._SFP_STATUS_ADDR)
        return {name: ((vect >> pos) & (2**width - 1))
                for name, pos, width in self.UDP_STATUS_VECT_BITS}

    async def clear_fpga_udp_errors(self, force=False, no_reset=False, max_trials=3):
        """Clear FPGA UDP communication errors (USP UDP MMI only)."""
        if self.BSB_TRANSPORT != type(self).BSB_TRANSPORT_UDP:
            return

        trial = 1
        while True:
            error_count = getattr(self.mmi, 'error_counter', 0) if hasattr(self.mmi, 'error_counter') else 0
            if force or error_count > 40:
                self.logger.warning(f'{self!r}: Resetting FPGA UDP stack (trial #{trial}/{max_trials})')

                if not no_reset:
                    await self.mb.reset_sfp()
                    await asyncio.sleep(0.1)
                    await self.reset_fpga_udp_stack()
                    await asyncio.sleep(0.1)
                self.logger.debug(f'{self!r}: Trying to read from FPGA UDP stack')
                try:
                    self.mmi.flush()
                    self.mmi.read(0, type=int, length=1, retry=-1, resync=1)
                    self.mmi.read(0, type=int, length=1, resync=1)
                    self.mmi.flush()
                    (cmd, rply) = self.GPIO.get_command_count()
                    self.mmi.send_counter = cmd
                    self.mmi.recv_counter = rply
                    break
                except IOError:
                    if trial >= max_trials:
                        self.logger.error(
                            f'{self!r}: cannot communicate with FPGA port '
                            f'after {trial} FPGA UDP stack resets')
                        break
                    self.logger.warning(
                        f'{self!r}: Still obtaining FPGA UDP errors '
                        f'after {trial} FPGA UDP stack reset. Retrying...')
                    trial += 1
                finally:
                    if hasattr(self.mmi, 'error_counter'):
                        self.mmi.error_counter = 0
                    self.logger.debug(f'{self!r}: Finished to attempt clearing FPGA UDP errors.')
            else:
                break

    async def reset_fpga_udp_stack(self):
        """Reset the FPGA UDP stack via ARM–FPGA SPI (before UDP may be usable)."""
        await self.mb.fpga_core_reg_spi_write_async(self._SFP_STATUS_ADDR, 3 << 30)
        await self.mb.fpga_core_reg_spi_write_async(self._SFP_STATUS_ADDR, 0 << 30)
        self.mmi.send_counter = 0

    async def set_sgmii_config_vector(
            self, sgmii=1, an=1, pause=0, duplex=1, reset=0,
            los=0, fault=0, an_trig=False, loopback=0, speed=2):
        """Configure SGMII / 1000BASE-X PMA via ARM–FPGA SPI (pre-UDP MMI)."""
        val = (loopback << 17) + (an << 20) + (los << 28)
        if sgmii:
            val |= (1 << 31) + (1 << 0) + (speed << 10) + (duplex << 12)
        else:
            val |= (duplex << 5) + (pause << 7) + (fault << 12)

        if reset:
            val |= 1 << 29
        if an_trig:
            val |= 1 << 30
        await self.mb.fpga_core_reg_spi_write_async(self._SFP_CONFIG_ADDR, val)
        return val

    async def get_sgmii_status_vector(self):
        """Read SGMII / 1000BASE-X status via ARM–FPGA SPI (pre-UDP MMI)."""
        val = await self.mb.fpga_core_reg_spi_read_async(self._SFP_STATUS_ADDR)
        self.logger.debug(f'{self!r}: SGMII/1000BASE-X status: '
                          f'link={bool(val&1)}, sync={bool(val&(1<<1))}, '
                          f'RUDI={(val>>2)&(0b11111):05b}, PHY={(val>>7)&1}, '
                          f'ERR={(val>>13)&1} ERRCODE={(val>>8)&3:02b}, '
                          f'speed={(val>>10)&3:02b}, duplex={(val>>12)&1}, '
                          f'pause={(val>>14)&3:02b}')
        return val

    async def set_fpga_udp_control_networking_parameters_async(
            self,
            fpga_mac_addr=None,
            fpga_ip_addr=None,
            fpga_port_number=None,
            fpga_command_reply_dest_port=0):
        """Set the MAC address, IP address and port number for the  FPGA's UDP-based command channel.
        
        The addresses are set via FPGA core registers, so they are accessible even if the FPGA's UDP stack networking addresses is not yet configured.

        
        Parameters:
            fpga_mac_addr (str): The MAC address of the command channel.
            fpga_ip_addr (str): The IP address of the command channel.
            fpga_port_number (int): The port number of the command channel. If None, the default port number is used.
            fpga_command_reply_dest_port (int): The port number to which the FPGA will send command replies.

        Returns:
            None
        """
        if fpga_port_number is None:
            fpga_port_number = self._FPGA_CONTROL_BASE_PORT

        fpga_ip_addr_bytes = socket.inet_aton(fpga_ip_addr)

        if fpga_mac_addr is None:
            fpga_mac_addr_bytes = bytes((0x12, 0x34)) + fpga_ip_addr_bytes
            fpga_mac_addr = ':'.join([f'{c:02X}' for c in fpga_mac_addr_bytes])
        else:
            fpga_mac_addr_bytes = bytes.fromhex(fpga_mac_addr.replace(':', ''))

        fpga_mac_addr_int = int.from_bytes(fpga_mac_addr_bytes, 'big')
        fpga_ip_addr_int = int.from_bytes(fpga_ip_addr_bytes, 'big')

        await self.mb.fpga_core_reg_spi_write_async(
            self._FPGA_MAC_ADDR_LSW_ADDR, fpga_mac_addr_int & 0xFFFFFFFF)
        await self.mb.fpga_core_reg_spi_write_async(
            self._FPGA_MAC_ADDR_MSW_CMD_LISTENING_PORT_ADDR,
            ((fpga_mac_addr_int >> 16) & 0xFFFF0000) | (fpga_port_number & 0xFFFF))
        await self.mb.fpga_core_reg_spi_write_async(self._FPGA_IP_ADDR_ADDR, fpga_ip_addr_int)

        word = await self.mb.fpga_core_reg_spi_read_async(self._CMD_REPLY_DEST_PORT_ADDR)
        await self.mb.fpga_core_reg_spi_write_async(
            self._CMD_REPLY_DEST_PORT_ADDR,
            (word & 0xFFFF0000) | (fpga_command_reply_dest_port & 0xFFFF))

        return fpga_mac_addr

    async def _set_local_udp_data_port_number_async(self, port):
        word = await self.fpga_core_reg_read_async(self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR)
        await self.fpga_core_reg_write_async(
            self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR,
            (word & 0xFFFF0000) | (port & 0xFFFF))

    async def _get_local_udp_data_port_number_async(self):
        return (await self.fpga_core_reg_read_async(self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR)) & 0xFFFF

    async def _set_udp_data_target_address_async(self, ip_addr=None, port=None, mac_addr=None):
        """Program UDP data destination MAC/IP/port in FPGA core registers.

        Called from :meth:`chFPGA.set_udp_data_target_address_async` when
        ``BSB_TRANSPORT_UDP`` is active. Use
        :meth:`_set_local_udp_data_port_number_async` to change only the port
        afterward.
        """
        ip_addr_int = int.from_bytes(socket.inet_aton(ip_addr), 'big') if ip_addr else 0
        mac_addr_int = int.from_bytes(bytes.fromhex(mac_addr.replace(':', '')), 'big') if mac_addr else 0

        self.logger.debug(
            f'{self!r}: setting data target address to '
            f'ip={ip_addr}({ip_addr_int}), port={port}({port}), '
            f'mac={mac_addr}({mac_addr_int})')

        await self.fpga_core_reg_write_async(self._FPGA_DATA_DEST_MAC_ADDR_LSW_ADDR, mac_addr_int & 0xFFFFFFFF)
        await self.fpga_core_reg_write_async(
            self._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR,
            (mac_addr_int >> 16) & 0xFFFF0000 | (port & 0xFFFF))
        await self.fpga_core_reg_write_async(self._FPGA_DATA_DEST_IP_ADDR_ADDR, ip_addr_int)
