"""Sensor platform for Toyota integration."""

# pylint: disable=W0212, W0511

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfLength
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.core import callback

from .const import CONF_BRAND_MAPPING, DOMAIN
from .entity import ToyotaBaseEntity
from .utils import (
    format_statistics_attributes,
    format_vin_sensor_attributes,
    round_number,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from pytoyoda.models.vehicle import Vehicle

    from . import StatisticsData, VehicleData


_LOGGER = logging.getLogger(__name__)




def _coerce_float(value: Any) -> float | None:  # noqa: ANN401
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_nested_attr(obj: Any, *names: str) -> Any:  # noqa: ANN401
    if obj is None:
        return None
    for name in names:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(name)
        else:
            obj = getattr(obj, name, None)
    return obj


def _get_battery_percent(vehicle: Vehicle) -> float | None:
    """Battery percent taken directly from EV charging status data.

    This mirrors the "battery_percent" attribute exposed on the
    "battery_charging_state" sensor.
    """

    status_obj = (
        getattr(vehicle, "electric_status", None)
        or getattr(vehicle, "ev_status", None)
        or getattr(vehicle, "remote_electric_status", None)
        or getattr(vehicle, "remote_ev_status", None)
    )

    return _coerce_float(
        _get_nested_attr(status_obj, "battery_level")
        or _get_nested_attr(status_obj, "battery_percentage")
        or _get_nested_attr(status_obj, "batteryPercent")
        or _get_nested_attr(status_obj, "battery_percent")
    )


def _get_charging_state(vehicle: Vehicle) -> str | None:
    status_obj = (
        getattr(vehicle, "electric_status", None)
        or getattr(vehicle, "ev_status", None)
        or getattr(vehicle, "remote_electric_status", None)
        or getattr(vehicle, "remote_ev_status", None)
    )

    # Try common fields (bool first)
    for field in (
        "is_charging",
        "charging",
        "charging_status",
        "chargingStatus",
        "charging_state",
        "chargingState",
        "charge_status",
        "chargeStatus",
    ):
        raw = _get_nested_attr(status_obj, field)
        if isinstance(raw, bool):
            return "charging" if raw else "not_charging"
        if raw is not None:
            raw_str = str(raw).strip().lower()
            if raw_str in {"charging", "in_progress", "inprogress", "start", "started"}:
                return "charging"
            if raw_str in {"not_charging", "notcharging", "stopped", "stop", "idle"}:
                return "not_charging"
            # Keep unknown enums as-is to avoid hiding useful info
            return raw_str

    # If we can infer from plug status, use that.
    plug = (
        _get_nested_attr(status_obj, "plugged_in")
        or _get_nested_attr(status_obj, "plugStatus")
        or _get_nested_attr(status_obj, "plug_status")
    )
    if isinstance(plug, bool):
        return "plugged" if plug else "unplugged"
    return None


def get_vehicle_capability(
    vehicle: Vehicle,
    capability_name: str,
    default: bool = False,  # noqa: FBT001, FBT002
) -> bool:
    """Safely retrieve a vehicle capability with a default fallback.

    Args:
        vehicle: The vehicle object
        capability_name: Name of the capability to check
        default: Default return value if capability cannot be retrieved

    Returns:
        bool: Value of the requested capability

    """
    try:
        return getattr(
            getattr(vehicle._vehicle_info, "extended_capabilities", False),  # noqa : SLF001
            capability_name,
            default,
        )
    except Exception:  # pylint: disable=W0718 # noqa : BLE001
        return default


class ToyotaSensorEntityDescription(SensorEntityDescription, frozen_or_thawed=True):
    """Describes a Toyota sensor entity."""

    value_fn: Callable[[Vehicle], StateType]
    attributes_fn: Callable[[Vehicle], dict[str, Any] | None]


class ToyotaStatisticsSensorEntityDescription(
    SensorEntityDescription, frozen_or_thawed=True
):
    """Describes a Toyota statistics sensor entity."""

    period: Literal["day", "week", "month", "year"]


VIN_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="vin",
    translation_key="vin",
    icon="mdi:car-info",
    entity_category=EntityCategory.DIAGNOSTIC,
    device_class=SensorDeviceClass.ENUM,
    state_class=None,
    value_fn=lambda vehicle: vehicle.vin,
    attributes_fn=lambda vehicle: format_vin_sensor_attributes(vehicle._vehicle_info),  # noqa : SLF001
)
ODOMETER_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="odometer",
    translation_key="odometer",
    icon="mdi:counter",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.TOTAL_INCREASING,
    value_fn=lambda vehicle: None
    if vehicle.dashboard is None
    else round_number(vehicle.dashboard.odometer),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa : ARG005
)
FUEL_LEVEL_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="fuel_level",
    translation_key="fuel_level",
    icon="mdi:gas-station",
    device_class=None,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda vehicle: None
    if vehicle.dashboard is None
    else round_number(vehicle.dashboard.fuel_level),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa : ARG005
)
FUEL_RANGE_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="fuel_range",
    translation_key="fuel_range",
    icon="mdi:map-marker-distance",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda vehicle: None
    if vehicle.dashboard is None
    else round_number(vehicle.dashboard.fuel_range),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa : ARG005
)
BATTERY_LEVEL_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="battery_level",
    translation_key="battery_level",
    icon="mdi:car-electric",
    device_class=SensorDeviceClass.BATTERY,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda vehicle: None
    if (percent := _get_battery_percent(vehicle)) is None
    else round_number(percent),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa: ARG005
)
BATTERY_RANGE_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="battery_range",
    translation_key="battery_range",
    icon="mdi:map-marker-distance",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda vehicle: None
    if vehicle.dashboard is None
    else round_number(vehicle.dashboard.battery_range),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa : ARG005
)

