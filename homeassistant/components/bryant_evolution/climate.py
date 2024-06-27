"""Support for Bryant Evolution HVAC systems."""

from datetime import timedelta
import logging
from typing import Any

from evolutionhttp import BryantEvolutionClient

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BryantEvolutionConfigEntry
from .const import CONF_SYSTEM_ID, CONF_ZONE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


SCAN_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BryantEvolutionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a config entry."""
    client = config_entry.runtime_data
    climate = BryantEvolutionClimate(
        # Suffix with -climate since we expect to add more entities later.
        f"{config_entry.entry_id}-climate",
        config_entry.data[CONF_SYSTEM_ID],
        config_entry.data[CONF_ZONE_ID],
        client,
    )
    curr_temp = BryantEvolutionTemperatureSensor(climate)
    async_add_entities([climate, curr_temp], update_before_add=True)


class BryantEvolutionClimate(ClimateEntity):
    """ClimateEntity for Bryant Evolution HVAC systems.

    Design note: this class updates using polling. However, polling
    is very slow (~1500 ms / parameter). To improve the user
    experience on updates, we also locally update this instance and
    call async_write_ha_state as well.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
        HVACMode.OFF,
    ]
    _attr_fan_modes = ["auto", "low", "med", "high"]
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self: ClimateEntity,
        unique_id: str,
        system_id: int,
        zone_id: int,
        client: BryantEvolutionClient,
    ) -> None:
        """Initialize an entity from parts."""
        self._client = client

        self._attr_unique_id = unique_id
        assert self.unique_id is not None  # Prevent mypy failure on next line
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            name=f"Bryant Evolution (System {system_id}, Zone {zone_id})",
        )

    async def async_update(self) -> None:
        """Update the entity state."""
        self._attr_current_temperature = await self._client.read_current_temperature()
        self._attr_fan_mode = await self._client.read_fan_mode()
        if self._attr_fan_mode is not None:
            self._attr_fan_mode = self._attr_fan_mode.lower()
        self._attr_target_temperature = None
        self._attr_target_temperature_high = None
        self._attr_target_temperature_low = None
        self._attr_hvac_mode = await self._read_hvac_mode()

        # Set target_temperature or target_temperature_{high, low} based on mode.
        match self._attr_hvac_mode:
            case HVACMode.HEAT:
                self._attr_target_temperature = (
                    await self._client.read_heating_setpoint()
                )
            case HVACMode.COOL:
                self._attr_target_temperature = (
                    await self._client.read_cooling_setpoint()
                )
            case HVACMode.HEAT_COOL:
                self._attr_target_temperature_high = (
                    await self._client.read_cooling_setpoint()
                )
                self._attr_target_temperature_low = (
                    await self._client.read_heating_setpoint()
                )
            case HVACMode.OFF:
                pass
            case _:
                _LOGGER.error("Unknown HVAC mode %s", self._attr_hvac_mode)

        # Note: depends on current temperature and target temperature low read
        # above.
        self._attr_hvac_action = await self._read_hvac_action()

    async def _read_hvac_mode(self) -> HVACMode:
        mode_and_active = await self._client.read_hvac_mode()
        if not mode_and_active:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="failed_to_read_hvac_mode"
            )
        mode = mode_and_active[0]
        match mode.upper():
            case "HEAT":
                return HVACMode.HEAT
            case "COOL":
                return HVACMode.COOL
            case "AUTO":
                return HVACMode.HEAT_COOL
            case "OFF":
                return HVACMode.OFF

        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="failed_to_parse_hvac_mode",
            translation_placeholders={"mode": mode},
        )

    async def _read_hvac_action(self) -> HVACAction:
        """Return the current running hvac operation."""
        mode_and_active = await self._client.read_hvac_mode()
        if not mode_and_active:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="failed_to_read_hvac_action"
            )
        mode, is_active = mode_and_active
        if not is_active:
            return HVACAction.OFF
        match mode.upper():
            case "HEAT":
                return HVACAction.HEATING
            case "COOL":
                return HVACAction.COOLING
            case "OFF":
                return HVACAction.OFF
            case "AUTO":
                # In AUTO, we need to figure out what the actual mode is
                # based on the setpoints.
                if (
                    self.current_temperature is not None
                    and self.target_temperature_low is not None
                ):
                    if self.current_temperature > self.target_temperature_low:
                        # If the system is on and the current temperature is
                        # higher than the point at which heating would activate,
                        # then we must be cooling.
                        return HVACAction.COOLING
                    return HVACAction.HEATING
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="failed_to_parse_hvac_mode",
            translation_placeholders={
                "mode_and_active": mode_and_active,
                "current_temperature": str(self.current_temperature),
                "target_temperature_low": str(self.target_temperature_low),
            },
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.HEAT_COOL:
            hvac_mode = HVACMode.AUTO
        if not await self._client.set_hvac_mode(hvac_mode):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="failed_to_set_hvac_mode"
            )
        self._attr_hvac_mode = hvac_mode
        self._async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if kwargs.get("target_temp_high"):
            temp = int(kwargs["target_temp_high"])
            if not await self._client.set_cooling_setpoint(temp):
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="failed_to_set_clsp"
                )
            self._attr_target_temperature_high = temp
            self._async_write_ha_state()

        if kwargs.get("target_temp_low"):
            temp = int(kwargs["target_temp_low"])
            if not await self._client.set_heating_setpoint(temp):
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="failed_to_set_htsp"
                )
            self._attr_target_temperature_low = temp
            self._async_write_ha_state()

        if kwargs.get("temperature"):
            temp = int(kwargs["temperature"])
            fn = (
                self._client.set_heating_setpoint
                if self.hvac_mode == HVACMode.HEAT
                else self._client.set_cooling_setpoint
            )
            if not await fn(temp):
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="failed_to_set_temp"
                )
            self._attr_target_temperature = temp
            self._async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        if not await self._client.set_fan_mode(fan_mode):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="failed_to_set_fan_mode"
            )
        self._attr_fan_mode = fan_mode.lower()
        self.async_write_ha_state()


class BryantEvolutionTemperatureSensor(SensorEntity):
    """Representation of a Bryant Evolution Temperature Sensor."""

    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, climate_entity: BryantEvolutionClimate) -> None:
        """Initialize the sensor."""
        self._climate_entity = climate_entity
        self._attr_name = f"{climate_entity.name} Temperature"
        self._attr_unique_id = f"{climate_entity.unique_id}_temperature"
        self._attr_device_info = climate_entity.device_info

    @property
    def native_value(self) -> float | None:
        """Return the current temperature."""
        return self._climate_entity.current_temperature
