# hislip-python-sdk

Python SDK for building virtual test instruments that speak [HiSLIP](https://www.ivifoundation.org/specifications/default.aspx) (IVI-6.1). Create simulated SCPI instruments accessible from any VISA client — PyVISA, NI MAX, Keysight Connection Expert, or the Carbon platform.

## Installation

```bash
pip install .
```

With mDNS/Zeroconf discovery:

```bash
pip install ".[mdns]"
```

## Quick Start

```python
from hislip_instruments import InstrumentBase, HiSLIPProtocol

class VirtualDMM(InstrumentBase):
    def setup_commands(self):
        self.engine.register_handler("READ?", lambda _: "3.14159")

dmm = VirtualDMM(
    manufacturer="ACME", model="DMM100",
    protocols=[HiSLIPProtocol(port=4880)],
)
dmm.start()
# VISA resource: TCPIP0::127.0.0.1::hislip0::INSTR
```

Then from any VISA client:

```python
import pyvisa
rm = pyvisa.ResourceManager()
dmm = rm.open_resource("TCPIP0::127.0.0.1::hislip0::INSTR")
print(dmm.query("*IDN?"))   # ACME,DMM100,SN001,1.0.0
print(dmm.query("READ?"))   # 3.14159
```

## API Overview

### Core Classes

| Class | Description |
|-------|-------------|
| `InstrumentBase` | Base class — extend this to build instruments |
| `CommandEngine` | SCPI command processor (string in, string out) |
| `SCPICommand` | Parsed command object passed to handlers |
| `HiSLIPProtocol` | HiSLIP server (IVI-6.1, port 4880) |
| `Signal` | Value that flows between instrument ports |
| `InputPort` / `OutputPort` | Inter-instrument wiring |

### Building an Instrument

1. Subclass `InstrumentBase`
2. Override `setup_commands()` to register SCPI handlers
3. Override `on_input_changed()` to react to wired signals
4. Attach a `HiSLIPProtocol` and call `start()`

IEEE 488.2 commands (`*IDN?`, `*RST`, `*OPC?`, etc.) are registered automatically when `ieee488=True` (the default).

### SCPICommand

Handlers receive an `SCPICommand` object instead of a raw string:

```python
def setup_commands(self):
    self.engine.register_handler("CONF:VOLT:DC", self.configure_vdc)

def configure_vdc(self, cmd):
    # cmd.raw    → "CONF:VOLT:DC 10,0.001"
    # cmd.prefix → "CONF:VOLT:DC"
    # cmd.query  → False
    # cmd.args   → ["10", "0.001"]
    rng = cmd.arg(0, float)       # 10.0
    res = cmd.arg(1, float)       # 0.001
    nplc = cmd.arg(2, int, 10)    # 10 (default — not provided)
```

The `cmd.arg(index, type, default)` method provides type coercion with safe defaults — no more manual `split()` / `try`/`except` boilerplate.

### Event Decorators

`HiSLIPProtocol` supports decorators for protocol-level events:

```python
proto = HiSLIPProtocol(port=4880)

@proto.on_trigger
def handle_trigger():
    print("Trigger received!")

@proto.on_clear
def handle_clear():
    print("Device cleared!")
```

| Decorator | HiSLIP Message | When |
|-----------|---------------|------|
| `@proto.on_trigger` | `MSG_TRIGGER` | Client sends a trigger |
| `@proto.on_clear` | `MSG_DEVICE_CLEAR_COMPLETE` | Device clear handshake completes |

### Wiring Instruments Together

Instruments connect through ports. An `OutputPort` on one instrument drives an `InputPort` on another:

```python
from hislip_instruments import Signal

# Signal generator -> oscilloscope
sig_gen.connect("OUTPUT", oscilloscope, "CH1")

# When the signal generator emits, the oscilloscope sees it
sig_gen.outputs["OUTPUT"].emit(Signal(value=2.0, unit="Vpp", metadata={"frequency": 1e3}))
```

## Examples

| File | Description |
|------|-------------|
| `examples/hello_world.py` | Minimal instrument (~15 lines) |
| `examples/multimeter.py` | 6.5-digit DMM with SCPI |
| `examples/oscilloscope.py` | 4-channel digital oscilloscope |
| `examples/power_supply.py` | Multi-channel DC power supply |
| `examples/signal_generator.py` | Function/signal generator |
| `examples/bench.py` | Wired bench: oscilloscope + signal generator |

## Instrument Profiles

The `profiles/` directory contains YAML instrument profile definitions that describe the SCPI command tree, variables, and metadata for each virtual instrument. These are used by the [Carbon platform](https://carbonplatform.ai) to auto-generate instrument control UIs.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache 2.0