BATTERY_CHARGING_STATE_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="battery_charging_state",
    translation_key="battery_charging_state",
    icon="mdi:battery-charging",
    device_class=SensorDeviceClass.ENUM,
    state_class=None,
    value_fn=_get_charging_state,
    attributes_fn=lambda vehicle: (lambda status_obj: {
        "source": "vehicle.electric_status/ev_status",
        "battery_percent": _get_battery_percent(vehicle),
        "charging_status": (
            _get_nested_attr(status_obj, "charging_status")
            or _get_nested_attr(status_obj, "chargingStatus")
            or _get_nested_attr(status_obj, "charging_state")
            or _get_nested_attr(status_obj, "chargingState")
        ),
        "remaining_charge_time": (
            str(_get_nested_attr(status_obj, "remaining_charge_time"))
            if _get_nested_attr(status_obj, "remaining_charge_time") is not None
            else None
        ),
        "ev_range": _coerce_float(_get_nested_attr(status_obj, "ev_range")),
        "ev_range_with_ac": _coerce_float(
            _get_nested_attr(status_obj, "ev_range_with_ac")
        ),
        "last_update_timestamp": (
            str(_get_nested_attr(status_obj, "last_update_timestamp"))
            if _get_nested_attr(status_obj, "last_update_timestamp") is not None
            else None
        ),
        "next_charging_event": (
            str(_get_nested_attr(status_obj, "next_charging_event"))
            if _get_nested_attr(status_obj, "next_charging_event") is not None
            else None
        ),
        "can_set_next_charging_event": _get_nested_attr(
            status_obj, "can_set_next_charging_event"
        ),
    })(
        getattr(vehicle, "electric_status", None)
        or getattr(vehicle, "ev_status", None)
        or getattr(vehicle, "remote_electric_status", None)
        or getattr(vehicle, "remote_ev_status", None)
    ),
)

