"""Keeping the boards told what the newest release is."""

from __future__ import annotations

import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import github
from .const import SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class LatestTagCoordinator(DataUpdateCoordinator[str]):
    """Publishes the newest release tag to the shared retained topic.

    Every board's update entity reads `latest_version` from one topic, so
    this publishes once rather than per board, and retained so a board
    that joins later still learns it. Contacts GitHub only -- no board is
    touched.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        repo: str,
        token: str,
        latest_topic: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="MatrixPortal latest firmware",
            update_interval=SCAN_INTERVAL,
        )
        self.repo = repo
        self.token = token
        self.latest_topic = latest_topic

    async def _async_update_data(self) -> str:
        session = async_get_clientsession(self.hass)
        try:
            release = await github.fetch_release(
                session, self.repo, "latest", self.token
            )
        except github.GitHubError as err:
            raise UpdateFailed(str(err)) from err

        tag = release["tag_name"]
        await mqtt.async_publish(self.hass, self.latest_topic, tag, retain=True)
        _LOGGER.debug("published %s to %s", tag, self.latest_topic)
        return tag
