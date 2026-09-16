# YPL wire and BLE behavior for a Y50P Python port

Research for [Research the authoritative YPL wire and BLE behavior](https://github.com/Venosta-web/hass-ypl-printer/issues/2). This note distinguishes Y50P evidence from behavior observed only on the sibling FlashToy U8. It does not assign meaning to unknown bytes.

## Sources and authority

The implementation should pin its byte compatibility to these revisions:

| Source | Pinned revision | Role |
| --- | --- | --- |
| [slastra/yplib](https://github.com/slastra/yplib/tree/4748c21393b30f78defed256a7394d0960ad9a07) | `4748c21393b30f78defed256a7394d0960ad9a07` | Primary source for Y50P framing, captures, raster, BLE profile, and tested orchestration |
| [slastra/printrow](https://github.com/slastra/printrow/tree/3e5923b1653e8205412bcd4d768515ea75dff4b3) | `3e5923b1653e8205412bcd4d768515ea75dff4b3` | Primary consumer showing how the released library is used end to end |
| [Souukou/OpenBluetoothPrinter](https://github.com/Souukou/OpenBluetoothPrinter/tree/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad) | `c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad` | Corroboration for the shared YPL command vocabulary, framing, CRC, status bits, and U8-specific differences |

`yplib` says its Y50P results came from manufacturer-app HCI captures and were bench-verified over USB, classic SPP, and BLE; its capture-conformance tests reconstruct complete captured sessions byte for byte ([derivation](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L3-L30), [capture tests](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/test/captures.test.ts#L7-L15)). Printrow resolves `@slastra/yplib` 0.1.0 in its lockfile and delegates framing, raster conversion, transport, and print orchestration to it rather than reimplementing them ([lock](https://github.com/slastra/printrow/blob/3e5923b1653e8205412bcd4d768515ea75dff4b3/bun.lock#L205-L207), [driver](https://github.com/slastra/printrow/blob/3e5923b1653e8205412bcd4d768515ea75dff4b3/src/lib/printer/drivers.ts#L99-L127)).

## Decision

Port the pinned `yplib` YPL-v1 path literally for the first prototype. Preserve the captured Y50P preamble, raw row-RLE raster, full trailer, reply parser, and conservative BLE defaults. Enforce exactly 50 mm / 400 dots across every row before opening a print transaction. Subscribe to notifications before any request. Poll `05 0b` until ready before sending. Do not import the U8's `FF00`/`FF01`/`FF02`/`FF03` profile, credit-window protocol, raw-row marker, or v2 zlib route.

The Python port should include the upstream vectors and capture fixtures as tests. It should treat notification silence as unknown/timeout, not ready. Because upstream does not prove final physical completion before its last-label return, connection teardown after sending remains a hardware-test question rather than a protocol fact.

## Frame and payload

A Y50P v1 control frame is:

```text
1a 01 <payload_length:u16-le> <payload> <crc32:u32-le> a1
```

The CRC covers only `payload`. It is reflected CRC-32 with polynomial `0xEDB88320`, initial register `0xCA896ADE`, and final xor `0xFFFFFFFF`; the result is serialized little-endian. The pinned implementation is the normative executable description ([source](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L1-L59)); the derivation reports validation against 18 distinct captured payload/checksum pairs ([findings](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L119-L139)). With Python's `zlib`, the equivalent observed by the upstream reference driver is:

```python
checksum = zlib.crc32(payload, 0xCA896ADE ^ 0xFFFFFFFF)
```

The control payload is:

```text
<group:u8> <command:u8> <direction:u8> <data_length:u16-le> <data>
```

Directions observed by the two implementations are `01` request, `02` reply, `03` unsolicited event, and `04` set ([yplib](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L150-L177), [corroboration](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/protocol.ts#L5-L32)). A reply's `data` has a one-byte discriminator followed by `value_length:u16-le` and value bytes. `yplib` calls the discriminator a type (`01` bytes, `03` little-endian integer); OpenBluetoothPrinter calls it response status. The bytes and parsing boundary agree, but its semantic name is not settled. Events do not follow this nested reply shape and must remain raw ([parser](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L181-L212), [event evidence](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L236-L238)).

A receiver must buffer notification fragments, locate `1a 01`, wait for the declared complete frame, require the `a1` terminator, verify CRC, and only then decode. It should tolerate noise and multiple frames per notification. `yplib` achieves this by accumulating an inbox for each status transaction before calling its resynchronizing parser; OpenBluetoothPrinter independently supplies an incremental decoder and explicitly tests split and coalesced frames ([yplib BLE receiver](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L74-L97), [corroborating decoder](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/protocol.ts#L245-L260)).

Only version byte `01` is established for this Y50P. Version `02` and command `05 1b` exist in U8 work, but no pinned Y50P capture uses them ([scope warning](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L97-L117)).

## Captured command stream

`buildStream(rows, 50, rate)` emits each hex payload below as a complete control frame, then inserts one unframed raster blob, then emits the trailer ([builder](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L66-L102), [assembly](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L145-L170)).

```text
Preamble payloads, in order:
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
05410402003200
05380402003200
05400402000000

Compression-rate set frame:
0539040100<rate:u8>

Raw raster bytes (not framed)

Trailer payloads, in order:
0521010000
0537010000
051a010000
050b010000  (ten times)
```

Established command names are: `01/04` model, `01/07` firmware, `01/02` serial, `01/b7` hardware info; `05/0b` status; `05/11` density; `05/19` start print; `05/1a` end print; `05/20` paper type; `05/21` paper locate; `05/36` first-task withdrawal; `05/37` end-task formfeed; `05/38` print width; `05/39` compression rate; `05/40` x reference; `05/41` canvas width; and `05/0f` event. `05/1b` is the unselected zlib route ([command table](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L179-L202)).

For the accepted media, `05/41` and `05/38` carry `0x0032` = 50 mm and `05/40` carries zero. The function parameter named `job` in `yplib` is actually sent as `05/39` compression rate; the source's default is `0x08`. Captured values include `0x13`, `0x08`, and `0x09`, and the upstream findings correct an earlier “job counter” interpretation ([correction](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L240-L252)). OpenBluetoothPrinter calculates the rate as truncated `encoded-data length / packed-bitmap length * 100`, excluding line markers, but that formula is U8-derived rather than directly established for Y50P ([corroborating calculation](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/raster.ts#L139-L166)). For first-byte compatibility, use the pinned builder's `0x08` unless a later hardware ticket proves a calculation is required.

The full trailer is material: `05/21` closes the raster, while `05/37` plus `05/1a` present the label at the tear bar. The incomplete horizontal-line capture stopped after `05/21` and left the label partly presented; the complete FlashLabel capture includes all three plus status polls ([evidence](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L258-L267)). The repeated handshake/status frames and trailing polls are captured behavior, but the minimum necessary subset has not been isolated; preserve them for the prototype.

## Raster and the non-negotiable width guard

The accepted prototype raster is 400 × 240 dots for 50 × 30 mm stock at 8 dots/mm. Pixel value `0` means white and `1` means black. For every row:

1. emit row marker `0x18`;
2. encode consecutive same-color pixels in runs of 1–128;
3. emit each run as `(color << 7) | (run_length - 1)`.

The implementation and decoder are compact and unambiguous ([codec](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L104-L143)). There is no raster byte length or row length on the wire. Row boundaries are recovered only by expanding runs until exactly 400 pixels have been produced, and label height is inferred from the number of rows, not transmitted ([height finding](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L251-L256)). A byte `0x18` can also be ordinary run data, so neither raster end nor row boundaries may be found by simply scanning for a marker.

Every row must be validated as exactly 400 binary pixels before transmission. `yplib` reports that a wrong-width row shifts every following row marker and can hang the firmware, and its builder refuses ragged or mismatched rows ([guard](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L145-L159), [tests](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/test/protocol.test.ts#L20-L39)). Do not rely on a Pillow canvas size alone: validate row count, each row length, and values in `{0, 1}` at the encoder boundary.

## Replies, status, and readiness

Send the framed request payload `05 0b 01 00 00` to poll status. A valid `05/0b` reply is ready only when its decoded value is exactly `0x00`. Known bits are:

| Bit | Meaning | Y50P evidence |
| --- | --- | --- |
| `0x01` | printing | U8 corroboration; not physically provoked in the Y50P study |
| `0x02` | cover open | physically measured on Y50P |
| `0x04` | out of paper | physically measured on Y50P |
| `0x08` | under voltage | U8 corroboration only |
| `0x10` | overheat | U8 corroboration only |

Y50P measurements were `00` closed/loaded, `04` closed/empty, and `06` cover open whether paper was present or not. Because opening the cover also trips the paper sensor, render `06` as “cover open” rather than the misleading “cover open, out of paper.” Preserve unknown bits in diagnostics ([measurements and interpretation](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L204-L238), [status formatter](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L215-L233)).

The pinned readiness algorithm polls before each label: return on `00`; continue only for `01`; fail immediately for any other non-null status; treat repeated silence as “printer not responding”; poll every 300 ms for at most 15 s ([orchestration](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/transport.ts#L27-L45)). A single status transaction waits 500 ms by default and yields null on silence. Printrow preserves this distinction, displaying silence as unknown rather than fault or ready ([transport](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L118-L131), [consumer behavior](https://github.com/slastra/printrow/blob/3e5923b1653e8205412bcd4d768515ea75dff4b3/src/lib/printer/drivers.ts#L108-L124)).

The upstream job function does **not** wait for ready after the last send: it marks progress immediately after all GATT writes have returned. A no-response GATT write completing is not evidence of physical print completion. Therefore, an integration that connects per job must not claim “printed” or disconnect immediately based only on send completion. The exact safe final wait/disconnect condition is unresolved and must be established on the target Y50P; until then, retain notifications, perform bounded post-send status observation, and report the outcome as uncertain if no reliable transition is observed.

## Y50P BLE contract

The hardware-verified Y50P LE personality advertises a name such as `Y50P_8895_LE` and exposes:

| Role | UUID | Operation |
| --- | --- | --- |
| Service | `000018f0-0000-1000-8000-00805f9b34fb` | primary service |
| Host → printer | `00002af1-0000-1000-8000-00805f9b34fb` | write without response |
| Printer → host | `00002af0-0000-1000-8000-00805f9b34fb` | notifications |

BLE printed a legible label and returned model, firmware, serial, and status without pairing. The observed ATT MTU was 23, so the hardware probe used 20-byte chunks and 10 ms inter-chunk sleeps ([hardware account](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/FINDINGS.md#L79-L95), [probe](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/reference/probes/ble-print.py)). The shipped Web Bluetooth transport uses the same service and characteristic roles, subscribes before sending, copies 20-byte chunks, uses write-without-response, and paces each chunk by 8 ms ([implementation](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L4-L19), [connect/send](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/web-bluetooth.ts#L62-L116)). Unpaced writes are documented as corrupting jobs because this write mode provides no printer-buffer backpressure.

For Home Assistant/Bleak, use 20 bytes and at least 8 ms pacing as the evidence-backed conservative baseline even if a larger negotiated write size is reported. Do not switch to write-with-response without hardware evidence: `2af1` is established specifically as write-without-response. Serialize complete jobs per device. If any chunk raises after transmission starts, do not replay automatically because the printer may have consumed a prefix.

The U8 is not a transport template for Y50P. It uses service `FF00`, data `FF02`, notify `FF01`, and a separate `FF03` flow-control characteristic with a credit window ([U8 profile](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/lib/flashtoy/bluetooth.ts#L1-L22)). Those UUIDs and credits must not appear in the Y50P implementation.

## Reusable test vectors and fixtures

Import these upstream vectors verbatim into Python unit tests ([source](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/src/protocol.ts#L236-L266)):

| Test | Expected |
| --- | --- |
| CRC of `050b010000` | `0x5c73542d` |
| CRC of `0104010000` | `0xf190e2bb` |
| CRC of `053904010013` | `0xfec2f091` |
| Full status-request frame | `1a010500050b0100002d54735ca1` |
| One all-white 400-dot row | `187f7f7f0f` |
| 400-dot row with black dots `[198, 202)` | `187f45837f45` |

Also vendor the three MIT-licensed binary fixtures from the pinned `captures/` directory and retain their hashes:

| Fixture | Bytes | SHA-256 | Established use |
| --- | ---: | --- | --- |
| `y50p-flashlabel-label.bin` | 3,106 | `c2838f27da086f993da6be3793d9f762e60936746f14249e9fc36792a2ce8465` | Full stream rebuild, rate `0x13`, full trailer |
| `y50p-horizontal-line.bin` | 1,507 | `0d4e7b64a07b2767eaa96ab05206241e560b009e2da9e337bde6e176a08dcda0` | Full rebuild through its intentionally truncated one-frame trailer, rate `0x08` |
| `y50p-vertical-line.bin` | 1,581 | `dd19f0e9fbedf097ad27e37a8831b286e47081c9634476e0d1a2c6e4e69d7c0d` | Decode/CRC conformance and vertical-run vector; capture begins mid-session |

All three must decode to exactly 240 rows of 400 dots and all control-frame CRCs must validate. The first two rebuild byte for byte under the limitations above ([conformance cases](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/test/captures.test.ts#L57-L93)).

## Licensing

`yplib` and Printrow are MIT licensed, copyright 2026 Shaun Lastra; OpenBluetoothPrinter is MIT licensed, copyright 2026 Yuhang ([yplib license](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/LICENSE), [Printrow license](https://github.com/slastra/printrow/blob/3e5923b1653e8205412bcd4d768515ea75dff4b3/LICENSE), [OpenBluetoothPrinter license](https://github.com/Souukou/OpenBluetoothPrinter/blob/c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad/LICENSE)). A direct port or copied fixtures must retain the relevant copyright and permission notices. `yplib` already preserves the OpenBluetoothPrinter notice for command names, direction naming, and the three U8-derived status bits ([acknowledgement](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/ACKNOWLEDGEMENTS.md)). The vendor SDK binary is not redistributed; only an `nm` symbol listing is present, so it is not a source to copy.

## Unresolved uncertainties for implementation and hardware acceptance

- **Final completion and disconnect:** no Y50P source proves when it is safe to disconnect after the final write. The upstream preflight polling is proven; postflight completion is not.
- **HA/Bleak pacing:** 20-byte chunks with 8–10 ms delay are demonstrated baselines, but the best Bleak call and exact delay on Home Assistant OS need a physical run. Larger MTUs must not silently increase the first prototype's chunk size.
- **Notification races:** subscribe before requests and keep a persistent fragment buffer. The behavior of late replies after a 500 ms timeout should be logged and correlated rather than allowed to satisfy a later request.
- **Compression-rate byte:** its command meaning and captured values are established; the exact Y50P calculation is not. Use upstream's capture-compatible `0x08` for the first fixed label.
- **Event `05/0f`:** payload bytes have been observed but are not decoded. Preserve and log them as hex.
- **Status bits:** `01`, `08`, and `10` are corroborated on U8 but were not physically provoked on Y50P. Unknown bits must survive diagnostics.
- **Minimum command sequence:** the captured repeated queries and keep-alives work, but upstream did not prove which can be removed. Preserve them initially.
- **Scope:** only YPL v1, the Y50P `18f0/2af1/2af0` BLE profile, and 50 × 30 mm / 400 × 240 are accepted. Sibling models, other widths, v2 zlib, U8 raw-row `0x16`, and U8 flow control are outside the evidence.