BATTERY_ENERGY_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="battery_energy",
    translation_key="battery_energy",
    icon="mdi:battery",
    # This is *stored* energy (not cumulative consumption), so keep device_class
    # unset and expose kWh as a measurement.
    device_class=None,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
    value_fn=lambda vehicle: (
        None
        if (percent := _get_battery_percent(vehicle)) is None
        else round(percent * 64.0 / 100.0, 1)
    ),
    attributes_fn=lambda vehicle: {
        "usable_capacity_kwh": 64.0,
        "battery_percent": _get_battery_percent(vehicle),
    },
)
BATTERY_RANGE_AC_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="battery_range_ac",
    translation_key="battery_range_ac",
    icon="mdi:map-marker-distance",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda vehicle: None
    if vehicle.dashboard is None
    else round_number(vehicle.dashboard.battery_range_with_ac),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa : ARG005
)
TOTAL_RANGE_ENTITY_DESCRIPTION = ToyotaSensorEntityDescription(
    key="total_range",
    translation_key="total_range",
    icon="mdi:map-marker-distance",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=lambda vehicle: None
    if vehicle.dashboard is None
    else round_number(vehicle.dashboard.range),
    suggested_display_precision=0,
    attributes_fn=lambda vehicle: None,  # noqa : ARG005
)

STATISTICS_ENTITY_DESCRIPTIONS_DAILY = ToyotaStatisticsSensorEntityDescription(
    key="current_day_statistics",
    translation_key="current_day_statistics",
    icon="mdi:history",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
    period="day",
)

STATISTICS_ENTITY_DESCRIPTIONS_WEEKLY = ToyotaStatisticsSensorEntityDescription(
    key="current_week_statistics",
    translation_key="current_week_statistics",
    icon="mdi:history",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
    period="week",
)

STATISTICS_ENTITY_DESCRIPTIONS_MONTHLY = ToyotaStatisticsSensorEntityDescription(
    key="current_month_statistics",
    translation_key="current_month_statistics",
    icon="mdi:history",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
    period="month",
)

STATISTICS_ENTITY_DESCRIPTIONS_YEARLY = ToyotaStatisticsSensorEntityDescription(
    key="current_year_statistics",
    translation_key="current_year_statistics",
    icon="mdi:history",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
    period="year",
)


