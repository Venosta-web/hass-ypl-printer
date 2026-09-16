# Growspace label-printing compatibility boundary

Research date: 2026-09-16

## Source baseline

This note treats the Growspace Manager repository as the source of truth. GitHub reported
`main` at commit [`af80728336ddfc34f9ced38b047b7b74a7adaa94`](https://github.com/Venosta-web/growspace_manager/commit/af80728336ddfc34f9ced38b047b7b74a7adaa94)
and the active `prerelease` branch at commit
[`baec20b3993c10baeabe929e30f46fa28c57ecce`](https://github.com/Venosta-web/growspace_manager/commit/baec20b3993c10baeabe929e30f46fa28c57ecce).
The print handler and its focused tests are unchanged between those revisions, so the stable
commit is used for source/test permalinks below; the `prerelease` commit is used where the
bundled current card fixture supplies additional UI evidence.

## Answer

### Current call boundary

`growspace_manager.print_label` accepts either a `plant_id` or loose strain metadata, plus
optional `device_id`, `preview`, `base_url`, field visibility, density, QR target, and label
size. The schema does not require either identity field, but the handler rejects calls that
resolve to no strain name. A `plant_id` is resolved inside Growspace; otherwise the caller's
strain fields are enriched from the strain library
([schema](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/schemas.py#L460-L477),
[handler input resolution](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L479-L532)).

After building the label description, the handler calls exactly:

```python
await hass.services.async_call(
    "niimbot",
    "print",
    service_data,
    blocking=True,
    return_response=True,
)
```

The `service_data` object always contains `width`, `height`, `rotate`, `density`, `payload`,
and `preview`; it contains `device_id` only when the caller supplied a truthy value. Rotation
is fixed at `0`. Density maps `low`, `normal`, and `high` to `3`, `5`, and `8`, defaulting to
`5`
([dispatch source](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L642-L695)).

### Payload and image-spec shape

Growspace does **not** pass a finished bitmap. It owns the semantic label composition and
passes a NIIMBOT-specific, ordered list of drawing elements:

- `new_multiline` for an uppercase strain header, using `ppb.ttf`, and for phenotype/breeder/
  lineage, using `rbm.ttf`; both include pixel coordinates, bounding dimensions, font size,
  and `fit: true`.
- `rectangle` for the divider.
- optional `dlimg` for a breeder logo, with URL/data URI and `xsize`/`ysize`.
- optional `qrcode` for a plant link or Home Assistant deep link.
- `text` for the date, also naming `rbm.ttf`.

The exact base coordinates and element shapes live in
[the payload construction](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L534-L641),
and focused tests assert the element types and selected values, logo dimensions, and QR box
size
([payload test](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/services/test_niimbot_printing.py#L92-L147)).

Large data-URI logos are preprocessed by Growspace in Home Assistant's executor: they are
thumbnailed to at most 100x100, re-encoded as PNG, and converted to 1-bit if still too large.
That is upstream content hygiene, not full-label rasterization
([logo preprocessing](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L49-L113),
[test](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/services/test_niimbot_printing.py#L198-L249)).

Therefore renderer ownership is split: Growspace chooses content, layout, coordinates, font
names, QR data, and logo preparation; the printer integration is expected to interpret those
elements and produce the final printer-ready image. The YPL prototype can own rendering for
its initial text-only API, but its renderer should stay separable from YPL encoding and BLE
transport so a later Growspace adapter can supply a rendered raster or a translated layout.

### The 400x240 contract

The label layout's reference canvas is 400x240 dots. `label_size` defaults to `50x30`, which
maps to `(400, 240)`; other sizes scale every known x- and y-coordinate/dimension from that
reference canvas
([size mapping and scaling](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L645-L665)).
Tests explicitly lock both the omitted-size default and explicit `50x30` case to 400x240 and
assert they produce identical payloads
([size tests](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/services/test_print_label_size_scaling.py#L48-L64),
[compatibility test](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/services/test_print_label_size_scaling.py#L114-L142)).

For the first YPL prototype, fixed 50x30 media at exactly 400x240 dots is therefore the one
valuable pixel-level compatibility seam. Supporting Growspace's other size identifiers is not
needed for this prototype.

### Target/device behavior

The handler treats `device_id` as an opaque optional string and forwards it unchanged; if it
is absent, the field is omitted and downstream default-device behavior decides the target
([source](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L667-L682),
[forwarding test](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/services/test_niimbot_printing.py#L112-L129)).

Its concrete meaning is inconsistent in current first-party evidence:

- service metadata calls it a Bluetooth address
  ([services metadata](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services.yaml#L1749-L1753));
- tests use the opaque value `printer_1`;
- the bundled card discovers `image.*_last_label_made` entities and passes that entity ID as
  `device_id` when printing, while its preview request omits `device_id`
  ([card behavior](https://github.com/Venosta-web/growspace_manager/blob/baec20b3993c10baeabe929e30f46fa28c57ecce/tests/fixtures/lovelace/growspace-manager-card.js#L27220-L27310)).

The YPL prototype should consequently preserve **targetability**, not copy an unproven
identifier convention. One config entry per discovered printer plus per-printer serialization
is sufficient now. A future Growspace adapter can translate its selected HA entity/device to
the YPL config entry; the prototype should not claim that `device_id` is a MAC address or an
entity ID without an explicit later decision.

### Response and error expectations

Growspace registers `print_label` with `SupportsResponse.OPTIONAL`; internally it always asks
`niimbot.print` for a response and returns that object unchanged
([service definition](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L735-L748),
[return path](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L679-L695)).
A test locks this pass-through behavior with `{"status": "success"}`
([response test](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/integration/test_print_label_fix.py#L48-L83)).
The current WebSocket wrapper, however, invokes the Growspace service with only
`blocking=True` and discards the return, so the card principally depends on completion versus
exception rather than a particular response body
([WebSocket handler](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/websocket/plant.py#L685-L693)).

Missing plants and missing strain identity are `HomeAssistantError`s. Downstream
`AttributeError`, `KeyError`, `ValueError`, `ServiceValidationError`, and `GrowspaceError` are
wrapped as `HomeAssistantError("Failed to print Niimbot label: ...")`; other exception types
are not caught by this handler and propagate. Tests explicitly cover missing plants, missing
identity, and a wrapped downstream `ValueError`
([error source](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L497-L519),
[downstream wrapping](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/custom_components/growspace_manager/services/strain_library.py#L679-L691),
[tests](https://github.com/Venosta-web/growspace_manager/blob/af80728336ddfc34f9ced38b047b7b74a7adaa94/tests/services/test_niimbot_printing.py#L150-L194)).

The YPL service should therefore support an optional structured response and use Home
Assistant service exceptions for validation, unavailable printer, connection, encoding, and
uncertain-transmission failures. Growspace does not require a specific response schema today,
so the prototype's planned diagnostics (`device`, raster dimensions, encoded bytes, BLE chunk
count, decoded status) are compatible.

## Minimal seams to preserve now

1. **Native canvas:** render exactly 400x240 dots for the fixed 50x30 profile.
2. **Replaceable renderer boundary:** keep text/layout-to-raster separate from raster-to-YPL
   encoding and BLE I/O. Do not adopt the NIIMBOT element dialect as the YPL protocol API.
3. **Optional printer targeting:** architect one independently serialized target per config
   entry, while deferring the external selector's exact identifier semantics.
4. **Blocking call semantics:** return only after the print attempt has reached a known result
   or an explicitly uncertain post-transmission failure.
5. **Optional structured response:** return diagnostics without making a particular NIIMBOT
   response shape part of the contract.
6. **Home Assistant-native failures:** surface actionable service exceptions; never turn a
   failed/uncertain job into a nominal success response.

No Growspace code change, `growspace_manager.print_label` adapter, NIIMBOT-payload parser,
preview entity, QR/logo support, density mapping, alternate media sizes, or card change is
needed to prove the first YPL print. Those are later compatibility work, not prototype
requirements.

## Remaining uncertainties

- The accepted identity type and omission behavior of hass-niimbot's `device_id` are not
  established by Growspace's own source because its metadata, tests, and card disagree.
- Growspace has no contract test for the exact downstream response keys, only pass-through of
  a mocked dictionary.
- The current card preview flow appears coupled to hass-niimbot's image entity and deliberately
  omits `device_id`; reproducing preview behavior for YPL would require a separate decision.
- The element payload names fonts that are expected to be understood downstream. This research
  did not establish font metric equivalence between hass-niimbot and the YPL prototype's
  bundled font, so pixel-identical label composition is not implied.

