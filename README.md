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
