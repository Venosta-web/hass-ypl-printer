# Home Assistant Bluetooth conventions for the YPL printer prototype

Research date: 2026-09-16

## Decision

Implement the prototype as one discovery-created config entry per printer, with
the discovered Bluetooth address as its stable identity. Let Home Assistant own
discovery and adapter selection; resolve a fresh `BLEDevice` from Home
Assistant immediately before each print, connect through
`bleak-retry-connector`, subscribe to notifications, transmit one job, and
disconnect in `finally`.

Register `ypl_printer.print` exactly once in `async_setup`, not once per config
entry. Route the service target to one loaded config entry, serialize jobs with
an `asyncio.Lock` stored in that entry's runtime data, and register the service
with `SupportsResponse.OPTIONAL`. Connection establishment may retry; after the
first successful write, never replay the job automatically because the physical
outcome is uncertain.

This recommendation targets **Home Assistant Core 2026.9.2**, tag commit
[`33c3e0cca60e73a8c4970ee677d75b8bc6464cdf`](https://github.com/home-assistant/core/tree/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf),
released 2026-09-11. The developer documentation inspected is commit
[`da3372bc7844c233c5ffa959b2ffda802f33b0bb`](https://github.com/home-assistant/developers.home-assistant/tree/da3372bc7844c233c5ffa959b2ffda802f33b0bb).
Core 2026.9.2 pins Bleak 3.0.2 and bleak-retry-connector 4.7.0 in its
[`bluetooth` manifest](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/homeassistant/components/bluetooth/manifest.json).
The connector APIs were checked at the pinned 4.7.0 commit
[`84bda39c71ea13d3d4fbab3fbe2a66e85f91b86e`](https://github.com/Bluetooth-Devices/bleak-retry-connector/tree/84bda39c71ea13d3d4fbab3fbe2a66e85f91b86e)
and at current 4.7.1 commit
[`0bdeaf11d91aa954ee0973ba06c9aba52ddb9971`](https://github.com/Bluetooth-Devices/bleak-retry-connector/tree/0bdeaf11d91aa954ee0973ba06c9aba52ddb9971).

## Manifest and Bluetooth discovery

Use this discovery shape:

```json
{
  "bluetooth": [
    {
      "connectable": true,
      "local_name": "Y50*"
    }
  ],
  "config_flow": true,
  "dependencies": ["bluetooth_adapters"],
  "integration_type": "device",
  "iot_class": "local_push"
}
```

`local_name`, `service_uuid`, and `connectable` are supported manifest matcher
keys, alternatives in the matcher list are ORed, and all fields within one
matcher must match. A wildcard is allowed here because Home Assistant only
forbids patterns within the first three characters; `Y50*` has three literal
leading characters. The integration needs an outgoing connection, so explicitly
require `connectable: true`. These rules are defined in the current
[`manifest.json` Bluetooth documentation](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/creating_integration_manifest.md#bluetooth).

Do **not** require advertised service UUID `000018f0-0000-1000-8000-00805f9b34fb`
in the initial matcher. The evidence establishes that service after GATT
connection, not that every Y50 advertisement includes it. A separate
service-only matcher would also invite false discoveries because 0x18F0 is not
known to uniquely identify YPL printers. Validate service 0x18F0 and
characteristics 0x2AF0/0x2AF1 during the safe connection probe instead.

`bluetooth_adapters` is the correct dependency: Home Assistant says it ensures
remote adapters are available before the integration uses them. The same guide
warns against an independent scanner and against reusing a `BleakClient` across
connections ([Bluetooth best practices](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/bluetooth.md#best-practices-for-integration-authors)).

## Config flow, identity, and device registration

Implement `async_step_bluetooth(discovery_info: BluetoothServiceInfoBleak)` and
a `bluetooth_confirm` step:

1. Reject a non-connectable discovery defensively.
2. Normalize the discovered address with
   `homeassistant.helpers.device_registry.format_mac` and call
   `await self.async_set_unique_id(normalized_address)`, then
   `self._abort_if_unique_id_configured()`.
3. Keep the original discovered address in `CONF_ADDRESS`; set title
   placeholders from the advertised name, falling back to `YPL Printer
   (<address>)`.
4. Always show a confirmation step. Current guidance says a discovery step must
   not create an entry without user confirmation.
5. On confirmation, resolve the current connectable `BLEDevice`, establish a
   temporary managed connection, verify service and characteristic presence,
   and disconnect without sending protocol bytes. Return `cannot_connect` or
   `not_supported` on the form when appropriate.
6. Create one entry for the confirmed printer. Since this effort chose
   discovery-only setup, `async_step_user` should explain/abort with a localized
   discovery-only reason rather than accepting an address.

Bluetooth discovery requires a unique ID, and a discovered MAC address formatted
with `format_mac` is an accepted stable ID
([config-flow identity rules](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/core/integration/config_flow.md#unique-ids)).
Core's Specialized Turbo Bluetooth flow is a current executable example of the
same pattern: normalized address, duplicate abort, user confirmation, managed
connection probe, and disconnect
([source](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/homeassistant/components/specialized_turbo/config_flow.py),
[tests](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/tests/components/specialized_turbo/test_config_flow.py)).

Because the MVP may expose no entities, create a device-registry record explicitly
in `async_setup_entry`:

```python
device_registry.async_get(hass).async_get_or_create(
    config_entry_id=entry.entry_id,
    identifiers={(DOMAIN, normalized_address)},
    connections={(device_registry.CONNECTION_BLUETOOTH, normalized_address)},
    name=entry.title,
    manufacturer="FlashLabel/KNAON",
    model="Y50 family",
)
```

Store a typed runtime object on `entry.runtime_data`; it should contain the raw
address used by HA Bluetooth, display name, and one `asyncio.Lock`. Do not use a
coordinator: printing is an on-demand command and this prototype does not poll.

## Shared `BLEDevice` and connection ownership

At the start of every job, while holding that printer's lock, call:

```python
ble_device = bluetooth.async_ble_device_from_address(
    hass, runtime.address, connectable=True
)
```

This returns the device from the best reachable configured adapter and avoids a
second scanner. If it returns `None`, include the human-facing result of
`async_address_reachability_diagnostics(...,
BluetoothReachabilityIntent.CONNECTION)` in the translated error, but never
parse that text because its wording is not stable. Both APIs and those
constraints are documented in the current
[`Bluetooth API`](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/core/bluetooth/api.md#fetching-the-bleak-bledevice-from-the-address).

Open a new client per job using the connector version already provided by Core:

```python
client = await establish_connection(
    BleakClient,
    ble_device,
    runtime.name,
)
```

`establish_connection` performs bounded retries/backoff and turns exhausted
failures into typed `BleakNotFoundError`, `BleakConnectionError`,
`BleakAbortedError`, or `BleakOutOfConnectionSlotsError`; its 4.7.0 signature and
behavior are in the pinned
[`bleak-retry-connector` source](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/84bda39c71ea13d3d4fbab3fbe2a66e85f91b86e/src/bleak_retry_connector/__init__.py#L451-L659).
The latest 4.7.1 public API is compatible for this call, but the custom
integration should target Core's 4.7.0 pin. Do not list Bleak,
bleak-retry-connector, or Pillow in custom `requirements`: current custom
integration guidance says to list only packages not already required by Core,
and Core 2026.9.2 already pins all three
([manifest requirement guidance](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/creating_integration_manifest.md#custom-integration-requirements),
[`package_constraints.txt`](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/homeassistant/package_constraints.txt)).

## Notifications and cleanup

After connecting and verifying the profile, call `start_notify` for the full
128-bit form of 0x2AF0 **before the first write**. The notification callback must
be synchronous and cheap: copy the bytes into a parser-owned buffer/queue and
set an `asyncio.Event`; do parsing or waiting in the print coroutine. Notifications
may be fragmented or coalesced, so feed a byte stream to the YPL reply parser
rather than assuming one callback equals one frame.

Treat inability to subscribe or a missing characteristic as a pre-transmission
failure. Once subscribed, absence of an interpretable reply after the short
upstream-derived status timeout is not a failed physical print: return
`status: null` (or `"unknown"`) and log at debug level. A decoded printer error
should raise a translated `HomeAssistantError` if received before successful job
completion.

Always disconnect in `finally`; optionally call `stop_notify` first when the
client is still connected, but cleanup errors must not mask the primary print
error. No client or notification callback survives the job. This realizes the
chosen connect-per-job policy and follows the official warning not to reuse
clients.

## Service registration, targeting, and response

Register `ypl_printer.print` in `async_setup`, so it remains known even when an
entry is temporarily unavailable. This is an explicit Home Assistant quality
rule; handlers must validate that their selected entry exists and is loaded
([service setup rule](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/core/integration-quality-scale/rules/action-setup.md)).

The service description should expose a device target restricted to the
`ypl_printer` integration and one required multiline `text` field. At runtime:

1. Require exactly one targeted device for the MVP.
2. Resolve it through the device registry and select the one associated loaded
   `ypl_printer` config entry.
3. Raise `ServiceValidationError` for no target, multiple targets, a foreign
   target, missing/unloaded entry, or invalid text/raster input.
4. Raise a translated `HomeAssistantError` for Bluetooth, protocol, or printer
   execution failures.

Avoid `homeassistant.helpers.service.async_extract_config_entry_ids`: it is
marked for removal in Home Assistant 2026.10 in the exact 2026.9.2
[`service.py`](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/homeassistant/helpers/service.py#L419-L442).
Resolve the device registry target directly instead. Current guidance distinguishes
bad user input (`ServiceValidationError`) from execution failure
(`HomeAssistantError`)
([action exception rule](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/core/integration-quality-scale/rules/action-exceptions.md)).

Register with `supports_response=SupportsResponse.OPTIONAL`, because printing is
an action that can optionally return diagnostics. `ONLY` is reserved for
read-only calls. When `call.return_response` is true, return a JSON-serializable
dictionary such as:

```json
{
  "device": "Y50_B9BC_BLE",
  "raster_width": 400,
  "raster_height": 240,
  "encoded_bytes": 12345,
  "ble_chunks": 618,
  "status": null
}
```

When it is false, return `None`. Never encode failure as a response status;
raise an exception. These are the current service response requirements
([service response documentation](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/dev_101_services.md#response-data))
and Core enforces the OPTIONAL/ONLY distinction
([`SupportsResponse`](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/homeassistant/core.py#L2539-L2549)).

## Per-device serialization and retry boundary

Use `async with runtime.print_lock:` around the whole sequence from current
`BLEDevice` lookup through disconnect. Each config entry owns its own lock, so
two jobs to the same printer queue, while two printers can operate concurrently.
Do not use a domain-global lock.

Track whether any write completed:

```python
transmission_started = False
for chunk in chunks:
    await client.write_gatt_char(WRITE_UUID, chunk, response=False)
    transmission_started = True
    await asyncio.sleep(PACING_SECONDS)
```

The connector may retry connection establishment because no job bytes have been
sent. If lookup, connection, profile validation, or notification subscription
fails, the service may be called again safely. If any write completed and a
later write, disconnect, or status wait fails, do **not** reconnect and replay;
raise an explicit translated error stating that the print outcome is uncertain.
This application-level rule is required even though
bleak-retry-connector provides a general retry decorator: replaying a physical
print command is not idempotent.

## Setup, unload, and removal

`async_setup_entry` should create the runtime object and device record only; it
does not need to connect. With no entity platforms, `async_unload_entry` can
clear `entry.runtime_data`/domain bookkeeping and return `True`. Because clients
are job-scoped and disconnected in `finally`, unload normally has no live BLE
resource to close; an in-flight call retains its runtime reference and lock until
its own cleanup finishes.

If an entry is removed, call `bluetooth.async_rediscover_address` with its stored
address so the printer can immediately be offered again. This API is documented
for config-entry/device removal in the
[`Bluetooth API`](https://github.com/home-assistant/developers.home-assistant/blob/da3372bc7844c233c5ffa959b2ffda802f33b0bb/docs/core/bluetooth/api.md#triggering-rediscovery-of-devices).

## Recommended automated tests

Use `pytest-homeassistant-custom-component` plus mocked Bleak; no automated test
should need a Bluetooth adapter or printer.

- **Manifest:** validate `Y50*`, `connectable: true`, `config_flow`, and
  `bluetooth_adapters`; run `hassfest`/custom-component validation.
- **Config flow:** Bluetooth discovery shows confirmation; unique ID is the
  normalized address; duplicate discovery aborts; user-initiated setup is
  discovery-only; non-connectable, unreachable, missing-service, and
  missing-characteristic probes show the correct errors; every probe disconnects.
- **Setup/device registry:** a service-only config entry creates one device with
  its domain identifier and Bluetooth connection; setup does not connect.
- **Service targeting:** absent, multiple, foreign, removed, and unloaded device
  targets raise `ServiceValidationError`; the selected loaded entry receives the
  call.
- **Service responses:** registration is `SupportsResponse.OPTIONAL`; a normal
  call returns `None`; `return_response=True` returns only JSON-compatible
  diagnostics with exact byte/chunk counts and nullable decoded status.
- **BLE ordering:** resolve through `async_ble_device_from_address`, establish a
  fresh client, validate profile, subscribe to 0x2AF0, write 20-byte-or-smaller
  chunks in order with `response=False`, then stop/disconnect. Assert no separate
  scanner construction.
- **Notifications:** fragmented and coalesced callbacks feed the parser; decoded
  status is returned; timeout/no interpretable response still succeeds;
  pre-write subscribe failure sends no bytes.
- **Serialization:** two calls for one entry never overlap; calls for two entries
  can overlap.
- **Retry safety:** connector connection retries can succeed before the first
  write; a failure after the first completed write produces `outcome_uncertain`
  and creates no second client/job replay.
- **Cleanup:** cancellation and every exception path disconnect; cleanup failure
  does not replace the original exception.
- **Protocol boundary:** malformed/non-400-by-240 raster data is rejected before
  BLE lookup. Protocol byte-vector tests remain in the protocol research/work,
  but the service test should assert the safety validation occurs before any I/O.

The current Core LED BLE tests show the standard discovery-flow test shape and
mocked connection errors
([tests](https://github.com/home-assistant/core/blob/33c3e0cca60e73a8c4970ee677d75b8bc6464cdf/tests/components/led_ble/test_config_flow.py)).
Specialized Turbo explicitly tests that its client factory delegates to
`establish_connection`, making it the stronger model for managed-client tests.

## hass-niimbot: separated architectural reference

Inspected reference: `eigger/hass-niimbot` commit
[`f2bed90599dfe4459a80079fd3430d93d26caf40`](https://github.com/eigger/hass-niimbot/tree/f2bed90599dfe4459a80079fd3430d93d26caf40).
It is useful evidence that a printer integration can use manifest Bluetooth
discovery, `async_ble_device_from_address`, device-targeted printing, Pillow
rendering, notifications, and OPTIONAL service responses
([manifest](https://github.com/eigger/hass-niimbot/blob/f2bed90599dfe4459a80079fd3430d93d26caf40/custom_components/niimbot/manifest.json),
[`__init__.py`](https://github.com/eigger/hass-niimbot/blob/f2bed90599dfe4459a80079fd3430d93d26caf40/custom_components/niimbot/__init__.py)).

Do not copy its protocol or its service lifecycle. NIIMBOT framing is unrelated
to YPL. At this pinned commit it registers `niimbot.print` inside every
`async_setup_entry`; that conflicts with current Home Assistant guidance to
register integration services once in `async_setup` and is unsafe as a
multiple-entry template. It also maintains polling/coordinator and entity
machinery that the first YPL print-only prototype does not need.

## Remaining uncertainties for the hardware session

- The confirmed GATT profile does not prove whether the real Y50 advertises
  service UUID 0x18F0. The recommended name-only manifest deliberately avoids
  depending on that unknown.
- Linux/HAOS exposes a stable Bluetooth address, but remote adapters/proxies and
  other platforms can represent addresses differently. The prototype's HAOS
  scope makes normalized discovery address an appropriate identity; obtain and
  migrate to a printer serial only if a verified, read-only YPL command later
  exposes one.
- It remains a hardware fact whether `write_gatt_char(..., response=False)` works
  reliably at upstream YPL's exact pacing through the user's specific HA
  adapter/proxy path. Do not tune chunk size or pacing in this architecture
  work; preserve the protocol research values and change them only from physical
  evidence.
- Notification timing and the exact meaning of every status are protocol facts,
  not Home Assistant conventions. Unknown/timeout status must therefore remain
  non-fatal after a completely transmitted first-print job.
