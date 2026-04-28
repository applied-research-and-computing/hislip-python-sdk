"""SCPI command processing engine.

The CommandEngine processes string commands and returns string responses.
It knows nothing about protocols or transport — those are handled by
Protocol classes.

IEEE 488.2 and SCPI support is opt-in via the `ieee488` flag. Instruments
with proprietary command sets can skip it entirely and register their
own handlers.
"""

from __future__ import annotations

import threading
from typing import Callable

from .command import SCPICommand


class CommandEngine:
    """SCPI command processor.

    At its core: string in, string out. Commands are dispatched to
    registered handlers by prefix matching. A built-in property store
    provides simple get/set semantics.

    If ieee488=True, standard IEEE 488.2 common commands (*IDN?, *RST,
    *CLS, *STB?, etc.) and basic SCPI commands (SYST:ERR?, MEAS:*?)
    are registered automatically.
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
    ):
        self.manufacturer = manufacturer
        self.model = model
        self.serial = serial
        self.firmware = firmware
        self.command_separator = command_separator

        self._lock = threading.RLock()

        # IEEE 488.2 status model (only meaningful when ieee488=True,
        # but always available so protocols can read STB regardless)
        self._stb: int = 0
        self._esr: int = 0
        self._ese: int = 0
        self._sre: int = 0
        self._opc: bool = False
        self._srq_pending: bool = False

        # SRQ callback — invoked (outside lock) when generate_srq() fires
        self._srq_callback: Callable[[], None] | None = None

        # Key/value property store — handlers can read/write these
        self._properties: dict[str, str] = {}

        # Command handlers: prefix -> callable(SCPICommand) -> response | None
        # Checked in registration order; first match wins.
        self._handlers: list[tuple[str, Callable[[SCPICommand], str | None]]] = []

        # Reset hooks — callables invoked by *RST after engine state is cleared.
        # Instruments register hooks here to reset their own state.
        self._reset_hooks: list[Callable[[], None]] = []

        if ieee488:
            self._register_ieee488()

    def _register_ieee488(self):
        """Register IEEE 488.2 common commands and basic SCPI commands."""
        self.register_handler("*IDN?", self._handle_idn)
        self.register_handler("*RST", self._handle_rst)
        self.register_handler("*CLS", self._handle_cls)
        self.register_handler("*STB?", self._handle_stb_query)
        self.register_handler("*ESR?", self._handle_esr_query)
        self.register_handler("*ESE", self._handle_ese)
        self.register_handler("*SRE", self._handle_sre)
        self.register_handler("*OPC?", lambda _cmd: "1")
        self.register_handler("*OPC", self._handle_opc)
        self.register_handler("*TST?", lambda _cmd: "0")

        # Basic SCPI system commands
        self.register_handler("SYST:ERR?", lambda _cmd: '0,"No error"')
        self.register_handler("SYSTEM:ERROR?", lambda _cmd: '0,"No error"')
        self.register_handler("SYST:VERS?", lambda _cmd: "1999.0")
        self.register_handler("SYSTEM:VERSION?", lambda _cmd: "1999.0")
        self.register_handler("MEAS:", self._handle_measure)
        self.register_handler("MEASURE:", self._handle_measure)

        self._register_ieee488_properties()

    def _register_ieee488_properties(self):
        """Set default SCPI measurement properties."""
        self._properties["VOLT"] = "1.00000E+00"
        self._properties["CURR"] = "5.00000E-03"
        self._properties["FREQ"] = "1.00000E+03"
        self._properties["RES"] = "1.00000E+04"

    # -- Public API ----------------------------------------------------------

    def process_command(self, command: str) -> str | None:
        """Process a command string. May contain multiple commands
        separated by the command_separator (default ";").

        Returns response string or None for non-query commands.
        """
        with self._lock:
            command = command.strip()
            if not command:
                return None

            if self.command_separator:
                commands = [cmd.strip() for cmd in command.split(self.command_separator)]
            else:
                commands = [command]

            responses = []
            for cmd in commands:
                if cmd:
                    resp = self._dispatch(cmd)
                    if resp is not None:
                        responses.append(resp)

            return ";".join(responses) if responses else None

    def register_handler(
        self, prefix: str, handler: Callable[[SCPICommand], str | None]
    ):
        """Register a command handler.

        The handler is called when a command starts with the given prefix
        (case-insensitive). It receives an :class:`SCPICommand` with the
        parsed command and should return a response string or None.

        When multiple handlers match, the longest prefix wins. This means
        a specific handler like "MEAS:VOLT?" always beats a general one
        like "MEAS:".
        """
        self._handlers.append((prefix.upper(), handler))

    def get_property(self, key: str) -> str | None:
        """Get a property value by key (case-insensitive)."""
        return self._properties.get(key.upper())

    def set_property(self, key: str, value: str):
        """Set a property value (case-insensitive key)."""
        self._properties[key.upper()] = value

    def read_stb(self) -> int:
        """Read status byte (used by protocols for serial poll / viReadSTB)."""
        with self._lock:
            stb = self._get_stb()
            self._srq_pending = False
            self._stb &= ~0x40
            return stb

    def generate_srq(self):
        """Generate a Service Request (set bit 6 of STB)."""
        with self._lock:
            self._srq_pending = True
            self._stb |= 0x40
        # Invoke callback outside lock to avoid deadlocks
        cb = self._srq_callback
        if cb is not None:
            cb()

    def on_srq(self, callback: Callable[[], None] | None):
        """Register a callback invoked when SRQ is generated."""
        self._srq_callback = callback

    # -- Internal dispatch ---------------------------------------------------

    @staticmethod
    def _normalize_command(command: str) -> str:
        """Normalize SCPI command syntax.

        Strips leading colon (root-anchored commands) and collapses
        consecutive colons into single colons.
        """
        normalized = command.lstrip(":")
        while "::" in normalized:
            normalized = normalized.replace("::", ":")
        return normalized

    def _dispatch(self, command: str) -> str | None:
        """Dispatch a single command to the longest matching handler.

        When multiple handlers match, the one with the longest prefix
        wins. This means a specific handler like "MEAS:VOLT?" always
        beats a general one like "MEAS:".
        """
        command = self._normalize_command(command)
        upper = command.upper()

        best_handler = None
        best_prefix = ""
        best_length = -1
        for prefix, handler in self._handlers:
            if upper.startswith(prefix) and len(prefix) > best_length:
                best_handler = handler
                best_prefix = prefix
                best_length = len(prefix)

        if best_handler is not None:
            cmd = SCPICommand(command, best_prefix)
            return best_handler(cmd)

        # Fallback: generic property get/set
        return self._handle_property(command)

    # -- IEEE 488.2 handlers -------------------------------------------------

    def _handle_idn(self, _cmd: SCPICommand) -> str:
        return f"{self.manufacturer},{self.model},{self.serial},{self.firmware}"

    def _handle_rst(self, _cmd: SCPICommand) -> None:
        # Per IEEE 488.2 section 10.32, *RST resets device-specific state
        # but must NOT clear the ESE or SRE enable registers.
        self._stb = 0
        self._esr = 0
        self._opc = False
        self._srq_pending = False
        self._properties.clear()
        self._register_ieee488_properties()
        for hook in self._reset_hooks:
            hook()
        return None

    def _handle_cls(self, _cmd: SCPICommand) -> None:
        self._stb = 0
        self._esr = 0
        self._srq_pending = False
        return None

    def _handle_stb_query(self, _cmd: SCPICommand) -> str:
        return str(self._get_stb())

    def _handle_esr_query(self, _cmd: SCPICommand) -> str:
        val = self._esr
        self._esr = 0
        return str(val)

    def _handle_ese(self, cmd: SCPICommand) -> str | None:
        if cmd.query:
            return str(self._ese)
        val = cmd.arg(0, int)
        if val is not None:
            self._ese = val & 0xFF
        return None

    def _handle_sre(self, cmd: SCPICommand) -> str | None:
        if cmd.query:
            return str(self._sre)
        val = cmd.arg(0, int)
        if val is not None:
            self._sre = val & 0xFF
        return None

    def _handle_opc(self, _cmd: SCPICommand) -> None:
        self._opc = True
        return None

    def _handle_measure(self, cmd: SCPICommand) -> str | None:
        upper = cmd.raw.upper().replace("MEASURE:", "MEAS:")
        parts = upper.split(":")
        if len(parts) >= 2:
            meas_type = parts[1].split("?")[0].split(" ")[0]
            key = meas_type.rstrip("?")
            if key in self._properties:
                return self._properties[key]
        return "9.90000E+37"  # NaN equivalent

    # -- Generic property handler --------------------------------------------

    def _handle_property(self, command: str) -> str | None:
        """Fallback handler: treat as property get (with ?) or set."""
        upper = command.upper().strip()

        if "?" in upper:
            key = upper.replace("?", "").strip()
            for candidate in [key, key.replace(":", "")]:
                if candidate in self._properties:
                    return self._properties[candidate]
            return None

        parts = command.split(None, 1)
        if len(parts) == 2:
            key = parts[0].upper().strip()
            value = parts[1].strip()
            self._properties[key] = value
        return None

    # -- Helpers -------------------------------------------------------------

    def _get_stb(self) -> int:
        stb = self._stb
        if self._srq_pending:
            stb |= 0x40
        return stb

