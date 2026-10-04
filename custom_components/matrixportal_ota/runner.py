"""Running an update across the boards."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import github, ota
from .const import DOMAIN
from .hosts import discovered_hosts, parse_override

_LOGGER = logging.getLogger(__name__)


class NoBoardsError(HomeAssistantError):
    """Nothing to update: none discovered, and no override set."""


class ImageMismatchError(HomeAssistantError):
    """The downloaded image does not match the hash in its .sig."""


class UpdateRunner:
    """Fetches a release once and pushes it to every board that needs it.

    One run at a time. Two pushes share a release and would fight over
    the boards, so the lock here does what `mode: single` did for the
    script this replaces.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        repo: str,
        token: str,
        host_override: str | None = None,
    ) -> None:
        self.hass = hass
        self.repo = repo
        self.token = token
        self.host_override = host_override
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        """True while a push is in flight."""
        return self._lock.locked()

    async def async_run(
        self, tag: str = "latest", only: str = "", dry_run: bool = False
    ) -> dict[str, Any]:
        """Updates the boards, waiting for any run already under way."""
        async with self._lock:
            return await self._run(tag, only, dry_run)

    async def _run(
        self, tag: str, only: str, dry_run: bool
    ) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        log: list[str] = []

        def say(line: str) -> None:
            _LOGGER.info("%s", line.strip())
            log.append(line)

        release = await github.fetch_release(session, self.repo, tag, self.token)
        resolved = release["tag_name"]
        say(f"release {resolved}")

        hosts = parse_override(self.host_override) or discovered_hosts(self.hass)
        if not hosts:
            raise NoBoardsError(
                "no boards: none discovered over MQTT, and no address override set"
            )

        image = await github.download_asset(
            session, github.pick_asset(release, ".bin")["url"], self.token
        )
        sig_bytes = await github.download_asset(
            session, github.pick_asset(release, ".bin.sig")["url"], self.token
        )
        sha, sig = github.parse_sig(sig_bytes.decode())

        actual = hashlib.sha256(image).hexdigest()
        if actual != sha:
            raise ImageMismatchError(
                f"the downloaded image does not match its .sig ({actual} vs {sha})"
            )
        say(
            f"  {len(image)} bytes, sha256 {sha[:16]}... verified against the .sig"
        )

        targets: list[tuple[str, str]] = []
        unreachable: list[str] = []
        skipped: list[str] = []

        for host in hosts:
            status = await ota.board_status(session, host)
            if status is None:
                # Not silently. A board that is switched off, or has
                # dropped off the network, is the one case where saying
                # nothing is worst: the update looks like it worked and
                # nobody learns that a display is still on the old
                # firmware.
                say(f"  {host}: unreachable")
                unreachable.append(host)
                continue

            name = status.get("id", "?")
            firmware = status.get("fw", "?")
            if only and name != only:
                continue
            if firmware == resolved:
                say(f"  {host} ({name}): already on {resolved}")
                skipped.append(name)
                continue
            say(f"  {host} ({name}): {firmware} -> {resolved}")
            targets.append((host, name))

        if unreachable:
            say(f"could not reach: {', '.join(unreachable)}")

        result: dict[str, Any] = {
            "tag": resolved,
            "dry_run": dry_run,
            "updated": [],
            "failed": [],
            "skipped": skipped,
            "unreachable": unreachable,
        }

        if dry_run:
            say("dry run: stopping here")
        elif not targets:
            say(
                "every board that answered is already on this release"
                if not unreachable
                else "nothing to do on the boards that answered"
            )

        if targets and not dry_run:
            for host, name in targets:
                say(f"{host} ({name}): pushing")
                if not await ota.push(session, host, image, sha, sig, say):
                    result["failed"].append(name)
                    continue
                if await ota.wait_for(session, host, resolved):
                    say(f"  {host}: now on {resolved}")
                    result["updated"].append(name)
                else:
                    say(f"  {host}: did not come back on {resolved} -- check the board")
                    result["failed"].append(name)

        result["log"] = log

        if result["failed"] or unreachable:
            self._notify(result, log)

        return result

    def _notify(self, result: dict[str, Any], log: list[str]) -> None:
        """Says so in the UI, as the script this replaces did."""
        trouble = []
        if result["failed"]:
            trouble.append(f"failed: {', '.join(result['failed'])}")
        if result["unreachable"]:
            trouble.append(f"unreachable: {', '.join(result['unreachable'])}")

        persistent_notification.async_create(
            self.hass,
            title="Matrix firmware update incomplete",
            message="\n".join([*trouble, "", "```", *log, "```"]),
            notification_id=f"{DOMAIN}_update_failed",
        )
