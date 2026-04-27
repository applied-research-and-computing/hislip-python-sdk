"""HiSLIP protocol server for virtual instruments.

Contains the Protocol ABC and the HiSLIPProtocol implementation.

HiSLIP (IVI-6.1) uses 16-byte framed messages over TCP, with separate
sync and async channels per client session.

Default port: 4880 (the HiSLIP standard port), but 0 can be used
for ephemeral port selection during testing.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import CommandEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol ABC
# ---------------------------------------------------------------------------

class Protocol(ABC):
    """Abstract base for instrument communication protocols.

    Subclasses implement the wire format and delegate command processing
    to the attached CommandEngine.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port  # 0 = pick an available ephemeral port
        self._engine: CommandEngine | None = None
        self._mdns_server = None

    def attach(self, engine: CommandEngine):
        """Called by InstrumentBase to give this protocol a command engine."""
        self._engine = engine

    @property
    def engine(self) -> CommandEngine:
        if self._engine is None:
            raise RuntimeError(
                f"{self.__class__.__name__} has no attached engine. "
                "Call attach() or pass this protocol to an InstrumentBase."
            )
        return self._engine

    @abstractmethod
    def start(self):
        """Start listening for client connections."""
        ...

    @abstractmethod
    def stop(self):
        """Stop the protocol server and release resources."""
        ...

    @property
    @abstractmethod
    def resource_string(self) -> str:
        """VISA-style resource string (VPP-4.3 canonical form).

        Example:
            TCPIP0::127.0.0.1::hislip0::INSTR  (HiSLIP)
        """
        ...

    @property
    def mdns_service_type(self) -> str | None:
        """mDNS service type for Zeroconf advertisement (e.g. '_hislip._tcp.local.').

        Return None (default) if this protocol does not support mDNS.
        """
        return None

    def register_mdns(self, instance_name: str) -> None:
        """Advertise this protocol via mDNS/Zeroconf.

        Requires the ``zeroconf`` package. Silently skips if not available
        or if the protocol has no mdns_service_type.
        """
        service_type = self.mdns_service_type
        if service_type is None:
            return
        try:
            from zeroconf import ServiceInfo, Zeroconf
            import socket as _socket
        except ImportError:
            logger.debug("zeroconf package not installed, skipping mDNS registration")
            return

        addr = _socket.inet_aton(self.host) if self.host != "0.0.0.0" else _socket.inet_aton("127.0.0.1")
        info = ServiceInfo(
            type_=service_type,
            name=f"{instance_name}.{service_type}",
            addresses=[addr],
            port=self.port,
            server=f"{instance_name}.local.",
        )
        try:
            zc = Zeroconf()
        except OSError:
            logger.warning("mDNS: could not bind to port 5353 (system mDNS daemon conflict), skipping")
            return
        zc.register_service(info)
        self._mdns_server = (zc, info)
        logger.info("mDNS: registered %s on port %d", service_type, self.port)

    def unregister_mdns(self) -> None:
        """Remove mDNS advertisement if active."""
        if self._mdns_server is not None:
            zc, info = self._mdns_server
            zc.unregister_service(info)
            zc.close()
            self._mdns_server = None


# ---------------------------------------------------------------------------
# HiSLIP constants and helpers
# ---------------------------------------------------------------------------

# HiSLIP message types
MSG_INITIALIZE = 0
MSG_INITIALIZE_RESPONSE = 1
MSG_FATAL_ERROR = 2
MSG_ERROR = 3
MSG_ASYNC_LOCK = 4
MSG_ASYNC_LOCK_RESPONSE = 5
MSG_DATA = 6
MSG_DATA_END = 7
MSG_DEVICE_CLEAR_COMPLETE = 8
MSG_DEVICE_CLEAR_ACK = 9
MSG_ASYNC_REMOTE_LOCAL_CONTROL = 10
MSG_ASYNC_REMOTE_LOCAL_RESPONSE = 11
MSG_TRIGGER = 12
MSG_INTERRUPTED = 13
MSG_ASYNC_INTERRUPTED = 14
MSG_ASYNC_MAX_MSG_SIZE = 15
MSG_ASYNC_MAX_MSG_SIZE_RESPONSE = 16
MSG_ASYNC_INITIALIZE = 17
MSG_ASYNC_INITIALIZE_RESPONSE = 18
MSG_ASYNC_DEVICE_CLEAR = 19
MSG_ASYNC_SERVICE_REQUEST = 20
MSG_ASYNC_STATUS_QUERY = 21
MSG_ASYNC_STATUS_RESPONSE = 22
MSG_ASYNC_DEVICE_CLEAR_ACK = 23

HEADER_FMT = "!2sBBIQ"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
DEFAULT_MAX_MSG_SIZE = 1 << 20  # 1 MB


def _pack_header(
    msg_type: int, control_code: int, message_parameter: int, payload_length: int
) -> bytes:
    return struct.pack(
        HEADER_FMT, b"HS", msg_type, control_code, message_parameter, payload_length
    )


