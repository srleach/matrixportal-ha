# MatrixPortal Display OTA

A Home Assistant custom integration that updates the firmware on
[MatrixPortal M4 wall displays](https://github.com/srleach/matrixportal-m4-display)
over the air.

The boards cannot fetch their own firmware. The firmware repository is private,
and reaching it would mean a TLS stack, a current CA bundle and GitHub's redirects
on a WiFi co-processor running 2019 firmware — a great deal of fragile code
guarding the one step that must not fail. So Home Assistant does the fetching,
and pushes the image to the board over the LAN:

```
  GitHub (private)           Home Assistant                board
  ----------------           --------------                -----
  release assets  --HTTPS-->  this integration  --HTTP-->  POST /api/ota/prepare
   .bin + .bin.sig            (holds the token)            stage on QSPI
                                                           check signature
                                                 --HTTP-->  POST /api/ota/commit
                                                           write flash, reset
```

The token never leaves Home Assistant. The board never learns there is a
GitHub.

## Why this is safe without a password

`POST /api/ota/*` on each board is open to anything on the network, deliberately.
Every release is signed with an Ed25519 key whose public half is built into the
firmware, and a board refuses any image whose signature does not verify — so
this integration being wrong, or someone else running one like it, cannot flash
anything.

It does **not** stop someone on your network pushing an older, properly signed
release. There is no rollback protection.

## Install

### 1. HACS

HACS → ⋮ → **Custom repositories** → add `srleach/matrixportal-ha`, category
**Integration**. Then install **MatrixPortal Display OTA** and restart Home
Assistant.

This repository is public so that HACS can read it: HACS authenticates with a
GitHub device-OAuth token that carries no scopes, so it can see public
repositories and nothing else. Nothing here needs to be secret — there is no
key material, no token and no addresses in it, and the firmware repository it
fetches releases from stays private.

### 2. A GitHub token for the integration

A fine-grained personal access token with **Contents: Read** on the firmware
repository and nothing else. This is the one that reaches the private
repository, and it goes in the config flow below — never in this repository.

### 3. Add the integration

Settings → Devices & Services → **Add Integration** → MatrixPortal Display OTA.

| Field | Default | |
|---|---|---|
| Repository | `srleach/matrixportal-m4-display` | owner/name |
| GitHub token | — | validated against the API before the entry is created |
| MQTT base topic | `matrixdisplay` | must match the base topic in each display's web portal |

That is the whole setup. There is no YAML, no `packages:` include, no file to
`chmod 600`, and no list of addresses to maintain.

## How it finds the displays

Every board already publishes its own address as a diagnostic sensor, so the
host list is derived from the devices Home Assistant can see. A board that
changes address is followed; a new board joins on its own once it appears over
MQTT.

The match is on the device **model** (`MatrixPortal M4 (64x32)`) rather than the
entity name, because two boards may well be called the same thing — which makes
their entity ids ambiguous but leaves the model alone.

To pin the list by hand — to reach a board that has dropped off MQTT, or to
update one deliberately while leaving the others alone — set the space-separated
override in the integration's **Configure** dialog. Leave it empty otherwise.

## Using it

### The Install button

Each board advertises a **Firmware** update entity over MQTT discovery, so they
appear in Settings → Updates by themselves. Pressing Install publishes to
`matrixdisplay/<id>/update/install`; the board does not subscribe to that — being
told to install is no use to something that cannot fetch — so this integration
picks it up and runs the push.

`latest_version` comes from `matrixdisplay/firmware/latest`, one retained topic
shared by every board, refreshed every six hours and at startup.

### By hand

```yaml
action: matrixportal_ota.update
data:
  tag: latest        # or v0.3.2
  only: ""           # or matrix-a1b2c3 for one board
  dry_run: false
```

It skips boards already on the release, and returns a response describing what
happened:

```yaml
tag: v0.3.2
dry_run: false
updated: [matrix-a1b2c3]
failed: []
skipped: [matrix-cccccc]
unreachable: []
log: [...]
```

A board that is switched off or has dropped off the network is reported, never
passed over silently: that is the one case where saying nothing is worst — the
update looks like it worked and nobody learns that a display is still on the old
firmware.

### Rolling back

There is no automatic rollback. The application region holds one image and there
is no room for two. Rolling back means pushing the previous release the same way:

```yaml
action: matrixportal_ota.update
data: { tag: v0.3.1, only: matrix-a1b2c3 }
```

A board that boots but misbehaves is recoverable from Home Assistant. A board
that does not boot at all needs a cable — the bootloader below `0x4000` is never
written, so it still comes up as the `MATRIXBOOT` drive over USB.

### Services

| Service | What it does |
|---|---|
| `matrixportal_ota.update` | Fetch a release and push it to the displays that need it |
| `matrixportal_ota.refresh_latest` | Re-read the newest tag and republish the retained topic |

## Requirements

- Home Assistant 2024.5 or newer, with the MQTT integration set up
- Firmware **v0.2.0 or later** on every board

Boards on v0.1.x have no `/api/ota/*` endpoints and no trusted signing key. They
need one flash over USB before they can ever be updated over the air.

## It still needs retrying

Roughly one transfer in ten stalls partway and is started over, up to three
times. The cause is a buffer limit in the WiFi co-processor's TCP stack:
ESP-IDF lwip's default `TCP_SND_BUF` is 5744 bytes, and overrunning it wedges
the socket mid-transfer, so the image is sent in 4096-byte pieces. It is
understood well enough to work around and not well enough to fix; newer
co-processor firmware is the next thing worth trying.

Nothing is risked by a failed transfer. The staging area is erased again on the
next prepare, and the internal flash is not touched until the whole image has
been hashed and its signature checked.

## Replaces

The `extra/home-assistant/matrix_ota.yaml` package in the firmware repository.
Both do the same job; this one does it without a `shell_command`, a template
sensor, a token file, or a `packages:` include. If you have the package
installed, remove it before adding this — otherwise both will answer the same
Install button.

## Licence

MIT.
