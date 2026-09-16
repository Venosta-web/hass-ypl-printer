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

**Label text**:
The caller-supplied Basic Latin content from which the first-print prototype creates one fixed-media label image. Line breaks are part of the content.
_Avoid_: NIIMBOT payload, drawing-element list, print stream

**Deterministic text renderer**:
The boundary that converts valid label text into the same fixed-size binary raster on the supported runtime. It does not encode YPL or communicate with a printer.
_Avoid_: YPL encoder, Bluetooth transport

**Diagnostic response**:
An optional machine-readable account of facts observed while processing a print job. It is not proof that a physical label was produced.
_Avoid_: Print confirmation, success receipt

**Uncertain physical outcome**:
A print job that fails after transmission has begun, when the integration cannot safely determine whether the printer produced some or all of the label. Such a job must not be replayed automatically.
_Avoid_: Failed print, safe retry

**Transmission boundary**:
The moment the first write of a print stream is started, whether or not that write succeeds. Any failure, timeout, or cancellation after this point is an uncertain physical outcome; before it, the printer is known not to have received print data. Preflight status polls do not cross it.
_Avoid_: First completed write, retry point

**Preflight readiness check**:
Bounded status polling before the transmission boundary. A readable non-ready status stops the job cleanly; silence is unknown and does not block printing.
_Avoid_: Ready check, handshake

**Post-send observation**:
Bounded status polling after the last stream write and before disconnect. It can reveal a printer error or unfinished printing, but even a ready status is not proof that a physical label was produced.
_Avoid_: Print confirmation, completion wait

**Acceptance specimen**:
The one fixed label text used for the golden renderer and byte tests and printed during the hardware procedure. It covers uppercase, lowercase, digits, punctuation, spaces, and a blank line.
_Avoid_: Test string, sample label

**Hardware test report**:
The committed record of one run of the hardware procedure on a named installation and Bluetooth path, including each step's result and a photograph of any output. Print compatibility is claimed only through such a report.
_Avoid_: Test log, success screenshot

**Determinism deviation**:
A hardware-test observation that the raster or job produced on the installed runtime differs from the pinned golden values. It is recorded and reviewed against the renderer contract, but on its own it neither proves nor disproves print compatibility.
_Avoid_: Render bug, failed print
