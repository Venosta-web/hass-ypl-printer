"""Bluetooth helpers shared by the config flow and print jobs."""

from __future__ import annotations

from bleak.backends.service import BleakGATTServiceCollection

from .const import NOTIFY_CHARACTERISTIC_UUID, SERVICE_UUID, WRITE_CHARACTERISTIC_UUID


def has_ypl_profile(services: BleakGATTServiceCollection) -> bool:
    """Return whether the discovered services contain the YPL profile.

    Only presence is checked: service 0x18F0 with characteristics 0x2AF0
    (notify) and 0x2AF1 (write). Nothing is subscribed to or written.
    """
    service = services.get_service(SERVICE_UUID)
    if service is None:
        return False
    return all(
        service.get_characteristic(uuid) is not None
        for uuid in (NOTIFY_CHARACTERISTIC_UUID, WRITE_CHARACTERISTIC_UUID)
    )
