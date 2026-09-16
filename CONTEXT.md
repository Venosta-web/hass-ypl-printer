# YPL Printer Integration

This context describes the language used to distinguish the printer, its Bluetooth transport, and proof that it can print through Home Assistant.

## Language

**YPL printer**:
A thermal label printer that accepts the YPL wire protocol. In this project, the first hardware target is the plain FlashLabel/KNAON Y50.
_Avoid_: NIIMBOT printer, generic Bluetooth printer

**Transport compatibility**:
Evidence that a printer exposes the expected BLE service and characteristics and accepts a GATT connection. It does not establish that the printer accepts a YPL print stream.
_Avoid_: Protocol compatibility, print compatibility

**Print compatibility**:
Evidence that a printer successfully produces a physical label from a verified YPL print stream. The plain Y50 does not have this status until the hardware test succeeds.
_Avoid_: Transport compatibility

**First-print prototype**:
The smallest Home Assistant custom integration whose acceptance criterion is a physical 50 × 30 mm test label printed by a plain Y50 directly from Home Assistant OS.
_Avoid_: Production integration, HACS release
