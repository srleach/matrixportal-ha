"""The board side of an update: prepare, chunk, seal, commit.

An async port of `extra/home-assistant/matrix_ota_push.py` from the
firmware repository. Every step and every timeout here exists because of
something the hardware does; see docs/ota.md for the measurements behind
them.

    POST /api/ota/prepare    X-OTA-Size, X-OTA-Sha256, X-OTA-Signature
    POST /api/ota/chunk      X-OTA-Offset, 4 KB of image      (repeated)
    POST /api/ota/seal       hash, signature, vector table
    POST /api/ota/commit     write flash and reset

Nothing in steps 1-3 can hurt a board. Only commit can, and it takes
about three seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

# How much of the image one request carries.
#
# ESP-IDF lwip's default TCP_SND_BUF is 5744 bytes -- four 1436-byte
# segments -- and overrunning it wedges the AirLift's socket mid
# transfer. Measured over forty transfers of a ~380 KB image, counting
# those that needed no retry at all:
#
#     8192   10 of 16     above the buffer
#     4096   15 of 16     below it
#     2048   14 of 16     below it
#
# Both sizes below the buffer behave the same within the noise of a
# sample that small, and both beat one above it. 4096 is the faster of
# the two -- half as many requests as 2048 -- so it is the one chosen.
CHUNK = 4096

# Claiming and erasing the staging area takes about four seconds, and
# the board cannot read its socket while it runs -- hence the long
# prepare timeout against an empty body.
PREPARE_TIMEOUT = aiohttp.ClientTimeout(total=90)
CHUNK_TIMEOUT = aiohttp.ClientTimeout(total=60)
SEAL_TIMEOUT = aiohttp.ClientTimeout(total=120)
COMMIT_TIMEOUT = aiohttp.ClientTimeout(total=30)
STATUS_TIMEOUT = aiohttp.ClientTimeout(total=10)

Logger = Callable[[str], None]


class _HttpError(Exception):
    """A non-2xx from a board, keeping the body for the 409 case."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


async def _post(
    session: aiohttp.ClientSession,
    host: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    timeout: aiohttp.ClientTimeout,
) -> dict:
    async with session.post(
        f"http://{host}{path}", data=body, headers=headers, timeout=timeout
    ) as response:
        raw = await response.read()
        if response.status >= 400:
            raise _HttpError(response.status, raw)
        return json.loads(raw) if raw else {}


async def board_status(
    session: aiohttp.ClientSession, host: str
) -> dict | None:
    """Asks a board what it is and what it is running, or None if silent."""
    try:
        async with session.get(
            f"http://{host}/api/status", timeout=STATUS_TIMEOUT
        ) as response:
            if response.status != 200:
                return None
            # The board labels this as JSON, but do not insist on it.
            return json.loads(await response.read())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("%s: unreachable (%s)", host, type(err).__name__)
        return None


async def push(
    session: aiohttp.ClientSession,
    host: str,
    image: bytes,
    sha: str,
    sig: str,
    say: Logger,
    attempts: int = 3,
) -> bool:
    """Sends the image, starting over if a transfer gets stuck.

    Some transfers stall partway, but a board that has just failed one
    succeeds on the next: the failure is per transfer, not a state the
    board stays in. Starting over is therefore worth far more than
    retrying a piece. Nothing is risked by it -- the staging area is
    erased again on the next prepare, and the internal flash is untouched
    until the whole image has been hashed and its signature checked.
    """
    for attempt in range(1, attempts + 1):
        if await _push_once(session, host, image, sha, sig, say):
            return True
        if attempt < attempts:
            say(f"  {host}: transfer {attempt} did not complete, starting over")
            await asyncio.sleep(5)
    return False


async def _push_once(
    session: aiohttp.ClientSession,
    host: str,
    image: bytes,
    sha: str,
    sig: str,
    say: Logger,
) -> bool:
    # Prepare first, on its own. Done as part of an upload, the sender
    # pushes the whole image into a board that is not listening; done
    # with an empty body, nothing is in flight to lose.
    try:
        await _post(
            session,
            host,
            "/api/ota/prepare",
            b"",
            {
                "X-OTA-Size": str(len(image)),
                "X-OTA-Sha256": sha,
                "X-OTA-Signature": sig,
            },
            PREPARE_TIMEOUT,
        )
    except _HttpError as err:
        say(
            f"  {host}: refused -- {err.status} "
            f"{err.body.decode('utf-8', 'replace')[:200]}"
        )
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        say(f"  {host}: prepare failed ({type(err).__name__})")
        return False

    offset = 0
    stalled = 0
    while offset < len(image):
        piece = image[offset : offset + CHUNK]
        try:
            body = await _post(
                session,
                host,
                "/api/ota/chunk",
                piece,
                {"X-OTA-Offset": str(offset)},
                CHUNK_TIMEOUT,
            )
            offset = body.get("received", offset + len(piece))
            stalled = 0
        except _HttpError as err:
            # A 409 carries where the board actually got to, so a piece
            # that half arrived is picked up from there rather than
            # guessed at.
            where = None
            try:
                where = json.loads(err.body).get("received")
            except (ValueError, AttributeError):
                pass
            if err.status == 409 and isinstance(where, int):
                offset = where
                continue
            say(f"  {host}: chunk at {offset} refused -- {err.status}")
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            # A piece that stalls is worth a couple of goes -- the board
            # says where it got to, so nothing is sent twice -- but a
            # transfer that keeps stalling is better restarted than
            # nursed.
            stalled += 1
            if stalled > 3:
                say(
                    f"  {host}: stuck at {offset}/{len(image)} "
                    f"({type(err).__name__})"
                )
                return False
            await asyncio.sleep(1)

    # Seal is separate from the last piece so a sender can retry pieces
    # freely without the board trying to verify half an image.
    try:
        body = await _post(session, host, "/api/ota/seal", b"", {}, SEAL_TIMEOUT)
    except _HttpError as err:
        say(
            f"  {host}: refused -- {err.status} "
            f"{err.body.decode('utf-8', 'replace')[:200]}"
        )
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        say(f"  {host}: seal failed ({type(err).__name__})")
        return False

    if not body.get("ota", {}).get("staged"):
        say(f"  {host}: staged but not verified -- {body}")
        return False

    try:
        await _post(session, host, "/api/ota/commit", b"", {}, COMMIT_TIMEOUT)
    except (_HttpError, aiohttp.ClientError, asyncio.TimeoutError):
        # The board resets mid-reply often enough that a dropped
        # connection here is the normal case, not a failure. The version
        # check that follows is what decides.
        pass
    return True


async def wait_for(
    session: aiohttp.ClientSession, host: str, want: str, seconds: int = 90
) -> bool:
    """Waits for a board to come back up on the version we just sent."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        status = await board_status(session, host)
        if status and status.get("fw") == want:
            return True
    return False
