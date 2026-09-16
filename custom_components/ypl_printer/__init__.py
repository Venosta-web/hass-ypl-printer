"""The YPL Printer integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class YplPrinterData:
    """Per-entry runtime data (spec §4.2).

    Holds no ``BLEDevice`` or ``BleakClient``: each print job resolves and
    connects afresh while holding ``lock``.
    """

    address: str
    name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


type YplPrinterConfigEntry = ConfigEntry[YplPrinterData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the YPL Printer integration and its print action."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: YplPrinterConfigEntry) -> bool:
    """Set up a YPL printer from a config entry, without connecting to it."""
    address: str = entry.data[CONF_ADDRESS]
    normalized_address = dr.format_mac(address)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, normalized_address)},
        connections={(dr.CONNECTION_BLUETOOTH, normalized_address)},
        name=entry.title,
    )
    entry.runtime_data = YplPrinterData(address=address, name=entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: YplPrinterConfigEntry) -> bool:
    """Unload a config entry.

    Home Assistant drops ``entry.runtime_data`` once this returns; a running
    job keeps its own reference and finishes its cleanup. The print service
    belongs to the integration and stays registered.
    """
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Let Home Assistant offer the removed printer for discovery again."""
    bluetooth.async_rediscover_address(hass, entry.data[CONF_ADDRESS])
