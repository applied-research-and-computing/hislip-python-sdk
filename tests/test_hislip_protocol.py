"""Tests for HiSLIPProtocol — IVI-6.1 HiSLIP server."""

import socket
import struct
import threading
import time

import pytest
from hislip_instruments.instrument import InstrumentBase
from hislip_instruments.protocol import (
    FATAL_BAD_MSG_TYPE,
    FATAL_UNIDENTIFIED,
    HEADER_FMT,
    HEADER_SIZE,
    HiSLIPProtocol,
    MSG_ASYNC_DEVICE_CLEAR,
    MSG_ASYNC_DEVICE_CLEAR_ACK,
    MSG_ASYNC_INITIALIZE,
    MSG_ASYNC_INITIALIZE_RESPONSE,
    MSG_ASYNC_LOCK,
    MSG_ASYNC_LOCK_RESPONSE,
    MSG_ASYNC_MAX_MSG_SIZE,
    MSG_ASYNC_MAX_MSG_SIZE_RESPONSE,
    MSG_ASYNC_SERVICE_REQUEST,
    MSG_ASYNC_STATUS_QUERY,
    MSG_ASYNC_STATUS_RESPONSE,
    MSG_DATA_END,
    MSG_DEVICE_CLEAR_ACK,
    MSG_DEVICE_CLEAR_COMPLETE,
    MSG_FATAL_ERROR,
    MSG_INITIALIZE,
    MSG_INITIALIZE_RESPONSE,
    MSG_TRIGGER,
    VENDOR_ID,
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

    def test_async_initialize_returns_vendor_id(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sync_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sync_sock.connect((proto.host, proto.port))
        _send_msg(sync_sock, MSG_INITIALIZE, payload=b"hislip0")
        msg = _recv_msg(sync_sock)
        session_id = msg[2] & 0xFFFF

        async_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        async_sock.connect((proto.host, proto.port))
        _send_msg(async_sock, MSG_ASYNC_INITIALIZE, msg_param=session_id)
        msg = _recv_msg(async_sock)
        assert msg is not None
        assert msg[0] == MSG_ASYNC_INITIALIZE_RESPONSE
        assert msg[2] == VENDOR_ID

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
        yield sync_sock, async_sock, session_id, proto, instrument
        sync_sock.close()
        async_sock.close()
        instrument.stop()

    def test_idn_query(self, session):
        sync_sock, async_sock, session_id, proto, instrument = session
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
        sync_sock, async_sock, session_id, proto, instrument = session
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=2, payload=b"*OPC?\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[3].decode("ascii").strip() == "1"

    def test_write_command_no_response(self, session):
        """Write commands (non-query) should not produce a sync response."""
        sync_sock, async_sock, session_id, proto, instrument = session
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
        yield sync_sock, async_sock, session_id, proto, instrument
        sync_sock.close()
        async_sock.close()
        instrument.stop()

    def test_max_message_size_negotiation(self, session):
        sync_sock, async_sock, session_id, proto, instrument = session
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
        sync_sock, async_sock, session_id, proto, instrument = session
        _send_msg(async_sock, MSG_ASYNC_STATUS_QUERY, control_code=0, msg_param=1)

        msg = _recv_msg(async_sock)
        assert msg is not None
        msg_type, control_code, msg_param, payload = msg
        assert msg_type == MSG_ASYNC_STATUS_RESPONSE
        # STB should be a valid byte
        assert 0 <= control_code <= 255

    def test_status_after_query_has_mav(self, session):
        """After a successful query, MAV bit should be set in STB."""
        sync_sock, async_sock, session_id, proto, instrument = session

        # Send a query to generate a response (sets MAV)
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=1, payload=b"*IDN?\n")
        _recv_msg(sync_sock)  # consume response

        # Now check status on async channel
        _send_msg(async_sock, MSG_ASYNC_STATUS_QUERY, control_code=0, msg_param=2)
        msg = _recv_msg(async_sock)
        assert msg is not None
        stb = msg[1]  # control_code = STB
        assert stb & 0x10  # MAV bit (bit 4) should be set


class TestHiSLIPErrorHandling:
    """FatalError responses for protocol violations."""

    def test_wrong_sub_address_gets_fatal_error(self):
        proto = HiSLIPProtocol(sub_address="hislip0")
        instrument = _make_instrument(proto)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((proto.host, proto.port))
        _send_msg(sock, MSG_INITIALIZE, payload=b"hislip99")

        msg = _recv_msg(sock)
        assert msg is not None
        assert msg[0] == MSG_FATAL_ERROR
        assert msg[1] == FATAL_UNIDENTIFIED

        sock.close()
        instrument.stop()

    def test_invalid_first_message_gets_fatal_error(self):
        """Sending Data as the first message should get a FatalError."""
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((proto.host, proto.port))
        # Send MSG_DATA_END as first message (not Initialize)
        _send_msg(sock, MSG_DATA_END, payload=b"*IDN?\n")

        msg = _recv_msg(sock)
        assert msg is not None
        assert msg[0] == MSG_FATAL_ERROR
        assert msg[1] == FATAL_BAD_MSG_TYPE

        sock.close()
        instrument.stop()

    def test_unknown_async_session_gets_fatal_error(self):
        """Async init with a bogus session ID should get a FatalError."""
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((proto.host, proto.port))
        _send_msg(sock, MSG_ASYNC_INITIALIZE, msg_param=9999)

        msg = _recv_msg(sock)
        assert msg is not None
        assert msg[0] == MSG_FATAL_ERROR
        assert msg[1] == FATAL_UNIDENTIFIED

        sock.close()
        instrument.stop()

    def test_engine_exception_doesnt_kill_session(self):
        """If the engine raises, the session should survive and return an error."""
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        # Register a handler that raises
        def bad_handler(_cmd):
            raise RuntimeError("boom")

        instrument.engine.register_handler("BOOM", bad_handler)

        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)

        # Send the bad command
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=1, payload=b"BOOM\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[0] == MSG_DATA_END
        # Should get an error response string
        response = msg[3].decode("ascii").strip()
        assert "error" in response.lower()

        # Session should still be alive — send a normal query
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=2, payload=b"*IDN?\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[3].decode("ascii").strip() == "TEST,HISLIP,SN001,1.0"

        sync_sock.close()
        async_sock.close()
        instrument.stop()