def _recv_exact(sock: socket.socket, count: int) -> bytes | None:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _recv_message(
    sock: socket.socket,
) -> tuple[int, int, int, bytes] | None:
    """Receive a HiSLIP message. Returns (msg_type, control_code, msg_param, payload) or None."""
    header = _recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None

    prologue, msg_type, control_code, msg_param, payload_len = struct.unpack(
        HEADER_FMT, header
    )
    if prologue != b"HS":
        logger.error("Invalid HiSLIP prologue: %r", prologue)
        return None

    payload = b""
    if payload_len > 0:
        payload = _recv_exact(sock, payload_len)
        if payload is None:
            return None

    return msg_type, control_code, msg_param, payload


def _send_message(
    sock: socket.socket,
    msg_type: int,
    control_code: int = 0,
    msg_param: int = 0,
    payload: bytes = b"",
):
    header = _pack_header(msg_type, control_code, msg_param, len(payload))
    sock.sendall(header + payload)


# ---------------------------------------------------------------------------
# HiSLIP session and protocol
# ---------------------------------------------------------------------------

class _HislipSession:
    """State for one HiSLIP client session (sync + async channels)."""

    def __init__(self, session_id: int):
        self.session_id = session_id
        self.sync_sock: socket.socket | None = None
        self.async_sock: socket.socket | None = None
        self.write_buffer = b""
        self.message_id = 0xFFFFFF00
        self.max_msg_size = DEFAULT_MAX_MSG_SIZE
        self.lock = threading.Lock()
        self.mav = False  # Message AVailable
        self.clear_pending = threading.Event()  # Signals sync loop to handle clear


