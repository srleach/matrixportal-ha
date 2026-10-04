"""Config flow for MatrixPortal display OTA."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import github
from .const import (
    CONF_BASE_TOPIC,
    CONF_HOST_OVERRIDE,
    CONF_REPO,
    CONF_TOKEN,
    DEFAULT_BASE_TOPIC,
    DEFAULT_REPO,
    DOMAIN,
)

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_REPO, default=DEFAULT_REPO): str,
        vol.Required(CONF_TOKEN): str,
        vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str,
    }
)


class MatrixPortalOtaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the repository and a token that can read its releases."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the token against GitHub before accepting it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                await github.fetch_release(
                    session, user_input[CONF_REPO], "latest", user_input[CONF_TOKEN]
                )
            except github.GitHubAuthError:
                errors["base"] = "invalid_auth"
            except github.GitHubNotFoundError:
                errors["base"] = "no_release"
            except github.GitHubError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_REPO])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_REPO], data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options: the manual address override."""
        return MatrixPortalOtaOptionsFlow()


class MatrixPortalOtaOptionsFlow(OptionsFlow):
    """Pin the address list by hand, when something needs it."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_HOST_OVERRIDE, "")
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_HOST_OVERRIDE, default=current): str,
                }
            ),
        )
