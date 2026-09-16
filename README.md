[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/Venosta-web/hass-ypl-printer?include_prereleases&style=for-the-badge)](https://github.com/Venosta-web/hass-ypl-printer/releases)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

# YPL Printer for Home Assistant

A Home Assistant custom integration for YPL thermal label printers, such as the
FlashLabel/KNAON **Y50**. It prints 50 × 30 mm text labels over Bluetooth,
directly from Home Assistant.

> [!NOTE]
> This is an early prototype. It supports one label size (50 × 30 mm) and
> plain text only. See [`docs/spec/first-print.md`](docs/spec/first-print.md)
> for the full scope.

## Requirements

- Home Assistant **2026.9.0** or newer (tested on 2026.9.2).
- The [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/)
  set up, with a **connectable** Bluetooth adapter within range of the printer.
  A local adapter on the Home Assistant host is the tested setup; ESPHome
  Bluetooth proxies are untested.
- [HACS](https://hacs.xyz/docs/use/) installed (for the recommended install
  method).

## Installation

### Option 1: HACS (recommended)

Click this button to open the repository in HACS on your Home Assistant
instance:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Venosta-web&repository=hass-ypl-printer&category=integration)

Or add it by hand:

1. In Home Assistant, open **HACS**.
2. Open the **⋮** menu in the top-right corner and choose **Custom repositories**.
3. Enter `https://github.com/Venosta-web/hass-ypl-printer` as the repository,
   choose **Integration** as the type, and select **Add**.
4. Search HACS for **YPL Printer**, open it, and select **Download**.
5. Restart Home Assistant (**Settings → System → ⏻ → Restart Home Assistant**).

HACS notifies you when a new version is released; update from the same page.

### Option 2: Manual

1. Download the source code of the
   [latest release](https://github.com/Venosta-web/hass-ypl-printer/releases).
2. Copy the `custom_components/ypl_printer` folder into the
   `custom_components` folder of your Home Assistant configuration directory
   (the folder that contains `configuration.yaml`). Create
   `custom_components` if it does not exist. The result should be
   `<config>/custom_components/ypl_printer/manifest.json`.
3. Restart Home Assistant.

## Setup

Printers are added **automatically through Bluetooth discovery**; there is no
manual setup form.

1. Switch the printer on and place it near your Bluetooth adapter.
2. Go to **Settings → Devices & services**. The printer appears under
   **Discovered** (its name starts with `Y50`).
3. Select **Add**, then **Submit**. Home Assistant briefly connects to the
   printer to check that it offers the YPL Bluetooth profile. Nothing is
   printed.

If the printer does not show up, make sure it is on, not connected to a phone
app, and in range of a connectable adapter, then wait a minute.

## Usage

The integration adds one action, **YPL Printer: Print label**
(`ypl_printer.print`). It prints one 50 × 30 mm label and waits until the job
finishes.

| Field | Description |
| --- | --- |
| `device_id` | The printer device to print on. |
| `text` | The label text. Basic Latin characters (`U+0020`–`U+007E`) only. Each line break starts a new line; text is never wrapped or shortened, and the font shrinks to fit. |

Try it from **Developer tools → Actions**, or use it in an automation or
script:

```yaml
action: ypl_printer.print
data:
  device_id: 0123456789abcdef0123456789abcdef
  text: |
    Tomato
    Sown 2026-09-16
```

Tip: pick the printer in the UI editor, then switch to YAML to see its
`device_id`.

### When a print fails

- If the action fails **before** sending anything (printer busy, out of range,
  not ready, text too long…), nothing was printed and it is safe to try again.
- If it fails **while** sending (the message says the label "may or may not
  have printed"), check the printer before printing again. The integration
  never resends a label automatically, to avoid duplicates.

## Troubleshooting

To get debug logs, add this to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.ypl_printer: debug
```

Please include those logs when you
[open an issue](https://github.com/Venosta-web/hass-ypl-printer/issues).

## Uninstall

1. Go to **Settings → Devices & services → YPL Printer** and delete each
   printer entry.
2. In HACS, open **YPL Printer**, open the **⋮** menu, and choose **Remove**
   (or delete `custom_components/ypl_printer` for a manual install).
3. Restart Home Assistant.

## Development

Tested with `pytest-homeassistant-custom-component==0.13.365` (Python 3.14).

```bash
pip install -r requirements_test.txt
pytest
```

CI runs `pytest`, `hassfest`, and HACS validation on every push.

### Releasing

1. Bump `version` in `custom_components/ypl_printer/manifest.json`.
2. Publish a GitHub release whose tag is that version (for example `v0.1.0`).
   HACS only offers published releases; the release workflow checks that the
   tag matches the manifest.

## Licence

MIT; see [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