class HiSLIPProtocol(Protocol):
    """HiSLIP protocol server.

    Args:
        host: Bind address (default "127.0.0.1").
        port: Listen port (default 0 for ephemeral, use 4880 for standard).
        sub_address: HiSLIP sub-address (default "hislip0").
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        sub_address: str = "hislip0",
    ):
        super().__init__(host=host, port=port)
        self.sub_address = sub_address
        self._sessions: dict[int, _HislipSession] = {}
        self._sessions_lock = threading.Lock()
        self._next_session_id = 1
        self._server_socket: socket.socket | None = None
        self._running = False

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(10)
        self._running = True

        self.port = self._server_socket.getsockname()[1]

        accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        accept_thread.start()
        logger.info("HiSLIP server on %s:%d", self.host, self.port)

    def stop(self):
        self._running = False
        if self._server_socket:
            self._server_socket.close()

    @property
    def resource_string(self) -> str:
        return f"TCPIP0::{self.host}::{self.sub_address}::INSTR"

    @property
    def mdns_service_type(self) -> str | None:
        return "_hislip._tcp.local."

    # -- Connection handling -------------------------------------------------

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, addr = self._server_socket.accept()
                logger.debug("HiSLIP connection from %s", addr)
                thread = threading.Thread(
                    target=self._handle_connection,
                    args=(client_sock,),
                    daemon=True,
                )
                thread.start()
            except OSError:
                break

    def _handle_connection(self, sock: socket.socket):
        try:
            msg = _recv_message(sock)
            if msg is None:
                sock.close()
                return

            msg_type, control_code, msg_param, payload = msg

            if msg_type == MSG_INITIALIZE:
                self._handle_sync_init(sock, control_code, msg_param, payload)
            elif msg_type == MSG_ASYNC_INITIALIZE:
                self._handle_async_init(sock, msg_param)
            else:
                logger.warning("Unexpected first HiSLIP message type: %d", msg_type)
                sock.close()
        except Exception:
            logger.exception("Error handling HiSLIP connection")
            try:
                sock.close()
            except OSError:
                pass

    def _handle_sync_init(
        self,
        sock: socket.socket,
        control_code: int,
        msg_param: int,
        payload: bytes,
    ):
        with self._sessions_lock:
            session_id = self._next_session_id
            self._next_session_id += 1
            session = _HislipSession(session_id)
            session.sync_sock = sock
            self._sessions[session_id] = session

        sub_address = payload.decode("ascii", errors="replace") if payload else self.sub_address

        # Parse client protocol version from upper 16 bits of msg_param
        client_version = (msg_param >> 16) & 0xFFFF
        server_version = 0x0100  # We support HiSLIP 1.0
        # Negotiate: use minimum of client and server version
        negotiated_version = min(client_version, server_version) if client_version > 0 else server_version

        logger.info(
            "HiSLIP sync init: session %d, sub_address=%s, client_version=0x%04X, negotiated=0x%04X",
            session_id, sub_address, client_version, negotiated_version,
        )

        # InitializeResponse: msg_param = (negotiated_version << 16) | session_id
        # control_code: 0 = synchronized mode (no overlap)
        resp_param = (negotiated_version << 16) | (session_id & 0xFFFF)
        _send_message(sock, MSG_INITIALIZE_RESPONSE, control_code=0, msg_param=resp_param)

        self._sync_loop(sock, session)

    def _handle_async_init(self, sock: socket.socket, session_id_param: int):
        session_id_low = session_id_param & 0xFFFF
        with self._sessions_lock:
            session = self._sessions.get(session_id_low)

        if session is None:
            logger.error("HiSLIP async init with unknown session: %d", session_id_low)
            sock.close()
            return

        session.async_sock = sock
        logger.info("HiSLIP async init: session %d", session_id_low)

        vendor_param = 0x00005256  # "RV" for RustVisa
        _send_message(sock, MSG_ASYNC_INITIALIZE_RESPONSE, msg_param=vendor_param)

        self._async_loop(sock, session)

    # -- Message loops -------------------------------------------------------

    def _sync_loop(self, sock: socket.socket, session: _HislipSession):
        while self._running:
            msg = _recv_message(sock)
            if msg is None:
                logger.info("HiSLIP sync closed for session %d", session.session_id)
                break

            msg_type, control_code, msg_param, payload = msg

            if msg_type == MSG_DATA:
                with session.lock:
                    session.write_buffer += payload

            elif msg_type == MSG_DATA_END:
                with session.lock:
                    session.write_buffer += payload
                    session.message_id = msg_param
                    data = session.write_buffer.decode("ascii", errors="replace").strip()
                    session.write_buffer = b""

                logger.debug("HiSLIP command: %s", data)
                response = self.engine.process_command(data)

                if response is not None:
                    resp_bytes = (response + "\n").encode("ascii")
                    _send_message(
                        sock,
                        MSG_DATA_END,
                        control_code=0,
                        msg_param=session.message_id,
                        payload=resp_bytes,
                    )
                    with session.lock:
                        session.mav = True

            elif msg_type == MSG_TRIGGER:
                with session.lock:
                    session.message_id = msg_param
                logger.debug("HiSLIP trigger received")

            elif msg_type == MSG_DEVICE_CLEAR_COMPLETE:
                # Step 3 of 4-step device clear handshake (sync channel)
                # Execute the clear
                self.engine.process_command("*CLS")
                # Reset message_id per spec
                with session.lock:
                    session.message_id = 0xFFFFFF00
                    session.write_buffer = b""
                # Step 4: send DeviceClearAcknowledge
                # control_code = feature_bitmap (0 = synchronized mode)
                _send_message(sock, MSG_DEVICE_CLEAR_ACK, control_code=0)

            else:
                logger.warning("Unhandled HiSLIP sync message: %d", msg_type)

    def _async_loop(self, sock: socket.socket, session: _HislipSession):
        while self._running:
            msg = _recv_message(sock)
            if msg is None:
                logger.info("HiSLIP async closed for session %d", session.session_id)
                break

            msg_type, control_code, msg_param, payload = msg

            if msg_type == MSG_ASYNC_MAX_MSG_SIZE:
                if len(payload) >= 8:
                    proposed = struct.unpack("!Q", payload[:8])[0]
                    negotiated = min(proposed, DEFAULT_MAX_MSG_SIZE)
                    session.max_msg_size = negotiated
                    resp_payload = struct.pack("!Q", negotiated)
                    _send_message(sock, MSG_ASYNC_MAX_MSG_SIZE_RESPONSE, payload=resp_payload)

            elif msg_type == MSG_ASYNC_LOCK:
                # Stub: always grant lock
                _send_message(sock, MSG_ASYNC_LOCK_RESPONSE, control_code=1)

            elif msg_type == MSG_ASYNC_STATUS_QUERY:
                rmt = control_code & 1
                with session.lock:
                    if rmt:
                        session.mav = False
                    stb = self.engine.read_stb()
                    if session.mav:
                        stb |= 0x10  # MAV bit
                _send_message(
                    sock,
                    MSG_ASYNC_STATUS_RESPONSE,
                    control_code=stb,
                    msg_param=0,
                )

            elif msg_type == MSG_ASYNC_DEVICE_CLEAR:
                # Step 1 of 4-step device clear handshake (async channel)
                # Step 2: send AsyncDeviceClearAcknowledge
                # control_code = feature_bitmap (bit 0: 0 = synchronized mode)
                _send_message(sock, MSG_ASYNC_DEVICE_CLEAR_ACK, control_code=0)
                # Steps 3-4 happen on the sync channel when client sends
                # DeviceClearComplete — handled in _sync_loop

            elif msg_type == MSG_ASYNC_REMOTE_LOCAL_CONTROL:
                # Remote/local control — accept but no-op for virtual instruments
                _send_message(sock, MSG_ASYNC_REMOTE_LOCAL_RESPONSE, control_code=0)

            else:
                logger.warning("Unhandled HiSLIP async message: %d", msg_type)