def create_sensor_configurations(metric_values: bool) -> list[dict[str, Any]]:  # noqa : FBT001
    """Create a list of sensor configurations based on vehicle capabilities.

    Args:
        vehicle: The vehicle object
        metric_values: Whether to use metric units

    Returns:
        List of sensor configurations

    """

    def get_length_unit(metric: bool) -> str:  # noqa : FBT001
        return UnitOfLength.KILOMETERS if metric else UnitOfLength.MILES

    return [
        {
            "description": VIN_ENTITY_DESCRIPTION,
            "capability_check": lambda v: True,  # noqa : ARG005
            "native_unit": None,
            "suggested_unit": None,
        },
        {
            "description": ODOMETER_ENTITY_DESCRIPTION,
            "capability_check": lambda v: get_vehicle_capability(
                v, "telemetry_capable"
            ),
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": FUEL_LEVEL_ENTITY_DESCRIPTION,
            "capability_check": lambda v: (
                get_vehicle_capability(v, "fuel_level_available")
                and v.type != "electric"
            ),
            "native_unit": PERCENTAGE,
            "suggested_unit": None,
        },
        {
            "description": FUEL_RANGE_ENTITY_DESCRIPTION,
            "capability_check": lambda v: (
                get_vehicle_capability(v, "fuel_range_available")
                and v.type != "electric"
            ),
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": BATTERY_LEVEL_ENTITY_DESCRIPTION,
            "capability_check": lambda v: get_vehicle_capability(
                v, "econnect_vehicle_status_capable"
            )
            or v.type == "electric",
            "native_unit": PERCENTAGE,
            "suggested_unit": None,
        },
        {
            "description": BATTERY_CHARGING_STATE_ENTITY_DESCRIPTION,
            "capability_check": lambda v: get_vehicle_capability(
                v, "econnect_vehicle_status_capable"
            )
            or v.type == "electric",
            "native_unit": None,
            "suggested_unit": None,
        },
        {
            "description": BATTERY_ENERGY_ENTITY_DESCRIPTION,
            "capability_check": lambda v: get_vehicle_capability(
                v, "econnect_vehicle_status_capable"
            )
            or v.type == "electric",
            "native_unit": UnitOfEnergy.KILO_WATT_HOUR,
            "suggested_unit": None,
        },
        {
            "description": BATTERY_RANGE_ENTITY_DESCRIPTION,
            "capability_check": lambda v: get_vehicle_capability(
                v, "econnect_vehicle_status_capable"
            )
            or v.type == "electric",
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": BATTERY_RANGE_AC_ENTITY_DESCRIPTION,
            "capability_check": lambda v: get_vehicle_capability(
                v, "econnect_vehicle_status_capable"
            )
            or v.type == "electric",
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": TOTAL_RANGE_ENTITY_DESCRIPTION,
            "capability_check": lambda v: (
                get_vehicle_capability(v, "econnect_vehicle_status_capable")
                and get_vehicle_capability(v, "fuel_range_available")
                and v.type != "electric"
            ),
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": STATISTICS_ENTITY_DESCRIPTIONS_DAILY,
            "capability_check": lambda v: True,  # noqa : ARG005
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": STATISTICS_ENTITY_DESCRIPTIONS_WEEKLY,
            "capability_check": lambda v: True,  # noqa : ARG005
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": STATISTICS_ENTITY_DESCRIPTIONS_MONTHLY,
            "capability_check": lambda v: True,  # noqa : ARG005
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
        {
            "description": STATISTICS_ENTITY_DESCRIPTIONS_YEARLY,
            "capability_check": lambda v: True,  # noqa : ARG005
            "native_unit": get_length_unit(metric_values),
            "suggested_unit": get_length_unit(metric_values),
        },
    ]


class ToyotaSensor(ToyotaBaseEntity, SensorEntity):
    """Representation of a Toyota sensor."""

    vehicle: Vehicle

    def __init__(  # noqa: PLR0913
        self,
        coordinator: DataUpdateCoordinator[list[VehicleData]],
        entry_id: str,
        vehicle_index: int,
        description: ToyotaSensorEntityDescription,
        native_unit: UnitOfLength | str,
        suggested_unit: UnitOfLength | str,
    ) -> None:
        """Initialise the ToyotaSensor class."""
        super().__init__(coordinator, entry_id, vehicle_index, description)
        self.description = description
        self._attr_native_unit_of_measurement = native_unit
        self._attr_suggested_unit_of_measurement = suggested_unit

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self.description.value_fn(self.vehicle)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the attributes of the sensor."""
        return self.description.attributes_fn(self.vehicle)


class ToyotaStatisticsSensor(ToyotaBaseEntity, SensorEntity):
    """Representation of a Toyota statistics sensor."""

    statistics: StatisticsData

    def __init__(  # noqa: PLR0913
        self,
        coordinator: DataUpdateCoordinator[list[VehicleData]],
        entry_id: str,
        vehicle_index: int,
        description: ToyotaStatisticsSensorEntityDescription,
        native_unit: UnitOfLength | str,
        suggested_unit: UnitOfLength | str,
    ) -> None:
        """Initialise the ToyotaStatisticsSensor class."""
        super().__init__(coordinator, entry_id, vehicle_index, description)
        self.period: Literal["day", "week", "month", "year"] = description.period
        self._attr_native_unit_of_measurement = native_unit
        self._attr_suggested_unit_of_measurement = suggested_unit

    @property
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        data = self.statistics[self.period]
        return round(data.distance, 1) if data and data.distance else None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return the state attributes."""
        data = self.statistics[self.period]
        return (
            format_statistics_attributes(data, self.vehicle._vehicle_info)  # noqa : SLF001
            if data
            else None
        )


class ToyotaAwayChargeSensor(RestoreEntity, SensorEntity):
    @property
    def device_info(self):
        info = getattr(self.vehicle, "_vehicle_info", None)  # noqa: SLF001
        brand = getattr(info, "brand", None)
        return {
            "identifiers": {(DOMAIN, self.vehicle.vin or "Unknown")},
            "name": getattr(self.vehicle, "alias", None) or self.vehicle.vin,
            "manufacturer": CONF_BRAND_MAPPING.get(brand) if brand else "Unknown",
            "model": getattr(info, "car_model_name", None),
        }
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[list[VehicleData]],
        entry_id: str,
        vehicle_index: int,
        parking_location_entity_id: str,
    ):
        super().__init__()
        self.coordinator = coordinator
        self.entry_id = entry_id
        self.index = vehicle_index
        self.vehicle = coordinator.data[self.index]["data"]
        self.parking_location_entity_id = parking_location_entity_id
        self._attr_unique_id = f"{entry_id}_{self.vehicle.vin}_away_charge"
        self._attr_name = "Charged Not Home"
        # Do not initialize _total or _last_kwh here

    async def async_added_to_hass(self):
        last_state = await self.async_get_last_state()
        if last_state:
            if last_state.state not in (None, 'unknown', 'unavailable'):
                try:
                    self._total = float(last_state.state)
                except (ValueError, TypeError):
                    self._total = 0.0
            else:
                self._total = 0.0
            self._last_kwh = last_state.attributes.get("last_kwh")
            if self._last_kwh is not None:
                try:
                    self._last_kwh = float(self._last_kwh)
                except (ValueError, TypeError):
                    self._last_kwh = 0.0
        else:
            self._total = 0.0
            self._last_kwh = None

        # Subscribe to coordinator updates (this entity does not inherit CoordinatorEntity)
        remove_listener = self.coordinator.async_add_listener(self._handle_coordinator_update)
        self.async_on_remove(remove_listener)

        # Run once immediately
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self):
        # Always read the latest vehicle object from the coordinator
        self.vehicle = self.coordinator.data[self.index]["data"]

        current_kwh = getattr(self.vehicle, "battery_energy_kwh", None)
        kwh_source = "vehicle.battery_energy_kwh"
        if current_kwh is None:
            # Fallback: estimate stored energy from reported battery percent.
            # Keep this simple and consistent with BATTERY_ENERGY_ENTITY_DESCRIPTION.
            percent = _get_battery_percent(self.vehicle)
            if percent is None:
                return
            current_kwh = percent * 64.0 / 100.0
            kwh_source = "battery_percent*64kwh"
        parking_location = self.hass.states.get(self.parking_location_entity_id)

        if current_kwh is None or parking_location is None:
            return

        try:
            current_kwh = float(current_kwh)
        except (ValueError, TypeError):
            return

        if self._last_kwh is None:
            self._last_kwh = current_kwh
            self.async_write_ha_state()
            return

        if parking_location.state != 'home':
            delta = current_kwh - self._last_kwh
            if delta > 0:
                self._total += delta
        self._last_kwh = current_kwh
        self._kwh_source = kwh_source
        self.async_write_ha_state()

    @property
    def state(self):
        return round(self._total, 2) if self._total is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "last_kwh": self._last_kwh,
            "kwh_source": getattr(self, "_kwh_source", None),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: DataUpdateCoordinator[list[VehicleData]] = hass.data[DOMAIN][
        entry.entry_id
    ]

    sensors: list[ToyotaSensor | ToyotaStatisticsSensor | ToyotaAwayChargeSensor] = []
    for index, vehicle_data in enumerate(coordinator.data):
        vehicle = vehicle_data["data"]
        metric_values = vehicle_data["metric_values"]

        sensor_configs = create_sensor_configurations(metric_values)

        sensors.extend(
            ToyotaSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                vehicle_index=index,
                description=config["description"],
                native_unit=config["native_unit"],
                suggested_unit=config["suggested_unit"],
            )
            for config in sensor_configs
            if not config["description"].key.startswith("current_")
            and config["capability_check"](vehicle)
        )
        # Add the away charge sensor for each vehicle
        parking_location_entity_id = f"device_tracker.{vehicle.vin}_parking_location"
        sensors.append(
            ToyotaAwayChargeSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                vehicle_index=index,
                parking_location_entity_id=parking_location_entity_id,
            )
        )
        # Commented out statistics sensors
        # sensors.extend(
        #     ToyotaStatisticsSensor(
        #         coordinator=coordinator,
        #         entry_id=entry.entry_id,
        #         vehicle_index=index,
        #         description=config["description"],
        #         native_unit=config["native_unit"],
        #         suggested_unit=config["suggested_unit"],
        #     )
        #     for config in sensor_configs
        #     if config["description"].key.startswith("current_")
        #     and config["capability_check"](vehicle)
        # )

    async_add_devices(sensors)
