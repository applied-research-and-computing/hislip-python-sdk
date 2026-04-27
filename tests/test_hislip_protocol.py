"""Tests for HiSLIPProtocol — IVI-6.1 HiSLIP server."""

import socket
import struct
import time

import pytest
from hislip_instruments.instrument import InstrumentBase
from hislip_instruments.protocol import (
    HiSLIPProtocol,
    HEADER_FMT,
    HEADER_SIZE,
    MSG_INITIALIZE,
    MSG_INITIALIZE_RESPONSE,
    MSG_ASYNC_INITIALIZE,
    MSG_ASYNC_INITIALIZE_RESPONSE,
    MSG_ASYNC_MAX_MSG_SIZE,
    MSG_ASYNC_MAX_MSG_SIZE_RESPONSE,
    MSG_DATA_END,
    MSG_ASYNC_STATUS_QUERY,
    MSG_ASYNC_STATUS_RESPONSE,
)


def _make_instrument(proto):
    instrument = InstrumentBase(
        manufacturer="TEST", model="HISLIP", serial="SN001", firmware="1.0",
        protocols=[proto],
    )
    instrument.start()
    time.sleep(0.1)
    return instrument


def _send_msg(sock, msg_type, control_code=0, msg_param=0, payload=b""):
    header = struct.pack(HEADER_FMT, b"HS", msg_type, control_code, msg_param, len(payload))
    sock.sendall(header + payload)


def _recv_msg(sock, timeout=2.0):
    sock.settimeout(timeout)
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(header))
        if not chunk:
            return None
        header += chunk
    prologue, msg_type, control_code, msg_param, payload_len = struct.unpack(HEADER_FMT, header)
    assert prologue == b"HS"
    payload = b""
    while len(payload) < payload_len:
        chunk = sock.recv(payload_len - len(payload))
        if not chunk:
            break
        payload += chunk
    return msg_type, control_code, msg_param, payload


def _hislip_connect(host, port, sub_address="hislip0"):
    """Establish a full HiSLIP session (sync + async channels)."""
    # Sync channel
    sync_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sync_sock.connect((host, port))
    _send_msg(sync_sock, MSG_INITIALIZE, payload=sub_address.encode("ascii"))
    msg = _recv_msg(sync_sock)
    assert msg is not None
    assert msg[0] == MSG_INITIALIZE_RESPONSE
    session_id = msg[2] & 0xFFFF

    # Async channel
    async_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    async_sock.connect((host, port))
    _send_msg(async_sock, MSG_ASYNC_INITIALIZE, msg_param=session_id)
    msg = _recv_msg(async_sock)
    assert msg is not None
    assert msg[0] == MSG_ASYNC_INITIALIZE_RESPONSE

    return sync_sock, async_sock, session_id


class TestHiSLIPLifecycle:
    """Start/stop and basic connectivity."""

    def test_start_and_stop(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)
        assert proto.port > 0
        instrument.stop()

    def test_resource_string(self):
        proto = HiSLIPProtocol(sub_address="hislip0")
        instrument = _make_instrument(proto)
        assert proto.resource_string == "TCPIP0::127.0.0.1::hislip0::INSTR"
        instrument.stop()


class TestHiSLIPInitialize:
    """Session initialization handshake."""

    def test_sync_initialize(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((proto.host, proto.port))
        _send_msg(sock, MSG_INITIALIZE, payload=b"hislip0")

        msg = _recv_msg(sock)
        assert msg is not None
        msg_type, control_code, msg_param, payload = msg
        assert msg_type == MSG_INITIALIZE_RESPONSE
        server_version = (msg_param >> 16) & 0xFFFF
        session_id = msg_param & 0xFFFF
        assert server_version == 0x0100
        assert session_id >= 1

        sock.close()
        instrument.stop()

    def test_async_initialize(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)
        assert session_id >= 1

        sync_sock.close()
        async_sock.close()
        instrument.stop()


class TestHiSLIPCommands:
    """Sending SCPI commands via HiSLIP."""

    @pytest.fixture()
    def session(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)
        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)
        yield sync_sock, async_sock, session_id, proto
        sync_sock.close()
        async_sock.close()
        instrument.stop()

    def test_idn_query(self, session):
        sync_sock, async_sock, session_id, proto = session
        command = b"*IDN?\n"
        message_id = 1

        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=message_id, payload=command)
        msg = _recv_msg(sync_sock)

        assert msg is not None
        msg_type, control_code, msg_param, payload = msg
        assert msg_type == MSG_DATA_END
        response = payload.decode("ascii").strip()
        assert response == "TEST,HISLIP,SN001,1.0"

    def test_opc_query(self, session):
        sync_sock, async_sock, session_id, proto = session
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=2, payload=b"*OPC?\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[3].decode("ascii").strip() == "1"

    def test_write_command_no_response(self, session):
        """Write commands (non-query) should not produce a sync response."""
        sync_sock, async_sock, session_id, proto = session
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=3, payload=b"*RST\n")
        # No DataEnd response expected for non-query
        # Send a follow-up query to verify server is still alive
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=4, payload=b"*OPC?\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[3].decode("ascii").strip() == "1"


class TestHiSLIPAsync:
    """Async channel operations."""

    @pytest.fixture()
    def session(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)
        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)
        yield sync_sock, async_sock, session_id, proto
        sync_sock.close()
        async_sock.close()
        instrument.stop()

    def test_max_message_size_negotiation(self, session):
        sync_sock, async_sock, session_id, proto = session
        proposed_size = 2 * 1024 * 1024  # 2 MB
        payload = struct.pack("!Q", proposed_size)
        _send_msg(async_sock, MSG_ASYNC_MAX_MSG_SIZE, payload=payload)

        msg = _recv_msg(async_sock)
        assert msg is not None
        msg_type, control_code, msg_param, resp_payload = msg
        assert msg_type == MSG_ASYNC_MAX_MSG_SIZE_RESPONSE
        negotiated = struct.unpack("!Q", resp_payload)[0]
        # Server should cap at its max (1 MB)
        assert negotiated <= proposed_size
        assert negotiated > 0

    def test_status_query(self, session):
        sync_sock, async_sock, session_id, proto = session
        _send_msg(async_sock, MSG_ASYNC_STATUS_QUERY, control_code=0, msg_param=1)

        msg = _recv_msg(async_sock)
        assert msg is not None
        msg_type, control_code, msg_param, payload = msg
        assert msg_type == MSG_ASYNC_STATUS_RESPONSE
        # STB should be a valid byte
        assert 0 <= control_code <= 255

    def test_status_after_query_has_mav(self, session):
        """After a successful query, MAV bit should be set in STB."""
        sync_sock, async_sock, session_id, proto = session

        # Send a query to generate a response (sets MAV)
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=1, payload=b"*IDN?\n")
        _recv_msg(sync_sock)  # consume response

        # Now check status on async channel
        _send_msg(async_sock, MSG_ASYNC_STATUS_QUERY, control_code=0, msg_param=2)
        msg = _recv_msg(async_sock)
        assert msg is not None
        stb = msg[1]  # control_code = STB
        assert stb & 0x10  # MAV bit (bit 4) should be set
