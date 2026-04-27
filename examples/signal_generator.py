"""Virtual signal/function generator.

Simulates a generic signal generator with standard SCPI waveform
commands. Supports sine, square, ramp, pulse, and noise waveforms
with configurable frequency, amplitude, and offset.

The OUTPUT port emits signals that can drive connected instruments
(e.g., a DMM input or oscilloscope channel).
"""

from __future__ import annotations

from hislip_instruments import InstrumentBase, Signal


class VirtualSignalGenerator(InstrumentBase):
    """Virtual signal/function generator.

    SCPI command tree:
        SOURce:FREQuency <value>
        SOURce:VOLTage[:LEVel] <value>
        SOURce:VOLTage:OFFSet <value>
        SOURce:FUNCtion <SIN|SQU|RAMP|PULS|NOIS|DC>
        OUTPut[:STATe] <ON|OFF>
        SOURce:BURSt:STATe <ON|OFF>
        SOURce:BURSt:NCYCles <value>
        SOURce:SWEep:STATe <ON|OFF>
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("manufacturer", "CARBON")
        kwargs.setdefault("model", "SG3300")
        kwargs.setdefault("serial", "SG001")
        kwargs.setdefault("firmware", "1.0.0")
        super().__init__(**kwargs)

        self._frequency = 1000.0
        self._amplitude = 1.0
        self._offset = 0.0
        self._function = "SIN"
        self._output_enabled = False
        self._burst_state = False
        self._burst_ncycles = 1
        self._sweep_state = False

        self.add_output("OUTPUT")
        self.add_input("TRIGGER_IN")
        self.add_output("SYNC")

    def _reset(self):
        """Reset instrument state to power-on defaults."""
        self._frequency = 1000.0
        self._amplitude = 1.0
        self._offset = 0.0
        self._function = "SIN"
        self._output_enabled = False
        self._burst_state = False
        self._burst_ncycles = 1
        self._sweep_state = False

    def setup_commands(self):
        engine = self.engine
        engine._reset_hooks.append(self._reset)

        # Frequency
        engine.register_handler("SOUR:FREQ", self._handle_frequency)
        engine.register_handler("SOURCE:FREQUENCY", self._handle_frequency)
        engine.register_handler("FREQ", self._handle_frequency)

        # Amplitude
        engine.register_handler("SOUR:VOLT:LEV", self._handle_amplitude)
        engine.register_handler("SOURCE:VOLTAGE:LEVEL", self._handle_amplitude)
        engine.register_handler("SOUR:VOLT:OFFS", self._handle_offset)
        engine.register_handler("SOURCE:VOLTAGE:OFFSET", self._handle_offset)
        engine.register_handler("SOUR:VOLT", self._handle_amplitude)
        engine.register_handler("SOURCE:VOLTAGE", self._handle_amplitude)
        engine.register_handler("VOLT", self._handle_amplitude)

        # Function/waveform
        engine.register_handler("SOUR:FUNC", self._handle_function)
        engine.register_handler("SOURCE:FUNCTION", self._handle_function)
        engine.register_handler("FUNC", self._handle_function)

        # Output enable
        engine.register_handler("OUTP:STAT", self._handle_output)
        engine.register_handler("OUTPUT:STATE", self._handle_output)
        engine.register_handler("OUTP", self._handle_output)
        engine.register_handler("OUTPUT", self._handle_output)

        # Burst mode
        engine.register_handler("SOUR:BURS:STAT", self._handle_burst_state)
        engine.register_handler("SOURCE:BURST:STATE", self._handle_burst_state)
        engine.register_handler("SOUR:BURS:NCYC", self._handle_burst_ncycles)
        engine.register_handler("SOURCE:BURST:NCYCLES", self._handle_burst_ncycles)

        # Sweep
        engine.register_handler("SOUR:SWE:STAT", self._handle_sweep_state)
        engine.register_handler("SOURCE:SWEEP:STATE", self._handle_sweep_state)

    def _emit_output(self):
        """Push current signal to the OUTPUT port."""
        if self._output_enabled:
            signal = Signal(
                value=self._amplitude,
                unit="Vpp",
                metadata={
                    "frequency": self._frequency,
                    "offset": self._offset,
                    "function": self._function,
                },
            )
            self.outputs["OUTPUT"].emit(signal)

    def _handle_frequency(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._frequency:.6E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._frequency = float(parts[-1])
                self._emit_output()
            except ValueError:
                pass
        return None

    def _handle_amplitude(self, command: str) -> str | None:
        upper = command.upper()
        if "?" in upper:
            return f"{self._amplitude:.4E}"
        # Don't handle OFFS here — let the longer prefix match win
        if "OFFS" in upper:
            return self._handle_offset(command)
        parts = command.split()
        if len(parts) > 1:
            try:
                self._amplitude = float(parts[-1])
                self._emit_output()
            except ValueError:
                pass
        return None

    def _handle_offset(self, command: str) -> str | None:
        if "?" in command:
            return f"{self._offset:.4E}"
        parts = command.split()
        if len(parts) > 1:
            try:
                self._offset = float(parts[-1])
                self._emit_output()
            except ValueError:
                pass
        return None

    def _handle_function(self, command: str) -> str | None:
        if "?" in command:
            return self._function
        parts = command.split()
        if len(parts) > 1:
            func = parts[-1].upper()
            valid = {"SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"}
            if func in valid:
                self._function = func
                self._emit_output()
        return None

    @staticmethod
    def _parse_bool(command: str) -> bool:
        """Parse ON/OFF/1/0 from the last token of a command string."""
        parts = command.strip().upper().split()
        value = parts[-1] if parts else ""
        return value in ("ON", "1")

    def _handle_output(self, command: str) -> str | None:
        if "?" in command.upper():
            return "1" if self._output_enabled else "0"
        self._output_enabled = self._parse_bool(command)
        if self._output_enabled:
            self._emit_output()
        return None

    def _handle_burst_state(self, command: str) -> str | None:
        if "?" in command.upper():
            return "1" if self._burst_state else "0"
        self._burst_state = self._parse_bool(command)
        return None

    def _handle_burst_ncycles(self, command: str) -> str | None:
        if "?" in command:
            return str(self._burst_ncycles)
        parts = command.split()
        if len(parts) > 1:
            try:
                self._burst_ncycles = int(float(parts[-1]))
            except ValueError:
                pass
        return None

    def _handle_sweep_state(self, command: str) -> str | None:
        if "?" in command.upper():
            return "1" if self._sweep_state else "0"
        self._sweep_state = self._parse_bool(command)
        return None


if __name__ == "__main__":
    from hislip_instruments import HiSLIPProtocol

    sg = VirtualSignalGenerator(protocols=[HiSLIPProtocol(port=4880)])
    sg.start()
    print(f"Signal generator running: {sg.resource_strings[0]}")
    print("Press Ctrl-C to stop.")

    import signal, threading
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    sg.stop()
