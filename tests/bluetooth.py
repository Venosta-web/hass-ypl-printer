"""Bluetooth fakes for the YPL Printer tests.

Advertisements go through Home Assistant's real Bluetooth manager (set up by
the ``enable_bluetooth`` fixture), the same way Core's own Bluetooth test
helpers inject them. GATT clients are scripted fakes; no test touches a real
adapter.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection
from habluetooth import BluetoothServiceInfoBleak

from homeassistant.components.bluetooth import SOURCE_LOCAL
from homeassistant.components.bluetooth.api import _get_manager
from homeassistant.core import HomeAssistant

from custom_components.ypl_printer.const import (
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
)

PRINTER_ADDRESS = "AA:BB:CC:DD:EE:FF"
PRINTER_UNIQUE_ID = "aa:bb:cc:dd:ee:ff"
PRINTER_NAME = "Y50-1234"

# Patch targets for the config flow's connection.
ESTABLISH_CONNECTION = "custom_components.ypl_printer.config_flow.establish_connection"


def service_info(
    name: str | None = PRINTER_NAME,
    address: str = PRINTER_ADDRESS,
    connectable: bool = True,
) -> BluetoothServiceInfoBleak:
    """Return a printer advertisement as Home Assistant's scanner reports it."""
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name or "",
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source=SOURCE_LOCAL,
        device=BLEDevice(address, name, {}),
        advertisement=advertisement,
        connectable=connectable,
        time=time.monotonic(),
        tx_power=-127,
    )


def inject_advertisement(hass: HomeAssistant, info: BluetoothServiceInfoBleak) -> None:
    """Feed an advertisement into Home Assistant's Bluetooth manager."""
    _get_manager(hass).scanner_adv_received(info)


YPL_PROFILE = (SERVICE_UUID, (NOTIFY_CHARACTERISTIC_UUID, WRITE_CHARACTERISTIC_UUID))
OTHER_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"


def gatt_services(
    *layout: tuple[str, tuple[str, ...]],
) -> BleakGATTServiceCollection:
    """Return discovered GATT services as ``(service, characteristics)`` pairs.

    Without arguments this is the YPL profile.
    """
    services = BleakGATTServiceCollection()
    handle = 0
    for service_uuid, characteristic_uuids in layout or (YPL_PROFILE,):
        handle += 1
        service = BleakGATTService(None, handle, service_uuid)
        services.add_service(service)
        for uuid in characteristic_uuids:
            handle += 1
            services.add_characteristic(
                BleakGATTCharacteristic(None, handle, uuid, [], lambda: 20, service)
            )
    return services


def fake_client(services: BleakGATTServiceCollection | None = None) -> MagicMock:
    """Return a connected fake ``BleakClient`` exposing ``services``."""
    client = MagicMock()
    client.services = gatt_services() if services is None else services
    client.disconnect = AsyncMock(return_value=True)
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    return client
