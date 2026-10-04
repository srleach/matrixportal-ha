"""Fetching signed firmware releases from GitHub.

Only this module ever sees the token. The boards never learn there is a
GitHub: Home Assistant does the fetching and pushes the bytes over the
LAN instead, because reaching GitHub from a board would mean a TLS
stack, a current CA bundle and GitHub's redirects on a WiFi
co-processor running 2019 firmware.

Nothing fetched here is trusted by a board. The image carries an Ed25519
signature from the release workflow, and a board refuses anything that
does not verify against the public key built into it -- so this module
being wrong, or someone else running one like it, cannot flash anything.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = aiohttp.ClientTimeout(total=120)

# GitHub answers an asset download with one redirect to signed object
# storage. Needing more than a few hops means something has gone wrong,
# not that the store is unusually deep.
MAX_REDIRECTS = 5


class GitHubError(HomeAssistantError):
    """GitHub would not give us what we asked for."""


class GitHubAuthError(GitHubError):
    """The token is missing, wrong, or lacks Contents: Read."""


class GitHubNotFoundError(GitHubError):
    """No such release -- or the token cannot see the repository."""


def _headers(token: str, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "matrixportal-ota",
    }


async def fetch_release(
    session: aiohttp.ClientSession, repo: str, tag: str, token: str
) -> dict[str, Any]:
    """Returns the release named by `tag`, or the newest for "latest"."""
    where = "releases/latest" if tag == "latest" else f"releases/tags/{tag}"
    url = f"{API}/repos/{repo}/{where}"

    try:
        async with session.get(
            url, headers=_headers(token, "application/vnd.github+json"), timeout=TIMEOUT
        ) as response:
            if response.status == 404:
                raise GitHubNotFoundError(
                    f"no release {tag!r} in {repo} (or the token cannot see it)"
                )
            if response.status in (401, 403):
                raise GitHubAuthError(
                    f"GitHub refused the token ({response.status}): "
                    f"it needs Contents: Read on {repo}"
                )
            if response.status != 200:
                raise GitHubError(f"GitHub returned {response.status} for {url}")
            return await response.json()
    except aiohttp.ClientError as err:
        raise GitHubError(f"could not reach GitHub: {err}") from err


def pick_asset(release: dict[str, Any], suffix: str) -> dict[str, Any]:
    """Finds the one release asset whose name ends in `suffix`.

    Exactly one, or it is an error: a release carrying two candidate
    images is a broken release, and guessing which to flash is the last
    thing this should do.
    """
    found = [a for a in release.get("assets", []) if a["name"].endswith(suffix)]
    if len(found) != 1:
        raise GitHubError(
            f"expected exactly one {suffix} asset in "
            f"{release.get('tag_name')}, found {len(found)}"
        )
    return found[0]


async def download_asset(
    session: aiohttp.ClientSession, url: str, token: str
) -> bytes:
    """Fetches a release asset, dropping the token at the storage redirect.

    GitHub answers with a 302 to a signed storage URL. Carrying the
    Authorization header across is both needless and fatal -- the store
    rejects a request bearing two credentials -- so it is dropped the
    moment the host changes.
    """
    origin = urlsplit(url).netloc
    headers = _headers(token, "application/octet-stream")

    try:
        for _ in range(MAX_REDIRECTS):
            async with session.get(
                url, headers=headers, allow_redirects=False, timeout=TIMEOUT
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise GitHubError(f"redirect from {url} carried no Location")
                    url = location
                    if urlsplit(url).netloc != origin:
                        headers = {
                            k: v
                            for k, v in headers.items()
                            if k.lower() != "authorization"
                        }
                    continue
                if response.status != 200:
                    raise GitHubError(
                        f"asset download returned {response.status}"
                    )
                return await response.read()
    except aiohttp.ClientError as err:
        raise GitHubError(f"could not download the asset: {err}") from err

    raise GitHubError("too many redirects fetching the asset")


def parse_sig(text: str) -> tuple[str, str]:
    """Reads the sha256 and signature lines out of a .bin.sig asset."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        fields[key] = value.strip()

    if "sha256" not in fields or "signature" not in fields:
        raise GitHubError("the .sig asset has no sha256/signature lines")
    return fields["sha256"], fields["signature"]
