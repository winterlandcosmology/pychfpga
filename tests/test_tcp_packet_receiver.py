"""Unit tests for TCPPacketReceiver framing (no hardware)."""

import socket
import struct
import threading

import pytest

from pychfpga.common.tcp_packet_receiver import (
    DATA_TCP_COOKIE,
    DATA_TCP_HEADER_STRUCT,
    TCPPacketReceiver,
)


def _frame(counter: int, payload: bytes) -> bytes:
    return DATA_TCP_HEADER_STRUCT.pack(
        DATA_TCP_COOKIE, counter, counter ^ 0xFFFF, len(payload)
    ) + payload


def _serve_bytes(payload: bytes, ready: threading.Event, port_holder: list):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port_holder.append(srv.getsockname()[1])
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    conn.sendall(payload)
    conn.close()
    srv.close()


def _connect_client(data: bytes) -> socket.socket:
    ready = threading.Event()
    port_holder = []
    t = threading.Thread(target=_serve_bytes, args=(data, ready, port_holder), daemon=True)
    t.start()
    assert ready.wait(2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port_holder[0]))
    sock.settimeout(0.5)
    t.join(2)
    return sock


def test_tcp_packet_receiver_basic_frames():
    p0 = bytes([0xA0]) + bytes(range(15))
    p1 = bytes([0xA1]) + bytes(range(20, 40))
    sock = _connect_client(_frame(0, p0) + _frame(1, p1))
    pr = TCPPacketReceiver([sock], buf_size=8, max_packet_size=64, verbose=0)
    n = pr.receive_packets()
    assert n == 2
    assert pr.pkt_len[0] == len(p0)
    assert pr.pkt_len[1] == len(p1)
    assert bytes(pr.buf[0, : len(p0)]) == p0
    assert bytes(pr.buf[1, : len(p1)]) == p1


def test_tcp_packet_receiver_resync_after_garbage():
    good = _frame(3, b"\xA0" + b"xyz")
    sock = _connect_client(b"\x00\x01\x02" + good)
    pr = TCPPacketReceiver([sock], buf_size=4, max_packet_size=64, verbose=0)
    n = pr.receive_packets()
    assert n == 1
    assert bytes(pr.buf[0, :4]) == b"\xA0xyz"


def test_tcp_packet_receiver_rejects_bad_counter_inv():
    bad_hdr = struct.pack("<HHHH", DATA_TCP_COOKIE, 1, 0x1234, 3) + b"abc"
    good = _frame(2, b"\xA0OK")
    sock = _connect_client(bad_hdr + good)
    pr = TCPPacketReceiver([sock], buf_size=4, max_packet_size=64, verbose=0)
    n = pr.receive_packets()
    assert n == 1
    assert bytes(pr.buf[0, :3]) == b"\xA0OK"


def test_tcp_packet_receiver_split_reads():
    """Carry buffer reassembles a frame delivered in tiny chunks."""
    payload = b"\xA0" + b"Z" * 50
    frame = _frame(9, payload)

    ready = threading.Event()
    port_holder = []

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port_holder.append(srv.getsockname()[1])
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        for i in range(0, len(frame), 3):
            conn.sendall(frame[i : i + 3])
        conn.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port_holder[0]))
    sock.settimeout(0.5)
    pr = TCPPacketReceiver([sock], buf_size=4, max_packet_size=128, verbose=0)
    n = pr.receive_packets()
    t.join(2)
    assert n == 1
    assert bytes(pr.buf[0, : len(payload)]) == payload
