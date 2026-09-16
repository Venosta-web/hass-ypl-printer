# YPL-v1 protocol (supported subset)

This document describes only the part of the YPL wire protocol that this integration implements: one 50 × 30 mm label on a Y50-family printer over the Y50P Bluetooth LE profile. The implementation is [`custom_components/ypl_printer/protocol.py`](../custom_components/ypl_printer/protocol.py); [`tests/test_protocol.py`](../tests/test_protocol.py) pins every fact below that concerns bytes.

This project is not affiliated with Yoctopuce. "YPL" is the name used by the prior-art projects cited here; it does not come from the printer vendor ([yplib FINDINGS](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L97-L117)).

Every fact is cited against a pinned upstream commit:

| Source | Commit | Role |
| --- | --- | --- |
| [slastra/yplib](https://github.com/slastra/yplib/tree/4748c21393b30f78defed256a7394d0960ad9a07) | `4748c21393b30f78defed256a7394d0960ad9a07` | Primary: Y50P captures, framing, raster, BLE profile |
| [Souukou/OpenBluetoothPrinter](https://github.com/Souukou/OpenBluetoothPrinter/tree/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad) | `c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad` | Corroboration only (FlashToy U8) |

Below, `yplib/…` means a file in yplib at that commit and `OBP/…` a file in OpenBluetoothPrinter at that commit.

## Frame format and CRC

```text
frame   = 1a 01 <payload_length:u16-le> <payload> <crc32:u32-le> a1
payload = <group:u8> <command:u8> <direction:u8> <data_length:u16-le> <data>
```

- `1a` is the start byte, `01` the protocol version, `a1` the terminator. Only version `01` is supported ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L97-L111), [yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L43-L59)).
- The CRC covers the payload only. It is a reflected CRC-32 with polynomial `0xEDB88320`, initial register `0xCA896ADE` and final xor `0xFFFFFFFF`, stored little-endian. In Python: `zlib.crc32(payload, 0xCA896ADE ^ 0xFFFFFFFF)` ([yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L1-L32), [yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L119-L139)).
- Directions: `01` request, `02` reply, `03` unsolicited event, `04` set ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L152-L165), corroborated by [OBP/lib/flashtoy/protocol.ts](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/protocol.ts#L5-L32)).
- A reply's `data` is itself an envelope: `<discriminator:u8> <value_length:u16-le> <value>`. Discriminator `01` carries bytes and `03` a little-endian integer ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L167-L173), [yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L181-L212)). Other discriminators are kept as raw bytes. Events do not use this envelope; their data is kept raw ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L236-L238)).

Vectors (all from [yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L236-L266)):

| Input | Expected |
| --- | --- |
| CRC of `050b010000` | `0x5c73542d` |
| CRC of `0104010000` | `0xf190e2bb` |
| CRC of `053904010013` | `0xfec2f091` |
| Status-request frame | `1a010500050b0100002d54735ca1` |

### Strict decoding

The module accepts a frame only if all of these hold. Anything else raises `FrameError`; nothing is repaired.

- it starts with `1a 01` and its payload length is at least 5;
- its total length is exactly `payload_length + 9`;
- it ends with `a1`;
- the CRC matches;
- the direction is `01`–`04`;
- `data_length` equals the number of data bytes;
- for a reply, `value_length` equals the number of value bytes, and an integer value has at least one byte.

The notification decoder (`FrameDecoder`) keeps a buffer across notifications, because BLE can split a frame across notifications or put several frames in one. It looks for `1a 01`, waits until the declared frame length has arrived, and then applies the strict checks above. This follows the receivers in [yplib/src/web-bluetooth.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L74-L97) and [OBP/lib/flashtoy/protocol.ts](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/protocol.ts#L245-L300). Unlike OBP, the decoder sets no maximum frame length. It returns every byte it discards as a `MalformedCandidate` with a reason, for diagnostics:

- bytes before a frame start are discarded;
- a rejected candidate is discarded from its start up to the next `1a 01`.

A candidate that declares a long payload holds the buffer until that many bytes arrive. Callers must bound each wait with their own timeout.

A status reading counts only if it comes from a strictly valid `05/0b` direction-`02` reply with an integer envelope (`decode_status`).

## Commands

Names follow [yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L179-L202), which takes them from OpenBluetoothPrinter ([OBP/lib/flashtoy/protocol.ts](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/protocol.ts#L17-L30)).

| Group/command | Name |
| --- | --- |
| `01/02` | serial |
| `01/04` | model |
| `01/07` | firmware |
| `01/b7` | hardware info |
| `05/0b` | status |
| `05/0f` | event (payload not interpreted) |
| `05/11` | density |
| `05/19` | start print |
| `05/1a` | end print |
| `05/20` | paper type |
| `05/21` | paper locate |
| `05/36` | first-task withdrawal |
| `05/37` | end-task formfeed |
| `05/38` | print width |
| `05/39` | compression rate |
| `05/40` | x reference |
| `05/41` | canvas width |

## Stream order

One label is sent as the sequence below. Every payload is sent as its own frame, except the raster, which is sent raw with no framing. The order and every repeated frame come from the captured vendor session. Nobody has established which frames could be dropped, so the sequence is not shortened ([yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L66-L102), [yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L145-L170)).

```text
Preamble:
0104010000  (twice)
01b7010000
0107010000
0102010000
050b010000  (six times)
052001010000
051101010008
0519010000
0536010000
050b010000
05410402003200     canvas width 50 mm
05380402003200     print width 50 mm
05400402000000     x reference 0
053904010008       compression rate 0x08

Raw raster (unframed)

Trailer:
0521010000         paper locate
0537010000         end-task formfeed
051a010000         end print
050b010000  (ten times)
```

- `build_print_stream` returns 34 segments: 20 preamble frames (including the compression-rate frame), the raster, and 13 trailer frames. It validates the raster before building anything.
- The compression rate is always `0x08`, the default in yplib's builder. The captures also contain `0x13` and `0x09`. The exact Y50P formula is not known, so this integration never calculates the rate ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L240-L249)).
- All three trailer commands are needed. `05/21` alone ends the raster but leaves the label half out of the printer; `05/37` and `05/1a` feed it to the tear bar ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L262-L266)).

### Capture conformance

The three yplib captures are vendored unmodified in [`tests/fixtures/captures/`](../tests/fixtures/captures/README.md), and their hashes are checked. All three decode to 240 × 400 rasters, and all of their frames are valid ([yplib/test/captures.test.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/test/captures.test.ts#L57-L93)).

| Capture | Test |
| --- | --- |
| `y50p-flashlabel-label.bin` | Rebuilt byte for byte from the decoded raster. The capture used compression rate `0x13`, so the test swaps that single frame into the builder's output. |
| `y50p-horizontal-line.bin` | Rebuilt byte for byte (rate `0x08`). The recording was stopped after the first trailer frame, so it is compared with the builder's output up to and including `05/21`. |
| `y50p-vertical-line.bin` | Decoded only. The recording starts mid-session, so it cannot be rebuilt. |

## Raster encoding and invariants

The raster is exactly 240 rows of exactly 400 pixels each (50 × 30 mm at 8 dots/mm). Each pixel is the literal integer `0` (white) or `1` (black). Booleans, floats and any other values are rejected ([yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L151-L159)).

```text
row = 18 <run>+
run = (colour << 7) | (run_length - 1)      run_length 1..128
```

([yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L104-L143))

- Neither the row length nor the raster length is sent. The printer takes rows until the raster ends ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L253-L255)).
- A decoder can find the end of a row only by expanding runs until it has exactly 400 pixels. The next byte must then be the `18` marker, and there must be exactly 240 rows. `18` is also a valid run byte (25 white pixels), so the decoder never searches for it as a delimiter.
- A row of the wrong width shifts every later row marker and can hang the firmware, so the width checks always run before anything is sent ([yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L151-L159)).

The decoder raises `RasterError` for any of these:

- a missing row marker;
- a run that goes past 400 pixels;
- a row that is cut short;
- fewer than 240 rows;
- extra bytes after row 240.

Vectors ([yplib/src/protocol.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L246-L253)):

| Row | Encoding |
| --- | --- |
| All white | `187f7f7f0f` |
| Black `[198, 202)` | `187f45837f45` |

## BLE profile and chunking

The protocol module does no BLE work. The transport layer uses this profile:

| Role | UUID | Operation |
| --- | --- | --- |
| Service | `000018f0-0000-1000-8000-00805f9b34fb` | primary service |
| Host → printer | `00002af1-0000-1000-8000-00805f9b34fb` | write without response |
| Printer → host | `00002af0-0000-1000-8000-00805f9b34fb` | notifications |

([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L79-L95), [yplib/src/web-bluetooth.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L4-L19))

- The transport subscribes to notifications before its first write.
- It joins the segments and splits them into 20-byte chunks, ignoring frame boundaries. The measured ATT MTU was 23, which leaves 20 bytes per write ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L90-L91)).
- Writes are paced: the yplib hardware probe waits 10 ms between them ([yplib/reference/probes/ble-print.py](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/reference/probes/ble-print.py#L46-L49)), and the Web Bluetooth transport 8 ms ([yplib/src/web-bluetooth.ts](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L102-L116)). Write-without-response gives no backpressure, so unpaced writes can corrupt the job.
- This integration uses 10 ms. A GATT write that returns does not prove that anything printed.

## Status bits

`05/0b` replies carry an integer. Raw zero means ready. `PrinterStatus.flags` lists the known bits in ascending order, and `unknown_bits` is `raw & ~0x1f`. Every raw value is preserved.

| Bit | Flag | Evidence |
| --- | --- | --- |
| `0x00` (raw zero) | `ready` | yplib ([status formatter](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L215-L233)) |
| `0x01` | `printing` | U8 corroboration only ([OBP/lib/flashtoy/protocol.ts](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/protocol.ts#L220-L232)) |
| `0x02` | `cover_open` | measured on Y50P ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L204-L224)) |
| `0x04` | `paper_out` | measured on Y50P (same) |
| `0x08` | `undervoltage` | U8 corroboration only |
| `0x10` | `overheat` | U8 corroboration only |

On a Y50P, the measured value `0x06` means the cover is open. The paper sensor also reads empty whenever the cover is open, so `0x02` was never seen on its own. `0x06` therefore has the flags `cover_open` and `paper_out`, and should be read as "cover open", with the paper state unknown ([yplib/FINDINGS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L226-L234)). Bits without a known meaning stay numeric.

## Explicitly unsupported

The following are outside this implementation:

- other media sizes;
- sibling printer families and other BLE profiles, including the U8's `FF00`–`FF03` transport and its credit-based flow control;
- YPL v2 and the `05/1b` zlib print path;
- the U8 raw-row marker `0x16`;
- a computed compression rate;
- any interpretation of `05/0f` event payloads;
- multi-label jobs, cancel commands, pairing assumptions and extension hooks;
- treating successful GATT writes as proof of printing;
- automatic replay after the transmission boundary.

The vendor SDK binary is not redistributed ([yplib/ACKNOWLEDGEMENTS.md](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/ACKNOWLEDGEMENTS.md#L44-L48)).
