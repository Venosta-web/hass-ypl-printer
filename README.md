[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Quality Scale](https://img.shields.io/badge/Quality%20Scale-Gold-gold.svg?style=for-the-badge)](https://developers.home-assistant.io/docs/integration-quality-scale/)
[![Version](https://img.shields.io/badge/Version-0.1.0-blue.svg?style=for-the-badge)](https://github.com/Venosta-web/hass-ypl-printer/releases)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

# hass-ypl-printer

Home Assistant custom integration for YPL thermal label printers. It is a
first-print prototype; see [`docs/spec/first-print.md`](docs/spec/first-print.md).

## Targeted Home Assistant version

Home Assistant Core **2026.9.2**, tested with
`pytest-homeassistant-custom-component==0.13.365` (Python 3.14).

## Development

```bash
pip install -r requirements_test.txt
pytest
```

CI runs `pytest` and `hassfest` on every push.
