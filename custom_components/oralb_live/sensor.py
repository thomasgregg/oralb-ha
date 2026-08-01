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
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
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

from .const import (
    DOMAIN,
    SIGNAL_CHARGER_DISCOVERED,
    SIGNAL_CHARGER_UPDATE,
    SIGNAL_UPDATE,
    brush_device_name,
)
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


CHARGER_SENSORS: tuple[OralBSensorDescription, ...] = (
    OralBSensorDescription(
        key="charger_state",
        translation_key="charger_state",
        name=None,
        data_key="state",
    ),
    OralBSensorDescription(
        key="charger_session_status",
        translation_key="charger_session_status",
        name="Session status",
        data_key="session_status",
    ),
    OralBSensorDescription(
        key="charger_brush_status",
        translation_key="charger_brush_status",
        name="Brush status",
        data_key="brush_status",
    ),
    OralBSensorDescription(
        key="charger_wifi_status",
        translation_key="charger_wifi_status",
        name="Wi-Fi status",
        data_key="wifi_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_cloud_status",
        translation_key="charger_cloud_status",
        name="Cloud connection",
        data_key="cloud_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_internet_type",
        translation_key="charger_internet_type",
        name="Internet type",
        data_key="internet_type",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_wifi_rssi",
        translation_key="charger_wifi_rssi",
        name="Wi-Fi signal",
        data_key="wifi_rssi",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_clock",
        translation_key="charger_clock",
        name="Displayed time",
        data_key="clock_text",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_clock_brightness",
        translation_key="charger_clock_brightness",
        name="Clock brightness",
        data_key="clock_brightness",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_clock_mode",
        translation_key="charger_clock_mode",
        name="Clock format",
        data_key="clock_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_date_show_mode",
        translation_key="charger_date_show_mode",
        name="Date display format",
        data_key="date_show_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_timezone",
        translation_key="charger_timezone",
        name="Timezone",
        data_key="timezone",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_night_light_mode",
        translation_key="charger_night_light_mode",
        name="Night-light mode",
        data_key="night_light_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_ring_color",
        translation_key="charger_ring_color",
        name="Ring color",
        data_key="ring_color",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OralBSensorDescription(
        key="charger_uptime",
        translation_key="charger_uptime",
        name="Uptime",
        data_key="uptime",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="charger_auto_update",
        translation_key="charger_auto_update",
        name="Automatic updates",
        data_key="auto_update",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="charger_touchpad_status",
        translation_key="charger_touchpad_status",
        name="Touchpad status",
        data_key="touchpad_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    OralBSensorDescription(
        key="charger_brush_connection_policy",
        translation_key="charger_brush_connection_policy",
        name="Brush connection policy",
        data_key="brush_connection_policy",
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
    charger_added = False

    @callback
    def _async_add_charger() -> None:
        nonlocal charger_added
        if charger_added or not coordinator.charger or not coordinator.charger.address:
            return
        charger_added = True
        async_add_entities(
            IOSenseSensor(coordinator, description) for description in CHARGER_SENSORS
        )

    _async_add_charger()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{SIGNAL_CHARGER_DISCOVERED}_{coordinator.address}",
            _async_add_charger,
        )
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
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            name=brush_device_name(
                coordinator.address, coordinator.data.get("model_name")
            ),
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
        # Session results must survive restarts; the brush will not replay them.
        if (
            self.entity_description.restore
            and (last := await self.async_get_last_state()) is not None
            and last.state
            not in (
                None,
                "unknown",
                "unavailable",
            )
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
                    "last_session_duration": last.attributes.get("duration_seconds"),
                    "last_session_mode": last.attributes.get("mode"),
                    "last_session_sectors": last.attributes.get("quadrants_covered"),
                    "last_session_high_pressure": last.attributes.get(
                        "high_pressure_events"
                    ),
                    "last_session_low_pressure": last.attributes.get(
                        "low_pressure_events"
                    ),
                    "last_session_high_pressure_time": last.attributes.get(
                        "high_pressure_seconds"
                    ),
                    "last_session_low_pressure_time": last.attributes.get(
                        "low_pressure_seconds"
                    ),
                    "last_session_average_pressure": last.attributes.get(
                        "average_pressure_millinewtons"
                    ),
                    "last_session_maximum_pressure": last.attributes.get(
                        "maximum_pressure_millinewtons"
                    ),
                    "last_session_battery_end": last.attributes.get(
                        "battery_percent_at_end"
                    ),
                    "last_session_target_duration": last.attributes.get(
                        "target_duration_seconds"
                    ),
                    "last_session_id": last.attributes.get("session_id"),
                    "last_session_source": last.attributes.get("source"),
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
                "low_pressure_events": data.get("last_session_low_pressure"),
                "high_pressure_seconds": data.get("last_session_high_pressure_time"),
                "low_pressure_seconds": data.get("last_session_low_pressure_time"),
                "average_pressure_millinewtons": data.get(
                    "last_session_average_pressure"
                ),
                "maximum_pressure_millinewtons": data.get(
                    "last_session_maximum_pressure"
                ),
                "battery_percent_at_end": data.get("last_session_battery_end"),
                "target_duration_seconds": data.get("last_session_target_duration"),
                "session_id": data.get("last_session_id"),
                "source": data.get("last_session_source"),
            }
        if self.entity_description.key == "toothbrush_state":
            self._attr_extra_state_attributes = {
                "live_connection": data.get("live"),
                "connection_mode": data.get("connection_mode"),
                "data_source": data.get("data_source"),
                "charger_address": data.get("charger_address"),
                "charger_bridge_latency_ms": data.get("charger_bridge_latency_ms"),
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
        elif self.entity_description.key == "pressure":
            self._attr_extra_state_attributes = {
                "force": data.get("pressure_force"),
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


class IOSenseSensor(SensorEntity):
    """A diagnostic sensor belonging to the matched iO Sense device."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OralBLiveCoordinator,
        description: OralBSensorDescription,
    ) -> None:
        assert coordinator.charger and coordinator.charger.address
        self.coordinator = coordinator
        self.charger = coordinator.charger
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}-iosense-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}-iosense")},
            connections={(CONNECTION_BLUETOOTH, self.charger.address)},
            name=self.charger.name,
            manufacturer="Oral-B",
            model="iO Sense",
            sw_version=self.charger.data.get("firmware"),
            hw_version=(
                str(self.charger.data["hardware_version"])
                if self.charger.data.get("hardware_version") is not None
                else None
            ),
            via_device=(DOMAIN, coordinator.address),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_CHARGER_UPDATE}_{self.coordinator.address}",
                self._handle_update,
            )
        )
        self._handle_update(self.charger.data)

    @callback
    def _handle_update(self, data: dict[str, Any]) -> None:
        self._attr_native_value = data.get(self.entity_description.data_key)
        if self.entity_description.key == "charger_state":
            self._attr_extra_state_attributes = {
                "address": data.get("address"),
                "mac": data.get("mac"),
                "rssi": data.get("rssi"),
                "firmware": data.get("firmware"),
                "hardware_version": data.get("hardware_version"),
                "body_color": data.get("body_color"),
                "server_mode": data.get("server_mode"),
                "internet_connected": data.get("internet_connected"),
                "cloud_connected": data.get("cloud_connected"),
                "brush_connected": data.get("brush_connected"),
                "brush_charging": data.get("brush_charging"),
                "brush_paired": data.get("brush_paired"),
                "bridge_connected": data.get("bridge_connected"),
                "last_response_latency_ms": data.get("last_response_latency_ms"),
            }
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if self.entity_description.key == "charger_state":
            return self.charger.available
        return self.charger.available and self._attr_native_value is not None
