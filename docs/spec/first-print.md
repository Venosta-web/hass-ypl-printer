# First-print prototype specification

This is the single implementation specification for the **first-print prototype**: a Home Assistant custom integration whose acceptance criterion is a plain FlashLabel/KNAON Y50 producing a physical 50 × 30 mm test label directly from Home Assistant OS.

It consolidates the decisions of the wayfinder map [#1](https://github.com/Venosta-web/hass-ypl-printer/issues/1) and its tickets #2–#10. Where a later decision superseded earlier wording, this document carries only the later wording and records the change in [Superseded wording](#13-superseded-wording). Terms in **bold** are defined in [`CONTEXT.md`](../../CONTEXT.md).

**Precedence.** Implementers follow this document. Anything it does not fix is either an explicitly named implementation choice or a gap: a gap reopens the owning ticket; it is never filled by guesswork. Any change listed under [Evidence loop](#12-evidence-loop-for-plain-y50-adaptation) as "reopens the owning ticket" follows the same rule.

Evidence:

- [`docs/research/ypl-wire-and-ble.md`](../research/ypl-wire-and-ble.md) (#2)
- [`docs/research/home-assistant-bluetooth-conventions.md`](../research/home-assistant-bluetooth-conventions.md) (#3)
- [`docs/research/growspace-label-printing-compatibility.md`](../research/growspace-label-printing-compatibility.md) (#4)

Pinned baselines:

| Source | Pin |
| --- | --- |
| yplib | `4748c21393b30f78defed256a7394d0960ad9a07` |
| Printrow (consumer evidence only) | `3e5923b1653e8205412bcd4d768515ea75dff4b3` |
| OpenBluetoothPrinter (corroboration only) | `c21f3b9cfc07c6fe898a1344ef137f70c0fbd6ad` |
| Home Assistant Core | 2026.9.2 (`33c3e0c`), or the current stable release when implementation starts — recorded either way |
| bleak-retry-connector (Core-pinned) | 4.7.0 (`84bda39`) |
| DejaVu Fonts | 2.37 (`0eda8a319c08835009849583cd090bb5b141ce25`) |

## 1. Scope

In scope: one fixed media profile (50 × 30 mm = 400 × 240 dots), Basic-Latin multiline text, Home Assistant Bluetooth discovery, one config entry per printer, one blocking print action, and the hardware procedure.

Out of scope: Growspace Manager changes, printer selection UI, backend abstraction, HACS release hardening, broad Home Assistant version compatibility, entities (status or preview), image/QR/logo input, other media sizes, other printer families, ESPHome Bluetooth proxies as an acceptance path, and upstream changes to yplib or Printrow. Growspace compatibility is an architectural seam only ([§5.10](#510-growspace-seam)).

## 2. Repository layout and scaffolding

- Integration directory: `custom_components/ypl_printer/`. Module and class names inside it are implementation choices.
- `manifest.json`:

  ```json
  {
    "domain": "ypl_printer",
    "name": "YPL Printer",
    "version": "0.1.0",
    "codeowners": ["@Venosta-web"],
    "documentation": "https://github.com/Venosta-web/hass-ypl-printer",
    "config_flow": true,
    "integration_type": "device",
    "iot_class": "local_push",
    "dependencies": ["bluetooth_adapters"],
    "bluetooth": [{ "local_name": "Y50*", "connectable": true }],
    "requirements": []
  }
  ```

  Do not require service UUID `0x18F0` in the matcher. Do not list Bleak, bleak-retry-connector, or Pillow: Core supplies and pins them.
- `THIRD_PARTY_NOTICES.md` (or equivalent) with the full MIT notices for yplib / Shaun Lastra and OpenBluetoothPrinter / Yuhang. The DejaVu licence ships beside the font.
- Tests under `tests/`, dependencies in `requirements_test.txt` pinning the `pytest-homeassistant-custom-component` release that matches the targeted Core. Python version is whatever that release requires.
- CI: one GitHub Actions workflow on every push running `pytest` and `hassfest`. No lint gate, no coverage-percentage gate, no HACS validation.
- `docs/protocol.md` ([§3.8](#38-protocol-documentation)).

## 3. YPL protocol module

A narrow, fail-closed, pure-Python port of the captured YPL-v1 path from yplib. It performs no I/O, no BLE chunking, and no text or image rendering.

### 3.1 Required capabilities

- payload-only YPL CRC-32;
- YPL-v1 control-frame encoding;
- incremental control-frame decoding that buffers fragments, handles coalesced frames, resynchronises on `1a 01`, and validates length, terminator and CRC before decoding;
- reply-envelope decoding with raw preservation of unsolicited events;
- strict fixed-profile raster validation;
- row-RLE encoding and decoding;
- construction of the exact one-label stream ([§3.4](#34-exact-first-print-stream)), returned as an ordered sequence of byte segments: framed preamble commands, one unframed raw raster segment, framed trailer commands;
- status-value decoding that preserves unknown bits.

### 3.2 Frame format

```text
1a 01 <payload_length:u16-le> <payload> <crc32:u32-le> a1
payload = <group:u8> <command:u8> <direction:u8> <data_length:u16-le> <data>
```

CRC covers only `payload`: reflected polynomial `0xEDB88320`, initial register `0xCA896ADE`, final xor `0xFFFFFFFF`, little-endian. Python equivalent: `zlib.crc32(payload, 0xCA896ADE ^ 0xFFFFFFFF)`.

Directions: `01` request, `02` reply, `03` unsolicited event, `04` set. Reply discriminators `01` (bytes) and `03` (little-endian integer) are decoded; unknown discriminators, commands and event payloads stay raw. Never invent semantics.

Malformed frames and rasters raise explicit protocol errors; invalid lengths, nested lengths, terminators, CRCs, directions, row markers, run overruns, incomplete rows or trailing raster data are never accepted or normalised. A notification decoder may discard a malformed candidate to resynchronise but must surface it for diagnostics. A status affects readiness only after strict validation of a v1 `05/0b` direction-`02` reply with a complete nested integer envelope.

### 3.3 Command vocabulary

`01/02` serial, `01/04` model, `01/07` firmware, `01/b7` hardware info; `05/0b` status, `05/0f` opaque event, `05/11` density, `05/19` start print, `05/1a` end print, `05/20` paper type, `05/21` paper locate, `05/36` first-task withdrawal, `05/37` end-task formfeed, `05/38` print width, `05/39` compression rate, `05/40` x reference, `05/41` canvas width.

### 3.4 Exact first-print stream

Preserve this order and every repetition. The minimum sequence is unproven; do not optimise.

```text
Preamble payloads:
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

Compression-rate set payload:
053904010008

Raw raster bytes (not framed)

Trailer payloads:
0521010000
0537010000
051a010000
050b010000  (ten times)
```

`05/39` is fixed at `0x08`; there is no dynamic compression-rate formula.

### 3.5 Raster invariants

Before any connection: exactly 240 rows, exactly 400 entries per row, every entry the literal integer `0` (white) or `1` (black). Booleans and other numerics are rejected.

Each row starts with `0x18`; equal-pixel runs of 1–128 encode as `(color << 7) | (run_length - 1)`. No row or raster length is on the wire. A decoder finds a row boundary only after expanding exactly 400 pixels, requires the next marker there, and requires exactly 240 rows. Never scan for `0x18` as a delimiter. A wrong-width row can hang firmware; these guards are unconditional.

### 3.6 Status vocabulary

| Bit | Meaning | Evidence |
| --- | --- | --- |
| `0x00` (raw zero) | ready | yplib |
| `0x01` | printing | U8 corroboration only |
| `0x02` | cover open | measured on Y50P |
| `0x04` | paper out | measured on Y50P |
| `0x08` | undervoltage | U8 corroboration only |
| `0x10` | overheat | U8 corroboration only |

Measured Y50P `0x06` means cover open (the paper sensor also trips). Other bits stay numeric.

### 3.7 Vectors and fixtures

| Vector | Expected |
| --- | --- |
| CRC of `050b010000` | `0x5c73542d` |
| CRC of `0104010000` | `0xf190e2bb` |
| CRC of `053904010013` | `0xfec2f091` |
| Full status-request frame | `1a010500050b0100002d54735ca1` |
| All-white 400-dot row | `187f7f7f0f` |
| 400-dot row, black `[198, 202)` | `187f45837f45` |

Vendor these MIT fixtures from yplib with provenance comments and verify hashes before use:

| Fixture | Bytes | SHA-256 |
| --- | ---: | --- |
| `y50p-flashlabel-label.bin` | 3,106 | `c2838f27da086f993da6be3793d9f762e60936746f14249e9fc36792a2ce8465` |
| `y50p-horizontal-line.bin` | 1,507 | `0d4e7b64a07b2767eaa96ab05206241e560b009e2da9e337bde6e176a08dcda0` |
| `y50p-vertical-line.bin` | 1,581 | `dd19f0e9fbedf097ad27e37a8831b286e47081c9634476e0d1a2c6e4e69d7c0d` |

All three decode to 240 × 400 with valid CRCs. Rebuild the FlashLabel capture byte for byte. Rebuild the horizontal-line capture byte for byte through its intentionally truncated one-frame trailer, documenting that. The vertical-line capture starts mid-session and is decode evidence only.

### 3.8 Protocol documentation

`docs/protocol.md` documents only the supported subset — frame format and CRC, stream order, row encoding and invariants, the `18F0`/`2AF1`/`2AF0` profile and chunking, status bits with evidence strength — citing pinned upstream commits for every fact. It lists the unsupported boundaries ([§3.9](#39-explicitly-unsupported)), states that the project is not affiliated with Yoctopuce ("YPL" is the prior-art name used by the cited projects), and must not contradict the tests.

### 3.9 Explicitly unsupported

Other media; sibling printer families or alternate BLE profiles; YPL v2 and `05/1b` zlib; U8 raw-row marker `0x16`; U8 `FF00`–`FF03` transport and credit flow control; dynamic compression rate; semantic interpretation of `05/0f`; multi-label jobs, cancellation commands, pairing assumptions, extension hooks; treating successful GATT writes as proof of printing; automatic replay after the **transmission boundary**. Do not redistribute the vendor SDK binary.

## 4. Home Assistant architecture

One config entry and one device-registry device per printer, discovered and connected only through Home Assistant's Bluetooth stack. No manual address entry, coordinator, entities, independent scanner, persistent BLE client, or cross-printer lock.

### 4.1 Config flow

- Starts only from Bluetooth discovery. `async_step_user` aborts with `discovery_only`.
- Unique ID: `format_mac(address)`. Rediscovery of a configured normalised address aborts with `already_configured`.
- Always shows a confirmation step. Title: advertised name, falling back to `YPL printer`. Entry data keeps the raw HA Bluetooth address.
- **Profile check** before creating the entry: resolve the device with `bluetooth.async_ble_device_from_address(hass, address, connectable=True)`, connect with `establish_connection` using Core's default retries, hold no lock, and only inspect the discovered services for service `0x18F0` with characteristics `0x2AF0` (notify) and `0x2AF1` (write). No notification subscription and no writes. Always disconnect in `finally`.
- Abort reasons: `cannot_connect` (device unresolvable or any connection error), `not_supported` (service or characteristic missing), `already_configured`, `discovery_only`. All are translated.

### 4.2 Setup, device registration, unload, removal

- Setup performs no connection. It creates or updates one device with identifier `(DOMAIN, normalized_address)`, connection `(CONNECTION_BLUETOOTH, normalized_address)`, the entry associated, and only evidence-backed manufacturer/model metadata.
- Typed `ConfigEntry.runtime_data` holds only the raw address, the display name, and one `asyncio.Lock`. Never keep `BLEDevice` or `BleakClient` between jobs.
- Unload clears the entry's runtime/routing association, leaves the service registered, and does not disturb other entries. A running job keeps its runtime reference and finishes its cleanup.
- Removal calls `bluetooth.async_rediscover_address` for the stored address.
- A changed Bluetooth address is a new printer; address migration is out of scope.

### 4.3 Per-job BLE lifecycle

While holding the entry lock: resolve a fresh connectable `BLEDevice`; connect a new client via Core's `establish_connection`; verify the profile; subscribe to `0x2AF0` before any write (the callback only feeds the parser/queue); run preflight, transmission and post-send observation ([§6](#6-transmission-state-machine)); clean up. The lock is held from device resolution through final disconnect.

## 5. Print action and renderer

### 5.1 Registration and schema

Register `ypl_printer.print` once in integration-level `async_setup` with `SupportsResponse.OPTIONAL`; never per entry, never removed on unload.

```yaml
print:
  fields:
    device_id:
      required: true
      selector:
        device:
          filter:
            integration: ypl_printer
          multiple: false
    text:
      required: true
      selector:
        text:
          multiline: true
```

The runtime Voluptuous schema validates exactly these two fields and rejects extras. Names and descriptions live in `strings.json`. No other fields exist (no width, height, density, rotation, copies, preview, font, alignment, raster, image or NIIMBOT payload).

### 5.2 Target resolution

`device_id` is one scalar device ID resolved directly (no deprecated target helpers) to exactly one loaded `ypl_printer` entry. The handler checks ownership itself because the selector filter is not enforced at runtime. Failures are raised before rendering or connecting ([§7](#7-error-and-translation-keys)). There is no default printer, entity or area target, list target, or address input.

### 5.3 Label text

Normalise `CRLF` and bare `CR` to `LF`, then validate. Valid characters: `U+0020`–`U+007E` and `LF`. Preserve every accepted character, including repeated, leading and trailing spaces and blank or trailing lines. Reject tabs, other controls, DEL and every non-Basic-Latin code point. Reject empty input and input consisting only of spaces and line feeds. Never replace, transliterate, trim, wrap, truncate or drop input. All validation and the complete render finish before any Bluetooth activity.

### 5.4 Bundled font

Vendor the unmodified `DejaVuSans.ttf` from DejaVu Fonts 2.37 with its full licence beside it. Never use a system font or name lookup. Do not subset or modify it.

- one-font archive SHA-256: `5c6e497a2f36552cb5ffb112c413a6af39c0f3c47653662b90b4fa6499822fd7`
- TTF size: 757,076 bytes
- TTF SHA-256 (test-locked): `7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954`

### 5.5 Layout

- Landscape 400 × 240 canvas, white background, black text, 16 px margin on every edge (content box 368 × 208).
- No automatic wrapping. Split on `LF`; every line, including blank ones, takes one line cell.
- Search integer sizes 160 down to 12; choose the first that fits, using only the bundled TTF with `ImageFont.Layout.BASIC`:
  - line-cell height = ascent + descent; gap between cells = 4 px;
  - block height `n × cell + (n − 1) × 4` ≤ 208;
  - every line's logical advance (including spaces) ≤ 368;
  - after centring, every line's ink bounds stay inside the content box.
- Centre each line horizontally by its logical advance and the block vertically, using floor division for half pixels. Blank lines draw nothing. Draw with an explicit top-left anchor and zero stroke width.
- If size 12 does not fit, fail validation (`text_does_not_fit`). Never clip or go below 12.

### 5.6 Binary rasterisation

Render on an 8-bit `L` canvas initialised to 255 with fill 0 and grayscale antialiasing explicitly enabled. No resize, rotate, dither, autocontrast, colour management or implicit 1-bit conversion. Per pixel: `0..127 → 1` (black), `128..255 → 0` (white). Return 240 rows × 400 literal integers and revalidate before encoding ([§3.5](#35-raster-invariants)).

Determinism holds for the recorded runtime, bundled font and exact inputs. A different Pillow or FreeType that changes the golden raster is an explicit contract review.

### 5.7 Fixed media

Size is not public input. Any internal dimension mismatch, non-binary pixel or encoder precondition failure fails closed before connecting. No scaling, cropping, padding, rotation or media negotiation.

### 5.8 Completion semantics

The handler awaits the whole job. A normal return means validation, rendering, complete transmission and bounded post-send observation finished without an observed failure. It is never proof of a physical label. Failures and uncertainty are always raised, never returned as response data.

### 5.9 Diagnostic response

`return_response` false → return `None`. True → return exactly:

```json
{
  "device_id": "home-assistant-device-id",
  "raster_width": 400,
  "raster_height": 240,
  "raster_sha256": "lowercase-hex-sha256",
  "encoded_bytes": 1234,
  "ble_chunks": 62,
  "printer_status": { "raw": 0, "flags": ["ready"], "unknown_bits": 0 }
}
```

- `raster_sha256`: SHA-256 of the row-major 96,000 bytes, one byte (`0` or `1`) per dot.
- `encoded_bytes`: total length of the complete ordered job.
- `ble_chunks`: chunks whose write call returned.
- `printer_status`: `null`, or the last strictly validated status of this job, with `raw`, `flags` (known names in ascending bit order: `printing`, `cover_open`, `paper_out`, `undervoltage`, `overheat`; raw zero → `["ready"]`) and `unknown_bits` (`raw & ~0x1f`).

No `success`, `printed`, `error` or message fields.

### 5.10 Growspace seam

Keep text → raster, raster → YPL, and BLE transport as separate layers so a later adapter can replace composition while keeping fixed 400 × 240 output, exact device targeting, blocking error semantics and per-printer serialisation. This is not `niimbot.print` compatibility.

## 6. Transmission state machine

One print attempt is a forward-only, lock-held state machine. Timing values are the evidence-backed starting point; they change only through the [evidence loop](#12-evidence-loop-for-plain-y50-adaptation).

### 6.1 Phases and outcomes

Phases (for logging only): `waiting_for_lock` → `resolving` → `connecting` → `subscribing` (including the profile check) → `preflight` → `transmitting` → `post_send_observation` → `cleanup`.

Outcomes: **completed** (normal return); **failed before transmission** (translated `HomeAssistantError`, printer known not to have received print data); **uncertain physical outcome** (`PrintOutcomeUncertainError(HomeAssistantError)` or equivalent subclass).

### 6.2 Lock

Wait at most 120 s for the entry lock, else `printer_busy`. Different printers never wait for each other.

### 6.3 Retry and transmission boundary

- No application-level retry. Only `establish_connection`'s own attempts, with Core defaults. Subscribe, preflight and writes are never retried.
- The **transmission boundary** is the moment the first print-stream write is *started*, whether or not it succeeds. Preflight polls do not cross it.
- After the boundary: never reconnect, replay or resend.

### 6.4 Status polls and correlation

A status poll is one write of the `05/0b` request frame (13 bytes, one write) to `0x2AF1` with `response=False` and a **5 s** write timeout, followed by a wait of up to **500 ms** for a reply. A reply satisfies a poll only if it is a strictly valid `05/0b` direction-`02` reply decoded *after* that poll was sent; late replies are logged and never satisfy a later poll. Any satisfied poll resets the consecutive-silence count.

### 6.5 Preflight

After subscribing, poll every 300 ms:

| Observation | Action |
| --- | --- |
| `0x00` | Start transmission. |
| `0x01` | Keep polling; still `0x01` after 15 s → `printer_busy`. |
| Any other valid status (including unknown bits) | `printer_not_ready` with flags and raw; zero stream writes. |
| 3 consecutive silent polls | WARNING, transmit with status unknown. |
| Poll write raises or times out | `communication_failed`. |

### 6.6 Transmission

- Concatenate the [§3.4](#34-exact-first-print-stream) segments and split into 20-byte chunks regardless of reported MTU or frame boundaries.
- Per chunk: `write_gatt_char(0x2AF1, chunk, response=False)` with a 5 s timeout, then `asyncio.sleep(0.010)`. No extra pause between segments; pacing is not configurable.
- Write error, write timeout or disconnect → `outcome_uncertain_transport`.

### 6.7 Notifications during transmission

- Replies to the stream's embedded status polls are not correlated individually. They update "last valid status" and error detection only.
- A valid status other than `0x00`/`0x01` (including unknown bits) does not stop the stream; the full stream is sent and the job then raises `outcome_uncertain_printer_error`. Only transport failure ends transmission early.
- `05/0f` events and non-status replies are logged raw and never affect the outcome. Malformed candidates are logged and skipped.

### 6.8 Post-send observation

After the last write, wait 1 s, then poll every 300 ms (per [§6.4](#64-status-polls-and-correlation)) for at most 15 s. Evaluate in this order:

1. Any valid status other than `0x00`/`0x01` seen at any point after the boundary (including unknown bits) → `outcome_uncertain_printer_error`.
2. Poll write fails, times out, or the link drops → `outcome_uncertain_transport`.
3. A correlated `0x00` → completed with that status.
4. 3 consecutive silent polls → stop polling and decide from the last valid status of this job: `0x00` → completed with it; `0x01` → `outcome_uncertain_still_printing`; none → completed with `printer_status: null` and a WARNING.
5. Still `0x01` at the 15 s deadline → `outcome_uncertain_still_printing`.

### 6.9 Cancellation and cleanup

- No overall job deadline; the phase limits bound the job.
- On `CancelledError`: always clean up; if the boundary was crossed, log a WARNING that the outcome is uncertain; re-raise unchanged.
- Cleanup: if connected, `stop_notify` (2 s timeout) then `disconnect` (10 s timeout). Cleanup failures are WARNING and never replace the original error or cancellation.

### 6.10 Logging

Every line includes the entry title and Bluetooth address.

- DEBUG: phase changes with durations; connection attempts; every notification as raw hex; every decoded frame, event and malformed candidate; every status poll and result; a final summary (bytes and chunks sent/total, duration, last status). Individual chunks are not logged. Exception text goes here.
- WARNING: uncertain outcomes (phase, sent/total, last status, cause class); preflight proceeding on silence; no status after sending; cleanup failures; cancellation after the boundary.
- Nothing at INFO on success; raised errors are not re-logged at ERROR.

## 7. Error and translation keys

All exceptions are translated, raised `from` the underlying exception, and carry only fixed facts. `strings.json` defines every key below with exactly the listed placeholders.

**Validation** (`ServiceValidationError`, raised before rendering or connecting):

| Key | Trigger | Placeholders |
| --- | --- | --- |
| `invalid_target` | `device_id` missing, malformed, unknown, foreign, or mapping to zero or several entries | `{device_id}` |
| `printer_not_loaded` | the printer's entry exists but is not loaded | `{name}` |
| `empty_text` | empty, or only spaces and line feeds | – |
| `unsupported_character` | first rejected code point | `{position}` (0-based index after newline normalisation), `{codepoint}` (e.g. `U+0009`) |
| `text_does_not_fit` | does not fit at size 12 | `{lines}` |

**Render fault** (`HomeAssistantError`, before connecting):

| Key | Trigger | Placeholders |
| --- | --- | --- |
| `render_failed` | renderer, font or encoder fault not caused by the caller, including protocol faults raised before connecting | `{error}` (exception class name) |

**Failed before transmission** (`HomeAssistantError`):

| Key | Trigger | Placeholders |
| --- | --- | --- |
| `printer_busy` | lock wait > 120 s, or preflight still `0x01` after 15 s | `{name}` |
| `printer_not_found` | no connectable `BLEDevice` | `{name}`, `{reachability}` (HA reachability diagnostics, passed through unparsed) |
| `no_connection_slots` | `BleakOutOfConnectionSlotsError` | `{name}` |
| `connection_failed` | other connector errors after its attempts | `{name}`, `{error}` (class name) |
| `unsupported_profile` | `18F0`/`2AF0`/`2AF1` missing at job time | `{name}` |
| `communication_failed` | `start_notify` fails, or a preflight poll write fails or times out | `{name}`, `{error}` |
| `printer_not_ready` | preflight reads a valid status other than `0x00`/`0x01` | `{name}`, `{flags}`, `{raw}` (e.g. `0x06`) |

**Uncertain physical outcome** (one exception subclass):

| Key | Trigger |
| --- | --- |
| `outcome_uncertain_transport` | write error/timeout or disconnect during transmission or post-send observation |
| `outcome_uncertain_printer_error` | error status after the boundary |
| `outcome_uncertain_still_printing` | still `0x01` when post-send observation ends |

Placeholders: `{name}`, `{sent}`, `{total}` (chunks), `{flags}`, `{raw}` (or `unknown`). Every message says the label may or may not have printed, that it must not be resent automatically, and that the printer should be checked before reprinting.

**Config-flow aborts**: `already_configured`, `cannot_connect`, `not_supported`, `discovery_only`.

## 8. Acceptance specimen

Four lines; the third is empty:

```text
YPL Y50 TEST 50x30
abc xyz 0123456789

!"#$%&'()*+,-./:;?@[]_{}~
```

Service string: `"YPL Y50 TEST 50x30\nabc xyz 0123456789\n\n!\"#$%&'()*+,-./:;?@[]_{}~"`. It is one shared test constant. If it fails to fit, only line 4 may be shortened, in that constant only.

## 9. Automated evidence

The suite uses scripted fakes only: no real Bluetooth and no real sleeping (patched time). The commit installed for hardware testing must be green in CI. The named tests below are the gate.

**Protocol** (§3): every vector and fixture hash in §3.7; decoding and rebuilding the fixtures as specified; malformed frame and raster rejection; fragmented, coalesced and resynchronised notification decoding.

**Service and renderer** (§5, §7):

1. runtime schema cardinality, required fields, extra-field rejection;
2. foreign, unknown, ambiguous and unloaded targets;
3. newline normalisation and every text rejection boundary;
4. preservation of spaces and blank/trailing lines;
5. largest fitting size, centring, margins, overflow rejection, no wrapping;
6. font hash, `127 → 1` / `128 → 0` threshold, raster shape/domain, golden raster SHA-256 of the specimen;
7. no connection attempt on any validation or render failure;
8. response requested vs omitted, canonical raster hash input, byte/chunk counts, nullable status, flag order, unknown bits;
9. translated validation, execution and uncertain-outcome exceptions;
10. no replay after the transmission boundary.

**State machine** (§6):

1. happy path: exact chunk order, 20-byte chunks, 10 ms sleep after every chunk;
2. preflight: `00` → transmit; `01`→`00` → transmit; `01` for 15 s → `printer_busy`; error status or unknown bit → `printer_not_ready` with zero stream writes; silence → transmit with WARNING; poll write timeout (5 s) → `communication_failed`;
3. a late reply does not satisfy a later poll; fragmented/coalesced notifications; malformed frames skipped and logged;
4. error status during transmission → full stream still sent → `outcome_uncertain_printer_error`;
5. post-send: `00` → success; still `01` at deadline → `still_printing`; `01` then 3 silent polls → `still_printing`; error status or unknown bit → `printer_error`; silence with no status in the job → success with `printer_status: null`; `00` seen earlier then silence → success with that status;
6. first stream write raises → uncertain, not `communication_failed`, no reconnect;
7. write timeout → uncertain;
8. lock timeout; two printers concurrently; same-printer jobs serialised;
9. cancellation before and after the boundary: cleanup runs, `CancelledError` re-raised, WARNING only after the boundary;
10. `stop_notify` or disconnect failure/timeout → original error still raised;
11. each connector exception type → its key.

**Config flow** (§4), using Home Assistant's Bluetooth test fixtures: `Y50*` discovery → confirmation; profile check passes → entry, fails → `not_supported` with no entry; unreachable → `cannot_connect`; configured address → `already_configured`; user step → `discovery_only`; every probe disconnects; setup registers the device; unload keeps the service; removal requests rediscovery.

**End-to-end golden**: specimen → raster (pinned hash) → complete ordered job → exact list of 20-byte chunks, with job byte count, chunk count and job SHA-256 pinned. These are the bytes CI expects the hardware test to send.

**Attribution**: notices file with both full MIT notices; DejaVu licence beside the font; fixture and font hashes checked.

**Translations**: `strings.json` defines every key in §7 with exactly its placeholders.

## 10. Installation for the hardware test

- Tag the green commit (e.g. `v0.1.0-proto`).
- Copy `custom_components/ypl_printer` into `/config/custom_components/` via the SSH or Samba add-on; restart Home Assistant.
- Acceptance path: the HA host's **local Bluetooth adapter**.
- Enable DEBUG logging for `custom_components.ypl_printer` and `bleak_retry_connector` before any attempt that may need a failure bundle.

The report records: tag and commit SHA; HA OS and Core versions; adapter model; Pillow and FreeType versions read inside the Home Assistant container with:

```bash
docker exec homeassistant python -c "import PIL, PIL.features as f; print(PIL.__version__, f.version('freetype2'))"
```

## 11. Hardware procedure

Each step must pass before the next starts; the report records every step.

1. **Discovery** — HA discovers the `Y50*` advertisement. Record advertised name, RSSI, and whether `18F0` is advertised.
2. **Setup** — confirmation and profile check pass; entry and device exist. Proves **transport compatibility** only.
3. **Print** — call `ypl_printer.print` from Developer Tools → Actions with the specimen and response enabled. Record the response JSON. Compare `raster_sha256`, `encoded_bytes` and `ble_chunks` with the pinned golden values and record the result. A mismatch is a **determinism deviation**: it is recorded in the report and filed as a follow-up against the renderer contract, but does not by itself fail this step.
4. **Inspect** — photograph the label. Passes when all four lines are fully readable, no character is cut off, and there is no row shift, skew or garbled band. Proves **print compatibility**.
5. **Repeat** — print the specimen again immediately; steps 3–4 must pass again.

Optional, recorded but not acceptance: cover open (expected `printer_not_ready`); printer off (expected `printer_not_found`). They show whether a plain Y50 answers `05/0b`.

**Failure evidence bundle**: failing step and translated key (or physical defect); DEBUG log of exactly one attempt with the address left in; response JSON if any; photo of any output; HA Bluetooth advertisement monitor entry; the §10 versions. After an uncertain outcome, check the printer physically before any retry; never resend automatically.

**Record**: commit `docs/hardware-tests/<YYYY-MM-DD>-y50-local-adapter.md` with photos in the same folder and link it from a comment on #1. The prototype succeeds only when that report shows steps 1–5 passing on a green CI build.

## 12. Evidence loop for plain-Y50 adaptation

- **Allowed with a log citation:** the 1 s post-send wait, the 15 s limits, the three-silent-polls rule, and chunk pacing (10 ms; 20 ms is the most conservative option).
- **Reopens the owning ticket first:** any change to a §3 byte or the stream order, a BLE characteristic or the 20-byte chunk size, the transmission boundary, or the §5 public contract.
- **Escalation:** after two failed tuned attempts, stop tuning and capture a Bluetooth HCI log of the vendor app printing a comparable 50 × 30 label on the plain Y50. That capture is the reference for any Y50-specific adaptation, which is handled by a conditional ticket opened only from that evidence.

## 13. Superseded wording

| Earlier wording | Replaced by |
| --- | --- |
| #6 and #7: retry/uncertainty boundary "after the first completed/successful write" | #8: the **transmission boundary** is the first print-stream write *started* ([§6.3](#63-retry-and-transmission-boundary)) |
| #6: "optionally stop notifications" | #8: `stop_notify` if still connected, 2 s timeout ([§6.9](#69-cancellation-and-cleanup)) |
| #7: validation errors without named keys | #10: [§7](#7-error-and-translation-keys) validation and render keys |
| #8: post-send "any error bit"; silence after a seen status | #10: any status other than `0x00`/`0x01` is an error; ordered evaluation in [§6.8](#68-post-send-observation) |
| #8: poll write "times out" without a value | #10: 5 s ([§6.4](#64-status-polls-and-correlation)) |
| #9: golden bytes "are the exact bytes sent during the hardware test" | #10: step 3 compares and records any determinism deviation ([§11](#11-hardware-procedure)) |

## 14. Implementation order

Tickets are listed on map #1. Each blocks the ones after it except where noted.

1. [#13](https://github.com/Venosta-web/hass-ypl-printer/issues/13) Project scaffold and CI — §2.
2. [#14](https://github.com/Venosta-web/hass-ypl-printer/issues/14) YPL protocol module — §3, §9 protocol tests.
3. [#15](https://github.com/Venosta-web/hass-ypl-printer/issues/15) Deterministic renderer — §5.4–§5.7, §8, §9 renderer tests (parallel with 2).
4. [#16](https://github.com/Venosta-web/hass-ypl-printer/issues/16) Config flow and device lifecycle — §4, §9 config-flow tests (after 1).
5. [#17](https://github.com/Venosta-web/hass-ypl-printer/issues/17) Print action contract — §5.1–§5.3, §5.8–§5.9, §7, §9 service tests (after 2, 3, 4).
6. [#18](https://github.com/Venosta-web/hass-ypl-printer/issues/18) Transmission state machine — §6, §9 state-machine and end-to-end tests (after 5).
7. [#19](https://github.com/Venosta-web/hass-ypl-printer/issues/19) Hardware test (HITL) — §10–§12 (after 6).