class TestHiSLIPSessionCleanup:
    """Session lifecycle and cleanup."""

    def test_session_removed_after_disconnect(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)

        # Verify session exists
        assert session_id in proto._sessions

        # Disconnect
        sync_sock.close()
        async_sock.close()
        time.sleep(0.3)  # Allow cleanup threads to run

        # Session should be removed
        assert session_id not in proto._sessions

        instrument.stop()

    def test_stop_closes_all_sessions(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        # Connect two sessions
        sync1, async1, sid1 = _hislip_connect(proto.host, proto.port)
        sync2, async2, sid2 = _hislip_connect(proto.host, proto.port)

        assert sid1 in proto._sessions
        assert sid2 in proto._sessions

        # Stop should clear all sessions
        instrument.stop()
        time.sleep(0.1)

        assert len(proto._sessions) == 0

        # Clean up (sockets may already be closed)
        for s in (sync1, async1, sync2, async2):
            try:
                s.close()
            except OSError:
                pass


class TestHiSLIPLocking:
    """Exclusive lock management."""

    @pytest.fixture()
    def two_sessions(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sync1, async1, sid1 = _hislip_connect(proto.host, proto.port)
        sync2, async2, sid2 = _hislip_connect(proto.host, proto.port)

        yield (sync1, async1, sid1), (sync2, async2, sid2), proto, instrument

        for s in (sync1, async1, sync2, async2):
            try:
                s.close()
            except OSError:
                pass
        instrument.stop()

    def test_exclusive_lock_grant(self, two_sessions):
        (sync1, async1, sid1), _, proto, instrument = two_sessions

        # Request exclusive lock (control_code=1, timeout=0)
        _send_msg(async1, MSG_ASYNC_LOCK, control_code=1, msg_param=0)
        msg = _recv_msg(async1)
        assert msg is not None
        assert msg[0] == MSG_ASYNC_LOCK_RESPONSE
        assert msg[1] == 1  # success

    def test_exclusive_lock_conflict(self, two_sessions):
        (sync1, async1, sid1), (sync2, async2, sid2), proto, instrument = two_sessions

        # Session 1 acquires lock
        _send_msg(async1, MSG_ASYNC_LOCK, control_code=1, msg_param=0)
        msg = _recv_msg(async1)
        assert msg is not None
        assert msg[1] == 1  # success

        # Session 2 tries to acquire — should fail
        _send_msg(async2, MSG_ASYNC_LOCK, control_code=1, msg_param=0)
        msg = _recv_msg(async2)
        assert msg is not None
        assert msg[0] == MSG_ASYNC_LOCK_RESPONSE
        assert msg[1] == 3  # error / cannot grant

    def test_lock_release_and_reacquire(self, two_sessions):
        (sync1, async1, sid1), (sync2, async2, sid2), proto, instrument = two_sessions

        # Session 1 acquires
        _send_msg(async1, MSG_ASYNC_LOCK, control_code=1, msg_param=0)
        msg = _recv_msg(async1)
        assert msg[1] == 1

        # Session 1 releases (control_code=0)
        _send_msg(async1, MSG_ASYNC_LOCK, control_code=0)
        msg = _recv_msg(async1)
        assert msg is not None
        assert msg[1] == 1  # release success

        # Session 2 can now acquire
        _send_msg(async2, MSG_ASYNC_LOCK, control_code=1, msg_param=0)
        msg = _recv_msg(async2)
        assert msg is not None
        assert msg[1] == 1  # success


class TestHiSLIPDeviceClear:
    """Device clear handshake."""

    @pytest.fixture()
    def session(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)
        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)
        yield sync_sock, async_sock, session_id, proto, instrument
        sync_sock.close()
        async_sock.close()
        instrument.stop()

    def test_full_device_clear_handshake(self, session):
        """Complete 4-step device clear handshake."""
        sync_sock, async_sock, session_id, proto, instrument = session

        # Step 1: Client sends AsyncDeviceClear on async channel
        _send_msg(async_sock, MSG_ASYNC_DEVICE_CLEAR)

        # Step 2: Server responds with AsyncDeviceClearAcknowledge
        msg = _recv_msg(async_sock)
        assert msg is not None
        assert msg[0] == MSG_ASYNC_DEVICE_CLEAR_ACK

        # Step 3: Client sends DeviceClearComplete on sync channel
        _send_msg(sync_sock, MSG_DEVICE_CLEAR_COMPLETE, control_code=0)

        # Step 4: Server responds with DeviceClearAcknowledge on sync channel
        # (may also receive AsyncInterrupted on async channel first)
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[0] == MSG_DEVICE_CLEAR_ACK

    def test_device_clear_resets_message_id(self, session):
        """After device clear, message_id should reset to 0xFFFFFF00."""
        sync_sock, async_sock, session_id, proto, instrument = session

        # Send a command to advance message_id
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=42, payload=b"*IDN?\n")
        _recv_msg(sync_sock)

        # Perform device clear
        _send_msg(async_sock, MSG_ASYNC_DEVICE_CLEAR)
        _recv_msg(async_sock)  # AsyncDeviceClearAck
        _send_msg(sync_sock, MSG_DEVICE_CLEAR_COMPLETE, control_code=0)

        # Consume DeviceClearAck (and possibly AsyncInterrupted)
        _recv_msg(sync_sock)

        # Send another command — verify session still works
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=0xFFFFFF00, payload=b"*OPC?\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[0] == MSG_DATA_END
        assert msg[3].decode("ascii").strip() == "1"


