"""Config flow for Oral-B Live."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN, ORALB_MANUFACTURER_ID


def _is_toothbrush(service_info: BluetoothServiceInfoBleak) -> bool:
    payload = service_info.manufacturer_data.get(ORALB_MANUFACTURER_ID)
    # Toothbrush state adverts carry an 11-byte payload; the iO Sense
    # charger advertises with the same vendor id but a different payload.
    return payload is not None and len(payload) == 11


class OralBLiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Oral-B Live."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery."""
        if not _is_toothbrush(discovery_info):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name or self._discovery_info.address,
                data={CONF_ADDRESS: self._discovery_info.address},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup: pick from currently visible brushes."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            service_info = self._discovered[address]
            return self.async_create_entry(
                title=service_info.name or address,
                data={CONF_ADDRESS: address},
            )

        current = self._async_current_ids(include_ignore=False)
        for service_info in async_discovered_service_info(
            self.hass, connectable=False
        ):
            if (
                service_info.address not in current
                and _is_toothbrush(service_info)
            ):
                self._discovered[service_info.address] = service_info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{info.name or 'Oral-B'} ({address})"
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

