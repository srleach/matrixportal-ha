"""Constants for the MatrixPortal display OTA integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "matrixportal_ota"

CONF_REPO = "repo"
CONF_TOKEN = "token"
CONF_BASE_TOPIC = "base_topic"
CONF_HOST_OVERRIDE = "host_override"

DEFAULT_REPO = "srleach/matrixportal-m4-display"
DEFAULT_BASE_TOPIC = "matrixdisplay"

SERVICE_UPDATE = "update"
SERVICE_REFRESH_LATEST = "refresh_latest"

ATTR_TAG = "tag"
ATTR_ONLY = "only"
ATTR_DRY_RUN = "dry_run"

# One retained topic shared by every board: each board's update entity
# reads latest_version from it, so this is published once rather than
# per board, and a board that joins later still learns it.
LATEST_TOPIC = "{base}/firmware/latest"

# The board publishes its update entity but never subscribes to the
# install topic -- being told to install is no use to something that
# cannot fetch. Home Assistant listens instead and does the push.
INSTALL_TOPIC = "{base}/+/update/install"

# Matches the firmware's six-hourly expectation in docs/ota.md.
SCAN_INTERVAL = timedelta(hours=6)

# The model the firmware advertises over MQTT discovery, as built in
# MqttBridge.cpp: "MatrixPortal M4 (64x32)". Matched on the model rather
# than the entity name because two boards may well be called the same
# thing, which makes their entity ids ambiguous but leaves the model
# alone.
DEVICE_MODEL_MATCH = "MatrixPortal"
