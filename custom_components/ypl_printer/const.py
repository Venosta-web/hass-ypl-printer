"""Constants for the YPL Printer integration."""

DOMAIN = "ypl_printer"

# Title of a printer whose advertisement carries no name (spec §4.1).
DEFAULT_NAME = "YPL printer"

# The YPL BLE profile (spec §4.1, docs/protocol.md).
SERVICE_UUID = "000018f0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHARACTERISTIC_UUID = "00002af0-0000-1000-8000-00805f9b34fb"
WRITE_CHARACTERISTIC_UUID = "00002af1-0000-1000-8000-00805f9b34fb"
