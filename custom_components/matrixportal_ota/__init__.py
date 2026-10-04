"""Firmware updates for the MatrixPortal displays, driven from Home Assistant.

The boards cannot fetch their own firmware -- the repository is private,
and reaching it would mean a TLS stack, a current CA bundle and GitHub's
redirects on a WiFi co-processor running 2019 firmware. So Home
Assistant does the fetching and pushes the image over the LAN.

The token never leaves Home Assistant. The board never learns there is a
GitHub.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_DRY_RUN,
    ATTR_ONLY,
    ATTR_TAG,
    CONF_BASE_TOPIC,
    CONF_HOST_OVERRIDE,
    CONF_REPO,
    CONF_TOKEN,
    DEFAULT_BASE_TOPIC,
    DOMAIN,
    INSTALL_TOPIC,
    LATEST_TOPIC,
    SERVICE_REFRESH_LATEST,
    SERVICE_UPDATE,
)
from .coordinator import LatestTagCoordinator
from .runner import UpdateRunner

_LOGGER = logging.getLogger(__name__)

UPDATE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_TAG, default="latest"): cv.string,
        vol.Optional(ATTR_ONLY, default=""): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    # Everything here rides on MQTT: the latest-version topic the boards
    # read, and the install topic they ask on.
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not available yet")

    repo: str = entry.data[CONF_REPO]
    token: str = entry.data[CONF_TOKEN]
    base: str = entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
    override: str = entry.options.get(CONF_HOST_OVERRIDE, "")

    coordinator = LatestTagCoordinator(
        hass, repo, token, LATEST_TOPIC.format(base=base)
    )
    runner = UpdateRunner(hass, repo, token, override)

    await coordinator.async_config_entry_first_refresh()

    @callback
    def _on_install(message: mqtt.ReceiveMessage) -> None:
        """The Install button on a board's Firmware entity.

        The board publishes the update entity but never subscribes to
        this topic -- being told to install is no use to something that
        cannot fetch. The push runs as a background task: it takes tens
        of seconds, and the MQTT callback must not wait for it.
        """
        parts = message.topic.split("/")
        if len(parts) < 2:
            return
        device = parts[1]
        _LOGGER.info("install requested for %s", device)
        hass.async_create_background_task(
            runner.async_run(tag="latest", only=device),
            f"{DOMAIN} install {device}",
        )

    entry.async_on_unload(
        await mqtt.async_subscribe(
            hass, INSTALL_TOPIC.format(base=base), _on_install
        )
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "runner": runner,
    }

    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)
        hass.services.async_remove(DOMAIN, SERVICE_UPDATE)
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH_LATEST)

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Pick up a changed address override or base topic."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _register_services(hass: HomeAssistant) -> None:
    """Register the two services, once."""
    if hass.services.has_service(DOMAIN, SERVICE_UPDATE):
        return

    def _runtime() -> dict:
        entries = hass.data.get(DOMAIN) or {}
        if not entries:
            raise HomeAssistantError(
                "the MatrixPortal display OTA integration is not loaded"
            )
        return next(iter(entries.values()))

    async def _update(call: ServiceCall) -> ServiceResponse:
        runner: UpdateRunner = _runtime()["runner"]
        return await runner.async_run(
            tag=call.data[ATTR_TAG],
            only=call.data[ATTR_ONLY],
            dry_run=call.data[ATTR_DRY_RUN],
        )

    async def _refresh_latest(call: ServiceCall) -> None:
        coordinator: LatestTagCoordinator = _runtime()["coordinator"]
        await coordinator.async_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE,
        _update,
        schema=UPDATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_LATEST, _refresh_latest)
