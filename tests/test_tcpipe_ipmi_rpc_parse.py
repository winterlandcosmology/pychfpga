import struct

from pychfpga.hardware.interfaces.tcpipe import TCPipe


def _pack_field(text):
    raw = text.encode('utf-8')
    return raw[:TCPipe.IPMI_FIELD_WIRE_LEN].ljust(TCPipe.IPMI_FIELD_WIRE_LEN, b'\x00')


def test_parse_motherboard_ipmi_reply():
    body = bytes([TCPipe.IPMI_RPC_VERSION])
    body += _pack_field('t0') + _pack_field('crs') + _pack_field('5')
    body += _pack_field('0037') + _pack_field('2026-01-01')
    reply = bytes([0]) + body
    tcp = TCPipe.__new__(TCPipe)
    info = tcp._parse_motherboard_ipmi_reply(reply)
    assert info['serial'] == '0037'
    assert info['product'] == 'crs'


def test_parse_backplane_ipmi_reply():
    body = bytes([TCPipe.IPMI_RPC_VERSION])
    body += struct.pack('<h', 9)
    body += bytes([TCPipe.BACKPLANE_IPMI_FLAG_PRESENT])
    for text in (
            't0', '4SBP', '000', '2', 't0', 'CRC4', '1', '001', 'CRC4', '001'):
        body += _pack_field(text)
    reply = bytes([0]) + body
    tcp = TCPipe.__new__(TCPipe)
    info = tcp._parse_backplane_ipmi_reply(reply)
    assert info['crate_slot'] == 9
    assert info['crate_model'] == 'CRC4'
    assert info['backplane_present'] is True
