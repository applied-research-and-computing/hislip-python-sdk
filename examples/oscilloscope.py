"""Virtual digital oscilloscope.

Simulates a generic 4-channel oscilloscope with standard SCPI commands.
Input ports receive signals from connected instruments (signal generators,
power supplies) and update channel measurements accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hislip_instruments import InstrumentBase, Signal


@dataclass
class _ChannelState:
    """State for a single oscilloscope input channel."""

    enabled: bool = True
    scale: float = 1.0  # V/div
    offset: float = 0.0
    coupling: str = "DC"
    bandwidth_limit: bool = False
    signal: Signal = field(default_factory=Signal)


class VirtualOscilloscope(InstrumentBase):
    """Virtual 4-channel digital oscilloscope.

    SCPI command tree:
        CHANnel<n>:DISPlay <ON|OFF>
        CHANnel<n>:SCALe <value>
        CHANnel<n>:OFFSet <value>
        CHANnel<n>:COUPling <DC|AC|GND>
        CHANnel<n>:BWLimit <ON|OFF>
        TIMebase:SCALe <value>
        TIMebase:POSition <value>
        TRIGger:SOURce <CHn|EXT>
        TRIGger:LEVel <value>
        TRIGger:SLOPe <POS|NEG>
        MEASure:SOURce <CHn>
        MEASure:FREQuency?
        MEASure:VPP?
        MEASure:VMAX?
        MEASure:VMIN?
        MEASure:VRMs?
        ACQuire:TYPE <NORMal|AVERage|PEAK>
        ACQuire:COUNt <value>
        RUN / STOP / SINGle
        WAVeform:SOURce <CHn>
        WAVeform:DATA?

    Args:
        num_channels: Number of input channels (default 4).
        **kwargs: Passed to InstrumentBase.
    """

    def __init__(self, num_channels: int = 4, **kwargs):
        kwargs.setdefault("manufacturer", "CARBON")
        kwargs.setdefault("model", "DSO4000")
        kwargs.setdefault("serial", "DSO001")
        kwargs.setdefault("firmware", "1.0.0")
        super().__init__(**kwargs)

        self._num_channels = num_channels
        self._channels = {idx: _ChannelState() for idx in range(1, num_channels + 1)}

        self._timebase_scale = 1e-3  # 1 ms/div
        self._timebase_position = 0.0
        self._trigger_source = "CH1"
        self._trigger_level = 0.0
        self._trigger_slope = "POS"
        self._acquire_type = "NORMAL"
        self._acquire_count = 1
        self._running = True
        self._meas_source = 1
        self._waveform_source = 1

        for idx in range(1, num_channels + 1):
            self.add_input(f"CH{idx}")
        self.add_input("EXT_TRIG")
        self.add_output("TRIGGER_OUT")

    def _reset(self):
        """Reset instrument state to power-on defaults."""
        for channel in self._channels.values():
            channel.enabled = True
            channel.scale = 1.0
            channel.offset = 0.0
            channel.coupling = "DC"
            channel.bandwidth_limit = False
            channel.signal = Signal()
        self._timebase_scale = 1e-3
        self._timebase_position = 0.0
        self._trigger_source = "CH1"
        self._trigger_level = 0.0
        self._trigger_slope = "POS"
        self._acquire_type = "NORMAL"
        self._acquire_count = 1
        self._running = True
        self._meas_source = 1
        self._waveform_source = 1

    def setup_commands(self):
        engine = self.engine
        engine._reset_hooks.append(self._reset)

        # Channel commands (CHAN1:SCAL, CHAN2:DISP, etc.)
        engine.register_handler("CHAN", self._handle_channel)
        engine.register_handler("CHANNEL", self._handle_channel)

        # Timebase
        engine.register_handler("TIM:SCAL", self._handle_timebase_scale)
        engine.register_handler("TIMEBASE:SCALE", self._handle_timebase_scale)
        engine.register_handler("TIM:POS", self._handle_timebase_position)
        engine.register_handler("TIMEBASE:POSITION", self._handle_timebase_position)

        # Trigger
        engine.register_handler("TRIG:SOUR", self._handle_trigger_source)
        engine.register_handler("TRIGGER:SOURCE", self._handle_trigger_source)
        engine.register_handler("TRIG:LEV", self._handle_trigger_level)
        engine.register_handler("TRIGGER:LEVEL", self._handle_trigger_level)
        engine.register_handler("TRIG:SLOP", self._handle_trigger_slope)
        engine.register_handler("TRIGGER:SLOPE", self._handle_trigger_slope)

        # Measure
        engine.register_handler("MEAS:SOUR", self._handle_meas_source)
        engine.register_handler("MEASURE:SOURCE", self._handle_meas_source)
        engine.register_handler("MEAS:FREQ", self._handle_meas_freq)
        engine.register_handler("MEASURE:FREQUENCY", self._handle_meas_freq)
        engine.register_handler("MEAS:VPP", self._handle_meas_vpp)
        engine.register_handler("MEASURE:VPP", self._handle_meas_vpp)
        engine.register_handler("MEAS:VMAX", self._handle_meas_vmax)
        engine.register_handler("MEASURE:VMAX", self._handle_meas_vmax)
        engine.register_handler("MEAS:VMIN", self._handle_meas_vmin)
        engine.register_handler("MEASURE:VMIN", self._handle_meas_vmin)
        engine.register_handler("MEAS:VRMS", self._handle_meas_vrms)
        engine.register_handler("MEASURE:VRMS", self._handle_meas_vrms)

        # Acquire
        engine.register_handler("ACQ:TYPE", self._handle_acquire_type)
        engine.register_handler("ACQUIRE:TYPE", self._handle_acquire_type)
        engine.register_handler("ACQ:COUN", self._handle_acquire_count)
        engine.register_handler("ACQUIRE:COUNT", self._handle_acquire_count)

        # Run control
        engine.register_handler("RUN", lambda _: self._set_running(True))
        engine.register_handler("STOP", lambda _: self._set_running(False))
        engine.register_handler("SING", lambda _: self._set_running(False))
        engine.register_handler("SINGLE", lambda _: self._set_running(False))

        # Waveform
        engine.register_handler("WAV:SOUR", self._handle_waveform_source)
        engine.register_handler("WAVEFORM:SOURCE", self._handle_waveform_source)
        engine.register_handler("WAV:DATA", self._handle_waveform_data)
        engine.register_handler("WAVEFORM:DATA", self._handle_waveform_data)

    def on_input_changed(self, port_name: str, signal: Signal):
        if port_name.startswith("CH"):
            try:
                idx = int(port_name[2:])
                if idx in self._channels:
                    self._channels[idx].signal = signal
            except ValueError:
                pass

    def _set_running(self, state: bool) -> None:
        self._running = state

    def _parse_channel_num(self, command: str) -> int | None:
        """Extract channel number from commands like CHAN1:SCAL or CHANNEL12:DISPLAY."""
        upper = command.upper()
        for prefix in ("CHANNEL", "CHAN"):
            if upper.startswith(prefix):
                rest = upper[len(prefix):]
                # Collect all leading digits
                digits = ""
                for char in rest:
                    if char.isdigit():
                        digits += char
                    else:
                        break
                if digits:
                    return int(digits)
        return None

    def _parse_channel_subcmd(self, command: str) -> tuple[int | None, str, str]:
        """Parse CHANn:SUBCMD [value] -> (channel_num, subcmd, value_str)."""
        upper = command.upper()
        channel_num = self._parse_channel_num(command)
        # Find the colon after the channel prefix+number
        # Skip past "CHAN" or "CHANNEL" prefix and digits to find the right colon
        for prefix in ("CHANNEL", "CHAN"):
            if upper.startswith(prefix):
                pos = len(prefix)
                while pos < len(upper) and upper[pos].isdigit():
                    pos += 1
                if pos < len(upper) and upper[pos] == ":":
                    rest = upper[pos + 1:]
                else:
                    return channel_num, "", ""
                break
        else:
            return channel_num, "", ""
        parts = rest.split(None, 1)
        subcmd = parts[0] if parts else ""
        value_str = parts[1] if len(parts) > 1 else ""
        return channel_num, subcmd, value_str

    def _handle_channel(self, command: str) -> str | None:
        channel_num, subcmd, value_str = self._parse_channel_subcmd(command)
        if channel_num is None or channel_num not in self._channels:
            return None
        channel = self._channels[channel_num]

        if subcmd.startswith("DISP") or subcmd.startswith("DIS"):
            if "?" in subcmd or "?" in value_str:
                return "1" if channel.enabled else "0"
            channel.enabled = value_str in ("ON", "1")
            return None

        if subcmd.startswith("SCAL"):
            if "?" in subcmd:
                return f"{channel.scale:.4E}"
            try:
                channel.scale = float(value_str) if value_str else channel.scale
            except ValueError:
                pass
            return None

        if subcmd.startswith("OFFS"):
            if "?" in subcmd:
                return f"{channel.offset:.4E}"
            try:
                channel.offset = float(value_str) if value_str else channel.offset
            except ValueError:
                pass
            return None

        if subcmd.startswith("COUP"):
            if "?" in subcmd:
                return channel.coupling
            if value_str in ("DC", "AC", "GND"):
                channel.coupling = value_str
            return None

        if subcmd.startswith("BWL") or subcmd.startswith("BWLIM"):
            if "?" in subcmd:
                return "1" if channel.bandwidth_limit else "0"
            channel.bandwidth_limit = value_str in ("ON", "1")
            return None

        return None

    def _handle_timebase_scale(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._timebase_scale:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._timebase_scale = float(parts[-1])
            except ValueError:
                pass
        return None

    def _handle_timebase_position(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._timebase_position:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._timebase_position = float(parts[-1])
            except ValueError:
                pass
        return None

    def _handle_trigger_source(self, command: str) -> str | None:
        if "?" in command:
            return self._trigger_source
        parts = command.split()
        if len(parts) > 1:
            self._trigger_source = parts[-1].upper()
        return None

    def _handle_trigger_level(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._trigger_level:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._trigger_level = float(parts[-1])
            except ValueError:
                pass
        return None

    def _handle_trigger_slope(self, command: str) -> str | None:
        if "?" in command:
            return self._trigger_slope
        parts = command.split()
        if len(parts) > 1:
            slope = parts[-1].upper()
            if slope in ("POS", "NEG", "EITH"):
                self._trigger_slope = slope
        return None

    def _handle_meas_source(self, command: str) -> str | None:
        if "?" in command:
            return f"CH{self._meas_source}"
        parts = command.split()
        if len(parts) > 1:
            src = parts[-1].upper()
            if src.startswith("CH") and src[2:].isdigit():
                idx = int(src[2:])
                if idx in self._channels:
                    self._meas_source = idx
        return None

    def _meas_signal(self) -> Signal:
        channel = self._channels[self._meas_source]
        if channel.coupling == "GND":
            return Signal(value=0.0, unit="V")
        return channel.signal

    def _is_dc_signal(self, signal: Signal) -> bool:
        """Determine if a signal is DC (not AC/Vpp)."""
        return signal.unit == "V" and "frequency" not in signal.metadata

    def _handle_meas_freq(self, _command: str) -> str:
        freq = self._meas_signal().metadata.get("frequency", 0.0)
        return f"{freq:.6E}"

    def _handle_meas_vpp(self, _command: str) -> str:
        signal = self._meas_signal()
        if self._is_dc_signal(signal):
            return f"{0.0:.6E}"  # DC has no peak-to-peak
        return f"{signal.value:.6E}"

    def _handle_meas_vmax(self, _command: str) -> str:
        signal = self._meas_signal()
        offset = signal.metadata.get("offset", 0.0)
        if self._is_dc_signal(signal):
            return f"{signal.value:.6E}"
        return f"{signal.value / 2.0 + offset:.6E}"

    def _handle_meas_vmin(self, _command: str) -> str:
        signal = self._meas_signal()
        offset = signal.metadata.get("offset", 0.0)
        if self._is_dc_signal(signal):
            return f"{signal.value:.6E}"
        return f"{-signal.value / 2.0 + offset:.6E}"

    def _handle_meas_vrms(self, _command: str) -> str:
        signal = self._meas_signal()
        if self._is_dc_signal(signal):
            return f"{signal.value:.6E}"  # RMS of DC = DC value
        # Vrms = Vpp / (2 * sqrt(2)) for a sine wave
        vrms = signal.value / (2.0 * 1.4142135623730951)
        return f"{vrms:.6E}"

    def _handle_acquire_type(self, command: str) -> str | None:
        if "?" in command:
            return self._acquire_type
        parts = command.split()
        if len(parts) > 1:
            acq_type = parts[-1].upper()
            valid = {"NORMAL", "NORM", "AVERAGE", "AVER", "PEAK", "HRESOLUTION", "HRES"}
            if acq_type in valid:
                self._acquire_type = acq_type
        return None

    def _handle_acquire_count(self, command: str) -> str | None:
        if "?" in command:
            return str(self._acquire_count)
        parts = command.split()
        if len(parts) > 1:
            try:
                self._acquire_count = int(parts[-1])
            except ValueError:
                pass
        return None

    def _handle_waveform_source(self, command: str) -> str | None:
        if "?" in command:
            return f"CH{self._waveform_source}"
        parts = command.split()
        if len(parts) > 1:
            src = parts[-1].upper()
            if src.startswith("CH") and src[2:].isdigit():
                idx = int(src[2:])
                if idx in self._channels:
                    self._waveform_source = idx
        return None

    def _handle_waveform_data(self, _command: str) -> str:
        """Return synthetic waveform data as comma-separated values.

        Point count scales with timebase: faster timebases produce
        more points (higher sample rate), slower timebases fewer.
        The base is 1000 points at 1 ms/div.

        When a signal with frequency metadata is connected, the waveform
        reflects the actual frequency, offset, and function shape so the
        display window shows the correct number of cycles.
        """
        import math

        signal = self._channels[self._waveform_source].signal
        amplitude = signal.value if signal.value != 0 else 1.0
        frequency = signal.metadata.get("frequency", 0.0)
        offset = signal.metadata.get("offset", 0.0)
        func = signal.metadata.get("function", "SIN")

        # Scale points with timebase: 1000 at 1ms, clamped to [10, 10000]
        num_points = int(1e-3 / self._timebase_scale * 1000)
        num_points = max(10, min(num_points, 10000))

        # Display window covers 10 divisions
        window_seconds = 10.0 * self._timebase_scale

        points: list[float] = []
        for i in range(num_points):
            t = i / num_points * window_seconds
            if frequency > 0:
                phase = 2.0 * math.pi * frequency * t
            else:
                phase = 2.0 * math.pi * i / num_points

            if func == "SQU":
                y = amplitude / 2.0 if (math.sin(phase) >= 0) else -amplitude / 2.0
            elif func == "RAMP":
                frac = (phase / (2.0 * math.pi)) % 1.0
                y = amplitude * (frac - 0.5)
            elif func == "DC":
                y = 0.0
            else:
                # SIN (default), PULS, NOIS treated as sine
                y = amplitude / 2.0 * math.sin(phase)

            points.append(y + offset)

        return ",".join(f"{point:.4E}" for point in points)


if __name__ == "__main__":
    from hislip_instruments import HiSLIPProtocol

    osc = VirtualOscilloscope(protocols=[HiSLIPProtocol(port=4880)])
    osc.start()
    print(f"Oscilloscope running: {osc.resource_strings[0]}")
    print("Press Ctrl-C to stop.")

    import signal, threading
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    osc.stop()
