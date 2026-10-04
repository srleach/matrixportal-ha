"""Working out which boards to talk to.

There is no list of addresses to keep up to date. Every board publishes
its own as a diagnostic sensor, so the list is derived from the devices
Home Assistant can already see: a board that changes address is followed,
and a new board joins on its own once it appears over MQTT.

The match is on the device model rather than the entity name, because two
boards may well be called the same thing -- which makes their entity ids
ambiguous but leaves the model alone.
"""

from __future__ import annotations

import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DEVICE_MODEL_MATCH

# The address sensor is picked out by its value looking like an address.
# A board's other sensors are signal, uptime, a message count and a sync
# state, none of which can be mistaken for one.
_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@callback
def discovered_hosts(hass: HomeAssistant) -> list[str]:
    """Every board address Home Assistant can currently see."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    hosts: set[str] = set()

    for device in devices.devices.values():
        if DEVICE_MODEL_MATCH not in (device.model or ""):
            continue
        for entry in er.async_entries_for_device(entities, device.id):
            if entry.domain != "sensor" or entry.platform != "mqtt":
                continue
            state = hass.states.get(entry.entity_id)
            if state is not None and _IP.match(state.state):
                hosts.add(state.state)

    return sorted(hosts)


def parse_override(raw: str | None) -> list[str]:
    """Splits the manual override into addresses.

    Set only to pin the list -- to reach a board that has dropped off
    MQTT, or to update one on purpose while leaving the others alone.
    """
    if not raw or raw.strip() in ("unknown", "unavailable"):
        return []
    return raw.split()
