"""Config flow for the YPL Printer integration.

Printers are added only from Home Assistant Bluetooth discovery (spec §4.1).
"""

from __future__ import annotations

import logging
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import format_mac

from .ble import has_ypl_profile
from .const import DEFAULT_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ProfileCheckError(Exception):
    """The profile check failed; ``reason`` is the config-flow abort key."""

    def __init__(self, reason: str) -> None:
        """Initialise with the abort reason."""
        super().__init__(reason)
        self.reason = reason


class YplPrinterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for YPL Printer."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._address: str | None = None
        self._name: str = DEFAULT_NAME

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refuse manual setup; printers are added from discovery only."""
        return self.async_abort(reason="discovery_only")

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth discovery."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()

        self._address = discovery_info.address
        self._name = discovery_info.name or DEFAULT_NAME
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered printer, then check its profile."""
        assert self._address is not None
        if user_input is None:
            self._set_confirm_only()
            return self.async_show_form(
                step_id="bluetooth_confirm",
                description_placeholders={"name": self._name},
            )

        try:
            await self._async_check_profile(self._address)
        except ProfileCheckError as err:
            return self.async_abort(reason=err.reason)

        return self.async_create_entry(
            title=self._name, data={CONF_ADDRESS: self._address}
        )

    async def _async_check_profile(self, address: str) -> None:
        """Connect, inspect the discovered services and always disconnect.

        No lock is held, nothing is subscribed to and nothing is written.
        """
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, address, connectable=True
        )
        if ble_device is None:
            _LOGGER.debug("%s (%s): no connectable device", self._name, address)
            raise ProfileCheckError("cannot_connect")

        try:
            client = await establish_connection(BleakClient, ble_device, self._name)
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s (%s): connection failed: %r", self._name, address, err)
            raise ProfileCheckError("cannot_connect") from err

        try:
            if not has_ypl_profile(client.services):
                _LOGGER.debug("%s (%s): YPL profile missing", self._name, address)
                raise ProfileCheckError("not_supported")
        finally:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError) as err:
                _LOGGER.warning(
                    "%s (%s): disconnect after profile check failed: %r",
                    self._name,
                    address,
                    err,
                )
