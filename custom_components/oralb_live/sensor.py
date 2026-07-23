"""Sensors for Oral-B Live.

Core entities mirror the official oralb integration so existing dashboards
keep working, with additional session, display, battery and brush diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceInfo,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE
from .coordinator import OralBLiveCoordinator


@dataclass(frozen=True, kw_only=True)
class OralBSensorDescription(SensorEntityDescription):
    """Describes an Oral-B Live sensor."""

    data_key: str = ""
    restore: bool = False


SENSORS: tuple[OralBSensorDescription, ...] = (
    OralBSensorDescription(
        key="toothbrush_state",
        translation_key="toothbrush_state",
        name=None,  # main entity carries the device name
        data_key="state",
    ),
    OralBSensorDescription(
        key="time",
        translation_key="time",
        name="Time",
        data_key="time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    OralBSensorDescription(
        key="pressure",
        translation_key="pressure",
        name="Pressure",
        data_key="pressure",
    ),
    OralBSensorDescription(
        key="mode",
        translation_key="mode",
        name="Mode",
        data_key="mode",
    ),
    OralBSensorDescription(
        key="sector",
        translation_key="sector",
        name="Sector",
        data_key="sector",
    ),
    OralBSensorDescription(
        key="sector_timer",
        translation_key="sector_timer",
        name="Sector timer",
        data_key="sector_timer",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    OralBSensorDescription(
        key="number_of_sectors",
        translation_key="number_of_sectors",
        name="Number of sectors",
        data_key="number_of_sectors",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="target_duration",
        translation_key="target_duration",
        name="Target duration",
        data_key="target_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="smiley",
        translation_key="smiley",
        name="Smiley",
        data_key="smiley",
    ),
    OralBSensorDescription(
        key="last_session",
        translation_key="last_session",
        name="Last session",
        data_key="last_session_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        restore=True,
    ),
    OralBSensorDescription(
        key="last_session_duration",
        translation_key="last_session_duration",
        name="Last session duration",
        data_key="last_session_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        restore=True,
    ),
    OralBSensorDescription(
        key="sessions_today",
        translation_key="sessions_today",
        name="Sessions today",
        data_key="sessions_today",
        state_class=SensorStateClass.TOTAL,
        restore=True,
    ),
    OralBSensorDescription(
        key="battery",
        translation_key="battery",
        name="Battery",
        data_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    OralBSensorDescription(
        key="battery_time_remaining",
        translation_key="battery_time_remaining",
        name="Battery time remaining",
        data_key="battery_time_remaining",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        name="Battery voltage",
        data_key="battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        name="Battery current",
        data_key="battery_current",
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        name="Battery temperature",
        data_key="battery_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="refill_days",
        translation_key="refill_days",
        name="Brush head remaining",
        data_key="refill_days",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="refill_brushing_time",
        translation_key="refill_brushing_time",
        name="Brush head brushing time remaining",
        data_key="refill_brushing_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: OralBLiveCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OralBLiveSensor(coordinator, description) for description in SENSORS
    )


class OralBLiveSensor(SensorEntity, RestoreEntity):
    """A single Oral-B Live sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OralBLiveCoordinator,
        description: OralBSensorDescription,
    ) -> None:
        self.coordinator = coordinator
        self.entity_description: OralBSensorDescription = description
        self._attr_unique_id = f"{coordinator.address}-{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            name=coordinator.name,
            manufacturer="Oral-B",
            model=coordinator.data.get("model_name"),
            sw_version=(
                f"0x{coordinator.data['firmware_revision']:02x}"
                if coordinator.data.get("firmware_revision") is not None
                else None
            ),
            hw_version=(
                f"BLE protocol {coordinator.data['protocol_version']}"
                if coordinator.data.get("protocol_version") is not None
                else None
            ),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_description.restore:
            # Session results must survive restarts; the brush will not
            # replay them.
            if (
                last := await self.async_get_last_state()
            ) is not None and last.state not in (
                None,
                "unknown",
                "unavailable",
            ):
                if self.entity_description.device_class is SensorDeviceClass.TIMESTAMP:
                    self._attr_native_value = dt_util.parse_datetime(last.state)
                elif (
                    self.entity_description.device_class is SensorDeviceClass.DURATION
                    or self.entity_description.key == "sessions_today"
                ):
                    self._attr_native_value = int(float(last.state))
                else:
                    self._attr_native_value = last.state
                # Restore into the shared coordinator too. This lets a later
                # ff29 read recognize and refine a passively recorded session
                # after an integration reload, instead of counting it twice.
                if self.coordinator.data.get(self.entity_description.data_key) is None:
                    self.coordinator.data[self.entity_description.data_key] = (
                        self._attr_native_value
                    )
                if self.entity_description.key == "last_session":
                    self._attr_extra_state_attributes = dict(last.attributes or {})
                    restored_attributes = {
                        "last_session_duration": last.attributes.get(
                            "duration_seconds"
                        ),
                        "last_session_mode": last.attributes.get("mode"),
                        "last_session_sectors": last.attributes.get(
                            "quadrants_covered"
                        ),
                        "last_session_high_pressure": last.attributes.get(
                            "high_pressure_events"
                        ),
                    }
                    for key, value in restored_attributes.items():
                        if value is not None and self.coordinator.data.get(key) is None:
                            self.coordinator.data[key] = value
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_UPDATE}_{self.coordinator.address}",
                self._handle_update,
            )
        )
        self._handle_update(self.coordinator.data)

    @callback
    def _handle_update(self, data: dict[str, Any]) -> None:
        value = data.get(self.entity_description.data_key)
        # Restored session values must not be wiped by a fresh coordinator
        # that has not seen a session yet.
        if value is None and self.entity_description.restore:
            self.async_write_ha_state()
            return
        self._attr_native_value = value
        if self.entity_description.key == "last_session":
            self._attr_extra_state_attributes = {
                "duration_seconds": data.get("last_session_duration"),
                "mode": data.get("last_session_mode"),
                "quadrants_covered": data.get("last_session_sectors"),
                "high_pressure_events": data.get("last_session_high_pressure"),
            }
        if self.entity_description.key == "toothbrush_state":
            self._attr_extra_state_attributes = {
                "live_connection": data.get("live"),
                "connection_mode": data.get("connection_mode"),
                "rssi": data.get("rssi"),
                "state_raw": data.get("state_raw"),
                "mode_raw": data.get("mode_raw"),
                "model": data.get("model_name"),
                "model_id": data.get("model_id"),
                "protocol_version": data.get("protocol_version"),
                "firmware_revision": data.get("firmware_revision"),
            }
        elif self.entity_description.key == "mode":
            self._attr_extra_state_attributes = {
                "mode_raw": data.get("mode_raw"),
                "available_modes": data.get("available_modes"),
                "available_modes_raw": data.get("available_modes_raw"),
            }
        elif self.entity_description.key in (
            "number_of_sectors",
            "target_duration",
        ):
            self._attr_extra_state_attributes = {
                "sector_times_seconds": data.get("sector_times"),
                "target_duration_seconds": data.get("target_duration"),
            }
        elif self.entity_description.key == "sector":
            self._attr_extra_state_attributes = {
                "sector_raw": data.get("sector_raw"),
            }
        elif self.entity_description.key == "smiley":
            self._attr_extra_state_attributes = {
                "smiley_raw": data.get("smiley_raw"),
            }
        elif self.entity_description.key in (
            "refill_days",
            "refill_brushing_time",
        ):
            self._attr_extra_state_attributes = {
                "refill_state": data.get("refill_state"),
                "refill_state_raw": data.get("refill_state_raw"),
            }
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if self.entity_description.restore:
            # Session history stays readable even when the brush is away.
            return True
        return self.coordinator.available
