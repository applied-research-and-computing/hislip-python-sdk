"""HiSLIP protocol server.

Contains the Protocol ABC, the HiSLIPProtocol implementation, and the
Flask-style HiSLIPServer entry point.

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
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .command import SCPICommand
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

# HiSLIP FatalError codes (IVI-6.1 Table 5)
FATAL_UNIDENTIFIED = 0
FATAL_BAD_MSG_TYPE = 1
FATAL_INIT_NOT_FIRST = 2
FATAL_MAX_CLIENTS = 3
FATAL_SECURE_NOT_SUPPORTED = 4

# Vendor ID: "CB" for Carbon (ASCII 0x43 0x42)
VENDOR_ID = 0x00004342

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
    max_payload: int = 0,
) -> tuple[int, int, int, bytes] | None:
    """Receive a HiSLIP message. Returns (msg_type, control_code, msg_param, payload) or None.

    If max_payload > 0, payloads exceeding that size cause an error return.
    """
    header = _recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None

    prologue, msg_type, control_code, msg_param, payload_len = struct.unpack(
        HEADER_FMT, header
    )
    if prologue != b"HS":
        logger.error("Invalid HiSLIP prologue: %r", prologue)
        return None

    if max_payload > 0 and payload_len > max_payload:
        logger.error("HiSLIP payload too large: %d > %d", payload_len, max_payload)
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


def _send_fatal_error(
    sock: socket.socket, error_code: int, message: str = ""
):
    """Send a FatalError message and close the connection."""
    payload = message.encode("ascii") if message else b""
    try:
        _send_message(sock, MSG_FATAL_ERROR, control_code=error_code, payload=payload)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HiSLIP lock manager
# ---------------------------------------------------------------------------

class _LockManager:
    """Manages HiSLIP exclusive/shared lock state across sessions."""

    def __init__(self):
        self._lock = threading.Lock()
        self._holder: int | None = None  # session_id holding exclusive lock

    def request_exclusive(self, session_id: int, timeout_ms: int) -> int:
        """Try to acquire exclusive lock.

        Returns: 1=success, 3=error (cannot grant), 0=fail (would need to wait).
        """
        with self._lock:
            if self._holder is None:
                self._holder = session_id
                return 1  # success
            if self._holder == session_id:
                return 1  # already held by this session
            return 3 if timeout_ms == 0 else 0  # fail

    def release(self, session_id: int) -> int:
        """Release lock held by session_id.

        Returns: 1=success exclusive released, 0=wasn't holding.
        """
        with self._lock:
            if self._holder == session_id:
                self._holder = None
                return 1
            return 0

    def release_for_session(self, session_id: int):
        """Release any locks held by a disconnecting session."""
        with self._lock:
            if self._holder == session_id:
                self._holder = None


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
        self.send_lock = threading.Lock()  # Prevents interleaved writes
        self.mav = False  # Message AVailable
        self.in_progress_message_id: int | None = None  # Set while processing a command
        self.clear_requested = False  # Set by async device clear
        self._closed = False

    def close(self):
        """Close both sockets for this session."""
        if self._closed:
            return
        self._closed = True
        for s in (self.sync_sock, self.async_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


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
        self._lock_manager = _LockManager()
        self._on_trigger: Callable[[], None] | None = None
        self._on_clear: Callable[[], None] | None = None

    def on_trigger(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Decorator for HiSLIP trigger events (MSG_TRIGGER).

        Usage::

            @protocol.on_trigger
            def handle_trigger():
                print("Trigger received!")
        """
        self._on_trigger = fn
        return fn

    def on_clear(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Decorator for HiSLIP device clear events (MSG_DEVICE_CLEAR_COMPLETE).

        Usage::

            @protocol.on_clear
            def handle_clear():
                print("Device cleared!")
        """
        self._on_clear = fn
        return fn

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(10)
        self._running = True

        self.port = self._server_socket.getsockname()[1]

        # Register SRQ callback to push to async channels
        self.engine.on_srq(self._push_srq)

        accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        accept_thread.start()
        logger.info("HiSLIP server on %s:%d", self.host, self.port)

    def stop(self):
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        # Close all active sessions
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    @property
    def resource_string(self) -> str:
        return f"TCPIP0::{self.host}::{self.sub_address}::INSTR"

    @property
    def mdns_service_type(self) -> str | None:
        return "_hislip._tcp.local."

    # -- SRQ push ------------------------------------------------------------

    def _push_srq(self):
        """Push AsyncServiceRequest to all sessions with async channels."""
        stb = self.engine.read_stb()
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.async_sock and not session._closed:
                try:
                    with session.send_lock:
                        _send_message(
                            session.async_sock,
                            MSG_ASYNC_SERVICE_REQUEST,
                            control_code=stb,
                        )
                except OSError:
                    pass

    # -- Session cleanup -----------------------------------------------------

    def _remove_session(self, session_id: int):
        """Remove a session from the sessions dict and release its locks."""
        self._lock_manager.release_for_session(session_id)
        with self._sessions_lock:
            self._sessions.pop(session_id, None)

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
                _send_fatal_error(sock, FATAL_BAD_MSG_TYPE, "Expected Initialize or AsyncInitialize")
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
        sub_address = payload.decode("ascii", errors="replace") if payload else ""

        # Validate sub-address
        if sub_address and sub_address != self.sub_address:
            logger.warning(
                "HiSLIP sync init: wrong sub-address %r (expected %r)",
                sub_address, self.sub_address,
            )
            _send_fatal_error(sock, FATAL_UNIDENTIFIED, f"Unknown sub-address: {sub_address}")
            return

        with self._sessions_lock:
            session_id = self._next_session_id
            self._next_session_id += 1
            session = _HislipSession(session_id)
            session.sync_sock = sock
            self._sessions[session_id] = session

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

        try:
            self._sync_loop(sock, session)
        finally:
            self._remove_session(session.session_id)

    def _handle_async_init(self, sock: socket.socket, session_id_param: int):
        session_id_low = session_id_param & 0xFFFF
        with self._sessions_lock:
            session = self._sessions.get(session_id_low)

        if session is None:
            logger.error("HiSLIP async init with unknown session: %d", session_id_low)
            _send_fatal_error(sock, FATAL_UNIDENTIFIED, "Unknown session ID")
            return

        session.async_sock = sock
        logger.info("HiSLIP async init: session %d", session_id_low)

        _send_message(sock, MSG_ASYNC_INITIALIZE_RESPONSE, msg_param=VENDOR_ID)

        self._async_loop(sock, session)

    # -- Message loops -------------------------------------------------------

    def _sync_loop(self, sock: socket.socket, session: _HislipSession):
        while self._running:
            msg = _recv_message(sock, max_payload=session.max_msg_size)
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
                    session.in_progress_message_id = msg_param
                    data = session.write_buffer.decode("ascii", errors="replace").strip()
                    session.write_buffer = b""

                logger.debug("HiSLIP command: %s", data)
                try:
                    response = self.engine.process_command(data)
                except Exception:
                    logger.exception("Engine error processing command: %s", data)
                    response = '-100,"Command error"'

                with session.lock:
                    session.in_progress_message_id = None

                if response is not None:
                    resp_bytes = (response + "\n").encode("ascii")
                    with session.send_lock:
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
                if self._on_trigger is not None:
                    self._on_trigger()

            elif msg_type == MSG_DEVICE_CLEAR_COMPLETE:
                # Step 3 of 4-step device clear handshake (sync channel)
                # Execute the clear
                self.engine.process_command("*CLS")
                if self._on_clear is not None:
                    self._on_clear()
                # Reset message_id per spec
                with session.lock:
                    session.message_id = 0xFFFFFF00
                    session.write_buffer = b""
                    session.in_progress_message_id = None
                    session.clear_requested = False
                # Send AsyncInterrupted on async channel to complete the clear
                if session.async_sock and not session._closed:
                    try:
                        with session.send_lock:
                            _send_message(
                                session.async_sock,
                                MSG_ASYNC_INTERRUPTED,
                                msg_param=session.message_id,
                            )
                    except OSError:
                        pass
                # Step 4: send DeviceClearAcknowledge
                # control_code = feature_bitmap (0 = synchronized mode)
                with session.send_lock:
                    _send_message(sock, MSG_DEVICE_CLEAR_ACK, control_code=0)

            else:
                logger.warning("Unhandled HiSLIP sync message: %d", msg_type)

    def _async_loop(self, sock: socket.socket, session: _HislipSession):
        try:
            while self._running:
                msg = _recv_message(sock, max_payload=session.max_msg_size)
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
                        with session.send_lock:
                            _send_message(
                                sock, MSG_ASYNC_MAX_MSG_SIZE_RESPONSE, payload=resp_payload
                            )

                elif msg_type == MSG_ASYNC_LOCK:
                    # control_code: 0=release, 1=exclusive
                    lock_timeout = msg_param  # timeout in ms
                    if control_code == 0:
                        result = self._lock_manager.release(session.session_id)
                        # Per spec: release returns 1=success exclusive, 0=wasn't holding
                        resp_code = 1 if result else 0
                    else:
                        resp_code = self._lock_manager.request_exclusive(
                            session.session_id, lock_timeout
                        )
                    with session.send_lock:
                        _send_message(
                            sock, MSG_ASYNC_LOCK_RESPONSE, control_code=resp_code
                        )

                elif msg_type == MSG_ASYNC_STATUS_QUERY:
                    rmt = control_code & 1
                    with session.lock:
                        if rmt:
                            session.mav = False
                        stb = self.engine.read_stb()
                        if session.mav:
                            stb |= 0x10  # MAV bit
                    with session.send_lock:
                        _send_message(
                            sock,
                            MSG_ASYNC_STATUS_RESPONSE,
                            control_code=stb,
                            msg_param=0,
                        )

                elif msg_type == MSG_ASYNC_DEVICE_CLEAR:
                    # Step 1 of 4-step device clear handshake (async channel)
                    with session.lock:
                        session.clear_requested = True
                        in_progress = session.in_progress_message_id
                    # Step 2: send AsyncDeviceClearAcknowledge
                    # control_code = feature_bitmap (bit 0: 0 = synchronized mode)
                    with session.send_lock:
                        _send_message(sock, MSG_ASYNC_DEVICE_CLEAR_ACK, control_code=0)
                    # If a command was in progress, send Interrupted on sync channel
                    if in_progress is not None and session.sync_sock and not session._closed:
                        try:
                            with session.send_lock:
                                _send_message(
                                    session.sync_sock,
                                    MSG_INTERRUPTED,
                                    msg_param=in_progress,
                                )
                        except OSError:
                            pass
                    # Steps 3-4 happen on the sync channel when client sends
                    # DeviceClearComplete — handled in _sync_loop

                elif msg_type == MSG_ASYNC_REMOTE_LOCAL_CONTROL:
                    # Remote/local control — acknowledge but no-op
                    with session.send_lock:
                        _send_message(
                            sock, MSG_ASYNC_REMOTE_LOCAL_RESPONSE, control_code=0
                        )

                else:
                    logger.warning("Unhandled HiSLIP async message: %d", msg_type)
        finally:
            # Async loop closing doesn't remove the session — sync side owns that.
            # But mark the async socket as gone so SRQ push skips it.
            session.async_sock = None


# ---------------------------------------------------------------------------
# HiSLIPServer — Flask-style entry point
# ---------------------------------------------------------------------------

class HiSLIPServer:
    """HiSLIP instrument server with a Flask-style decorator API.

    Quick start::

        from hislip_instruments import HiSLIPServer

        server = HiSLIPServer(manufacturer="ACME", model="DMM100", port=4880)

        @server.command("READ?")
        def read_measurement(cmd):
            return "1.234"

        @server.command("CONF:VOLT:DC")
        def configure_vdc(cmd):
            rng = cmd.arg(0, float)
            ...

        @server.on_trigger
        def handle_trigger():
            ...

        server.run()

    Args:
        manufacturer: Manufacturer name for ``*IDN?`` response.
        model: Model name for ``*IDN?`` response.
        serial: Serial number for ``*IDN?`` response.
        firmware: Firmware version for ``*IDN?`` response.
        ieee488: Enable IEEE 488.2 common commands (default True).
        command_separator: Command separator character (default ";").
        host: Bind address (default "127.0.0.1").
        port: Listen port (default 4880).
        sub_address: HiSLIP sub-address (default "hislip0").
    """

    def __init__(
        self,
        *,
        manufacturer: str = "MOCK",
        model: str = "Instrument",
        serial: str = "SN001",
        firmware: str = "1.0.0",
        ieee488: bool = True,
        command_separator: str = ";",
        host: str = "127.0.0.1",
        port: int = 4880,
        sub_address: str = "hislip0",
    ):
        from .engine import CommandEngine

        self.engine = CommandEngine(
            manufacturer=manufacturer,
            model=model,
            serial=serial,
            firmware=firmware,
            ieee488=ieee488,
            command_separator=command_separator,
        )
        self._protocol = HiSLIPProtocol(host=host, port=port, sub_address=sub_address)
        self._protocol.attach(self.engine)
        self._stop_event = threading.Event()

    def command(self, prefix: str) -> Callable:
        """Decorator to register a SCPI command handler.

        Usage::

            @server.command("READ?")
            def read_measurement(cmd):
                return "1.234"
        """
        def decorator(fn: Callable[[SCPICommand], str | None]) -> Callable[[SCPICommand], str | None]:
            self.engine.register_handler(prefix, fn)
            return fn
        return decorator

    @property
    def on_trigger(self):
        """Decorator for HiSLIP trigger events.

        Usage::

            @server.on_trigger
            def handle_trigger():
                start_acquisition()
        """
        return self._protocol.on_trigger

    @property
    def on_clear(self):
        """Decorator for HiSLIP device clear events.

        Usage::

            @server.on_clear
            def handle_clear():
                reset_hardware()
        """
        return self._protocol.on_clear

    def start(self):
        """Start the server in the background (non-blocking)."""
        self._protocol.start()

    def stop(self):
        """Stop the server and release resources."""
        self._protocol.unregister_mdns()
        self._protocol.stop()
        self._stop_event.set()

    def wait(self):
        """Block until :meth:`stop` is called. Handles Ctrl-C gracefully."""
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            self.stop()

    def run(self):
        """Start the server and block until interrupted.

        Equivalent to ``start()`` followed by ``wait()``.
        """
        self.start()
        self.wait()

    @property
    def host(self) -> str:
        return self._protocol.host

    @property
    def port(self) -> int:
        return self._protocol.port

    @property
    def sub_address(self) -> str:
        return self._protocol.sub_address

    @property
    def resource_string(self) -> str:
        """VISA resource string for this server."""
        return self._protocol.resource_string

    def register_mdns(self, instance_name: str) -> None:
        """Advertise this server via mDNS/Zeroconf."""
        self._protocol.register_mdns(instance_name)

    def unregister_mdns(self) -> None:
        """Remove mDNS advertisement."""
        self._protocol.unregister_mdns()
