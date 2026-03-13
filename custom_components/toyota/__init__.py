"""Toyota EU community integration."""

# pylint: disable=W0212, W0511

from __future__ import annotations

import asyncio
import asyncio.exceptions as asyncioexceptions
import logging
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict

import httpcore
import httpx
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from loguru import logger
from pydantic import ValidationError

from .const import CONF_BRAND, CONF_FETCH_HISTORY, CONF_METRIC_VALUES, DOMAIN, PLATFORMS, STARTUP_MESSAGE

_LOGGER = logging.getLogger(__name__)


# These imports must be after Loguru configuration to properly intercept logging
from pytoyoda.client import MyT  # noqa: E402
from pytoyoda.exceptions import (  # noqa: E402
    ToyotaApiError,
    ToyotaInternalError,
    ToyotaLoginError,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pytoyoda.models.summary import Summary
    from pytoyoda.models.vehicle import Vehicle


class StatisticsData(TypedDict):
    """Representing Statistics data."""

    day: Summary | None
    week: Summary | None
    month: Summary | None
    year: Summary | None


class VehicleData(TypedDict):
    """Representing Vehicle data."""

    data: Vehicle
    statistics: StatisticsData | None
    metric_values: bool

async def _refresh_vehicle_minimal(hass, vehicle):
    """Refresh only essential vehicle endpoints (avoids trips/notifications/service history)."""
    vin = vehicle.vin
    if not vin:
        return
    info = vehicle._vehicle_info
    ext = getattr(info, "extended_capabilities", None)
    feat = getattr(info, "features", None)
    calls = []
    calls.append(("health_status", vehicle._api.get_vehicle_health_status(vin=vin)))
    if getattr(ext, "telemetry_capable", False):
        calls.append(("telemetry", vehicle._api.get_telemetry(vin=vin)))
    if getattr(ext, "econnect_vehicle_status_capable", False):
        calls.append(("electric_status", vehicle._api.get_vehicle_electric_status(vin=vin)))
    if getattr(ext, "vehicle_status", False):
        calls.append(("status", vehicle._api.get_remote_status(vin=vin)))
    if getattr(ext, "last_parked_capable", False) or getattr(feat, "last_parked", False):
        calls.append(("location", vehicle._api.get_location(vin=vin)))
    climate_traditional = getattr(ext, "climate_capable", False)
    climate_econnect = getattr(ext, "econnect_climate_capable", False)
    climate_feature = getattr(feat, "climate_start_engine", False)
    if climate_traditional or climate_econnect or climate_feature:
        calls.append(("climate_settings", vehicle._api.get_climate_settings(vin=vin)))
        calls.append(("climate_status", vehicle._api.get_climate_status(vin=vin)))
    results = await asyncio.gather(
        *[hass.async_add_executor_job(_run_pytoyoda_sync, call) for _, call in calls],
        return_exceptions=True,
    )
    for (name, _), result in zip(calls, results, strict=False):
        if not isinstance(result, Exception):
            vehicle._endpoint_data[name] = result
        else:
            _LOGGER.debug("Failed to refresh %s: %s", name, result)

def loguru_to_hass(message: str) -> None:
    """Forward Loguru logs to standard Python logger used by HACS."""
    level_name = message.record["level"].name.lower()

    if "debug" in level_name:
        _LOGGER.debug(message)
    elif "info" in level_name:
        _LOGGER.info(message)
    elif "warn" in level_name:
        _LOGGER.warning(message)
    elif "error" in level_name:
        _LOGGER.error(message)
    else:
        _LOGGER.critical(message)


logger.remove()
logger.configure(handlers=[{"sink": loguru_to_hass}])

def _is_vehicle_charging(vehicle: Any) -> bool:
    """Best-effort: determine whether vehicle is charging from EV status."""
    status_obj = getattr(vehicle, "electric_status", None) or getattr(vehicle, "ev_status", None)
    if status_obj is None:
        return False
    raw_bool = getattr(status_obj, "is_charging", None)
    if isinstance(raw_bool, bool):
        return raw_bool
    raw = getattr(status_obj, "charging_status", None) or getattr(status_obj, "chargingStatus", None)
    if raw is None:
        return False
    raw_str = str(raw).strip().lower()
    return raw_str in {"charging", "in_progress", "inprogress", "start", "started"}


def _run_pytoyoda_sync(coro: Coroutine) -> Any:  # noqa : ANN401
    """Run a pytoyoda coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def async_setup_entry(  # pylint: disable=too-many-statements # noqa: PLR0915, C901
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up Toyota Connected Services from a config entry."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    metric_values = entry.data[CONF_METRIC_VALUES]
    fetch_history = entry.options.get(CONF_FETCH_HISTORY, False)
    brand = entry.data.get(
        CONF_BRAND, "toyota"
    )  # Get brand from config, default to toyota

    # Map brand selection to API brand code
    brand_map = {"toyota": "T", "lexus": "L"}
    brand_code = brand_map.get(brand, "T")

    _LOGGER.info("Setting up %s integration (brand code: %s)", brand, brand_code)

    client = await hass.async_add_executor_job(
        partial(
            MyT,
            username=email,
            password=password,
            use_metric=metric_values,
            brand=brand_code,  # Pass brand code to API client
        )
    )

    try:

        def _sync_login() -> Any:  # noqa: ANN401
            loop = asyncio.new_event_loop()
            result = None
            try:
                result = loop.run_until_complete(client.login())
            finally:
                loop.close()
            return result

        await hass.async_add_executor_job(_sync_login)
    except ToyotaLoginError as ex:
        raise ConfigEntryAuthFailed(ex) from ex
    except (httpx.ConnectTimeout, httpcore.ConnectTimeout) as ex:
        msg = "Unable to connect to Toyota Connected Services"
        raise ConfigEntryNotReady(msg) from ex

    async def async_get_vehicle_data() -> list[VehicleData] | None:  # noqa: C901
        """Fetch vehicle data from Toyota API."""
        try:
            vehicles = await asyncio.wait_for(
                hass.async_add_executor_job(_run_pytoyoda_sync, client.get_vehicles()),
                15,
            )
            vehicle_informations: list[VehicleData] = []
            if vehicles:
                for vehicle in vehicles:
                    if vehicle:
                        await _refresh_vehicle_minimal(hass, vehicle)
                        vehicle_data = VehicleData(
                            data=vehicle, statistics=None, metric_values=metric_values
                        )
                        if fetch_history and vehicle.vin is not None:
                            driving_statistics = await asyncio.gather(
                                hass.async_add_executor_job(
                                    _run_pytoyoda_sync,
                                    vehicle.get_current_day_summary(),
                                ),
                                hass.async_add_executor_job(
                                    _run_pytoyoda_sync,
                                    vehicle.get_current_week_summary(),
                                ),
                                hass.async_add_executor_job(
                                    _run_pytoyoda_sync,
                                    vehicle.get_current_month_summary(),
                                ),
                                hass.async_add_executor_job(
                                    _run_pytoyoda_sync,
                                    vehicle.get_current_year_summary(),
                                ),
                            )
                            vehicle_data["statistics"] = StatisticsData(
                                day=driving_statistics[0],
                                week=driving_statistics[1],
                                month=driving_statistics[2],
                                year=driving_statistics[3],
                            )
                        else:
                            vehicle_data["statistics"] = StatisticsData(
                                day=None,
                                week=None,
                                month=None,
                                year=None,
                            )
                        vehicle_informations.append(vehicle_data)
                return vehicle_informations
        except ToyotaLoginError:
            _LOGGER.exception("Toyota login error")
        except ToyotaInternalError as ex:
            _LOGGER.debug(ex)
        except ToyotaApiError as ex:
            raise UpdateFailed(ex) from ex
        except (httpx.ConnectTimeout, httpcore.ConnectTimeout) as ex:
            msg = "Unable to connect to Toyota Connected Services"
            raise UpdateFailed(msg) from ex
        except ValidationError:
            _LOGGER.exception("Toyota validation error")
        except (
            asyncioexceptions.CancelledError,
            asyncioexceptions.TimeoutError,
            httpx.ReadTimeout,
        ) as ex:
            msg = (
                "Update canceled! \n"
                "Toyota's API was too slow to respond. Will try again later."
            )
            raise UpdateFailed(msg) from ex
        return None

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_get_vehicle_data,
        update_interval=timedelta(seconds=1200),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