class TestHiSLIPSRQ:
    """Service Request push to async channels."""

    def test_srq_push_to_async_channel(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)

        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)

        # Generate an SRQ from the engine
        instrument.engine.generate_srq()

        # Should receive AsyncServiceRequest on the async channel
        msg = _recv_msg(async_sock, timeout=2.0)
        assert msg is not None
        assert msg[0] == MSG_ASYNC_SERVICE_REQUEST
        # control_code should contain STB
        stb = msg[1]
        assert isinstance(stb, int)

        sync_sock.close()
        async_sock.close()
        instrument.stop()


class TestEventDecorators:
    """on_trigger and on_clear event callbacks."""

    @pytest.fixture()
    def session(self):
        proto = HiSLIPProtocol()
        instrument = _make_instrument(proto)
        sync_sock, async_sock, session_id = _hislip_connect(proto.host, proto.port)
        yield sync_sock, async_sock, session_id, proto, instrument
        sync_sock.close()
        async_sock.close()
        instrument.stop()

    def test_on_trigger_decorator(self, session):
        sync_sock, async_sock, session_id, proto, instrument = session

        triggered = threading.Event()

        @proto.on_trigger
        def handle_trigger():
            triggered.set()

        # Send MSG_TRIGGER on sync channel
        _send_msg(sync_sock, MSG_TRIGGER, msg_param=1)

        assert triggered.wait(timeout=2.0), "on_trigger callback was not invoked"

    def test_on_clear_decorator(self, session):
        sync_sock, async_sock, session_id, proto, instrument = session

        cleared = threading.Event()

        @proto.on_clear
        def handle_clear():
            cleared.set()

        # Perform 4-step device clear handshake
        # Step 1: AsyncDeviceClear on async channel
        _send_msg(async_sock, MSG_ASYNC_DEVICE_CLEAR)
        # Step 2: Receive AsyncDeviceClearAck
        msg = _recv_msg(async_sock)
        assert msg is not None
        assert msg[0] == MSG_ASYNC_DEVICE_CLEAR_ACK
        # Step 3: DeviceClearComplete on sync channel
        _send_msg(sync_sock, MSG_DEVICE_CLEAR_COMPLETE, control_code=0)
        # Step 4: Receive DeviceClearAck
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[0] == MSG_DEVICE_CLEAR_ACK

        assert cleared.wait(timeout=2.0), "on_clear callback was not invoked"

    def test_trigger_without_callback(self, session):
        """MSG_TRIGGER without a registered callback should not crash."""
        sync_sock, async_sock, session_id, proto, instrument = session

        _send_msg(sync_sock, MSG_TRIGGER, msg_param=1)

        # Session should still be alive — send a query to verify
        _send_msg(sync_sock, MSG_DATA_END, control_code=1, msg_param=2, payload=b"*IDN?\n")
        msg = _recv_msg(sync_sock)
        assert msg is not None
        assert msg[3].decode("ascii").strip() == "TEST,HISLIP,SN001,1.0"
