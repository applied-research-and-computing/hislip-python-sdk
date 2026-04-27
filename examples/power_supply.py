"""Virtual programmable DC power supply.

Simulates a multi-channel DC power supply with standard SCPI commands.
Each channel has independent voltage, current, and output state.
Output ports emit signals when a channel is enabled, allowing
connected instruments to receive the programmed voltage.
"""

from __future__ import annotations

from dataclasses import dataclass

from hislip_instruments import InstrumentBase, Signal


@dataclass
class _Channel:
    """State for a single PSU channel."""

    voltage: float = 0.0
    current: float = 0.0
    voltage_limit: float = 30.0
    current_limit: float = 5.0
    enabled: bool = False
    ovp: float = 33.0  # Over-voltage protection


class VirtualPowerSupply(InstrumentBase):
    """Virtual multi-channel DC power supply.

    SCPI command tree (channel selected via INSTrument:NSELect):
        INSTrument:NSELect <1|2|3>
        SOURce:VOLTage[:LEVel] <value>
        SOURce:CURRent[:LEVel] <value>
        SOURce:VOLTage:PROTection <value>
        MEASure:VOLTage?
        MEASure:CURRent?
        OUTPut[:STATe] <ON|OFF>
        OUTPut:GENeral <ON|OFF>   (all channels)

    Args:
        num_channels: Number of output channels (default 3).
        **kwargs: Passed to InstrumentBase.
    """

    def __init__(self, num_channels: int = 3, **kwargs):
        kwargs.setdefault("manufacturer", "CARBON")
        kwargs.setdefault("model", "PSU6300")
        kwargs.setdefault("serial", "PSU001")
        kwargs.setdefault("firmware", "1.0.0")
        super().__init__(**kwargs)

        self._num_channels = num_channels
        self._channels = {idx: _Channel() for idx in range(1, num_channels + 1)}
        self._selected = 1

        for idx in range(1, num_channels + 1):
            self.add_output(f"CH{idx}")

    def _reset(self):
        """Reset instrument state to power-on defaults."""
        self._selected = 1
        for channel in self._channels.values():
            channel.voltage = 0.0
            channel.current = 0.0
            channel.current_limit = 5.0
            channel.enabled = False
            channel.ovp = 33.0
        for idx in self._channels:
            self._emit_channel(idx)

    def setup_commands(self):
        engine = self.engine
        engine._reset_hooks.append(self._reset)

        # Channel selection
        engine.register_handler("INST:NSEL", self._handle_channel_select)
        engine.register_handler("INSTRUMENT:NSELECT", self._handle_channel_select)

        # Voltage
        engine.register_handler("SOUR:VOLT:PROT", self._handle_ovp)
        engine.register_handler("SOURCE:VOLTAGE:PROTECTION", self._handle_ovp)
        engine.register_handler("SOUR:VOLT", self._handle_voltage)
        engine.register_handler("SOURCE:VOLTAGE", self._handle_voltage)

        # Current
        engine.register_handler("SOUR:CURR", self._handle_current)
        engine.register_handler("SOURCE:CURRENT", self._handle_current)

        # Output
        engine.register_handler("OUTP:GEN", self._handle_output_general)
        engine.register_handler("OUTPUT:GENERAL", self._handle_output_general)
        engine.register_handler("OUTP", self._handle_output)
        engine.register_handler("OUTPUT", self._handle_output)

        # Applied (combined voltage/current set)
        engine.register_handler("APPL", self._handle_apply)

        # Measure
        engine.register_handler("MEAS:VOLT", self._handle_meas_voltage)
        engine.register_handler("MEASURE:VOLTAGE", self._handle_meas_voltage)
        engine.register_handler("MEAS:CURR", self._handle_meas_current)
        engine.register_handler("MEASURE:CURRENT", self._handle_meas_current)

    @property
    def _channel(self) -> _Channel:
        return self._channels[self._selected]

    def _emit_channel(self, channel_idx: int):
        """Emit signal from a channel's output port."""
        channel = self._channels[channel_idx]
        port_name = f"CH{channel_idx}"
        if channel.enabled:
            signal = Signal(
                value=channel.voltage,
                unit="V",
                metadata={"current_limit": channel.current_limit},
            )
            self.outputs[port_name].emit(signal)
        else:
            self.outputs[port_name].emit(Signal(value=0.0, unit="V"))

    def _handle_channel_select(self, command: str) -> str | None:
        if "?" in command:
            return str(self._selected)
        parts = command.split()
        if len(parts) > 1:
            try:
                idx = int(parts[-1])
                if 1 <= idx <= self._num_channels:
                    self._selected = idx
            except ValueError:
                pass
        return None

    @staticmethod
    def _command_header(command: str) -> str:
        """Extract the colon-delimited command header (before any value)."""
        return command.split()[0].upper() if command.split() else ""

    def _handle_voltage(self, command: str) -> str | None:
        header = self._command_header(command)
        if ":PROT" in header or header.endswith("PROT") or ":PROTECTION" in header:
            return self._handle_ovp(command)
        upper = command.upper()
        if "?" in upper:
            return f"{self._channel.voltage:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                voltage = float(parts[-1])
                voltage = min(voltage, self._channel.ovp)
                voltage = min(voltage, self._channel.voltage_limit)
                self._channel.voltage = voltage
                if self._channel.enabled:
                    self._emit_channel(self._selected)
            except ValueError:
                pass
        return None

    def _handle_current(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._channel.current_limit:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._channel.current_limit = float(parts[-1])
            except ValueError:
                pass
        return None

    def _handle_ovp(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._channel.ovp:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._channel.ovp = float(parts[-1])
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_bool(command: str) -> bool:
        """Parse ON/OFF/1/0 from the last token of a command string."""
        parts = command.strip().upper().split()
        value = parts[-1] if parts else ""
        return value in ("ON", "1")

    def _handle_output(self, command: str) -> str | None:
        header = self._command_header(command)
        if ":GEN" in header or header.endswith("GEN") or ":GENERAL" in header:
            return self._handle_output_general(command)
        upper = command.upper()
        if "?" in upper:
            return "1" if self._channel.enabled else "0"
        self._channel.enabled = self._parse_bool(command)
        self._emit_channel(self._selected)
        return None

    def _handle_output_general(self, command: str) -> str | None:
        if "?" in command.upper():
            all_on = all(channel.enabled for channel in self._channels.values())
            return "1" if all_on else "0"
        enabled = self._parse_bool(command)
        for idx, channel in self._channels.items():
            channel.enabled = enabled
            self._emit_channel(idx)
        return None

    def _handle_apply(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._channel.voltage:.4E},{self._channel.current_limit:.4E}"
        parts = command.split()
        if len(parts) > 1:
            values = parts[-1].split(",")
            try:
                if len(values) >= 1:
                    self._channel.voltage = float(values[0])
                if len(values) >= 2:
                    self._channel.current_limit = float(values[1])
                if self._channel.enabled:
                    self._emit_channel(self._selected)
            except ValueError:
                pass
        return None

    def _handle_meas_voltage(self, _command: str) -> str:
        if self._channel.enabled:
            return f"{self._channel.voltage:.6E}"
        return "0.000000E+00"

    def _handle_meas_current(self, _command: str) -> str:
        if self._channel.enabled:
            return f"{self._channel.current:.6E}"
        return "0.000000E+00"


if __name__ == "__main__":
    from hislip_instruments import HiSLIPProtocol

    psu = VirtualPowerSupply(protocols=[HiSLIPProtocol(port=4880)])
    psu.start()
    print(f"Power supply running: {psu.resource_strings[0]}")
    print("Press Ctrl-C to stop.")

    import signal, threading
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    psu.stop()
