"""Private, local snapshot storage and API for a Home Assistant timeline card."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change

from .const import (
    API_ROOT,
    ATTR_LABEL,
    CONF_DIRECTORY,
    CONF_RETENTION_DAYS,
    DEFAULT_DIRECTORY,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
    SERVICE_CAPTURE,
    SERVICE_CLEANUP,
)
from .snapshot import SNAPSHOT_RE, finalize_snapshot
from .event_label import delayed_image_label, detection_entity_for

_LOGGER = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_DIRECTORY, default=DEFAULT_DIRECTORY): cv.string,
                vol.Optional(
                    CONF_RETENTION_DAYS, default=DEFAULT_RETENTION_DAYS
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

CAPTURE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_LABEL, default="motion"): cv.string,
    }
)


def _safe_label(value: str) -> str:
    """Turn an automation-provided label into a safe filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
    return (cleaned or "event")[:40].lower()


def _snapshot_metadata(path: Path) -> dict[str, Any] | None:
    """Parse metadata only from files created by this integration."""
    match = SNAPSHOT_RE.fullmatch(path.name)
    if match is None:
        return None

    date = match.group("date")
    time = ":".join(
        (match.group("hour"), match.group("minute"), match.group("second"))
    )
    raw_label = match.group("label") or "event"
    label = raw_label.replace("_", " ").replace("-", " ").strip().title()
    stat = path.stat()

    return {
        "id": path.name,
        "file": path.name,
        "date": date,
        "time": time,
        "timestamp": f"{date}T{time}",
        "label": label,
        "size": stat.st_size,
        "url": f"{API_ROOT}/image/{quote(path.name, safe='')}",
    }


def _build_index(directory: Path, requested_date: str | None) -> dict[str, Any]:
    """Scan the snapshot directory and return the requested day's index."""
    all_items: list[dict[str, Any]] = []

    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            metadata = _snapshot_metadata(path)
        except OSError:
            continue
        if metadata is not None:
            all_items.append(metadata)

    all_items.sort(key=lambda item: (item["timestamp"], item["file"]))
    counts = Counter(item["date"] for item in all_items)
    category_counts = Counter()
    for item in all_items:
        label = item["label"].lower().replace("_", " ").replace("-", " ")
        if re.match(r"^(person|stranger)( |$)", label):
            category_counts["person"] += 1
        elif re.match(r"^package( |$)", label):
            category_counts["package"] += 1
    dates = [{"date": date, "count": counts[date]} for date in sorted(counts)]

    selected_date = requested_date
    if selected_date is None and dates:
        selected_date = dates[-1]["date"]

    images = (
        [item for item in all_items if item["date"] == selected_date]
        if selected_date
        else []
    )

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "selected_date": selected_date,
        "latest_date": dates[-1]["date"] if dates else None,
        "total_count": len(all_items),
        "counts": {
            "person": category_counts["person"],
            "package": category_counts["package"],
        },
        "dates": dates,
        "images": images,
    }


def _cleanup_files(directory: Path, retention_days: int) -> int:
    """Delete only recognized timeline files older than the retention period."""
    cutoff = datetime.now().timestamp() - retention_days * 86_400
    removed = 0

    for path in directory.iterdir():
        if not path.is_file() or SNAPSHOT_RE.fullmatch(path.name) is None:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as err:
            _LOGGER.warning("Could not remove old snapshot %s: %s", path, err)

    return removed


class TimelineIndexView(HomeAssistantView):
    """Return available dates and snapshot metadata to authenticated users."""

    url = API_ROOT
    name = "api:front_door_timeline:index"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, directory: Path) -> None:
        self._hass = hass
        self._directory = directory

    async def get(self, request: web.Request) -> web.Response:
        """Handle an authenticated index request."""
        requested_date = request.query.get("date")
        if requested_date and DATE_RE.fullmatch(requested_date) is None:
            raise web.HTTPBadRequest(text="date must use YYYY-MM-DD")

        payload = await self._hass.async_add_executor_job(
            _build_index, self._directory, requested_date
        )
        states = getattr(self._hass, "states", None)
        source_id = self._hass.data[DOMAIN].get("source_entity", "image.front_door_last_event")
        detection_id = detection_entity_for(source_id)
        source = states.get(source_id) if states is not None else None
        detection = states.get(detection_id) if states is not None and detection_id else None
        payload["source_status"] = {
            "image_entity": source_id,
            "image_updated": source.state if source else None,
            "detection_entity": detection_id,
            "detection_updated": detection.state if detection else None,
            "event_type": detection.attributes.get("event_type") if detection else None,
        }
        return web.json_response(payload)


class TimelineImageView(HomeAssistantView):
    """Serve a single snapshot only after Home Assistant authentication."""

    url = f"{API_ROOT}/image/{{filename}}"
    name = "api:front_door_timeline:image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, directory: Path) -> None:
        self._hass = hass
        self._directory = directory

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        """Return one recognized snapshot without allowing path traversal."""
        if Path(filename).name != filename or SNAPSHOT_RE.fullmatch(filename) is None:
            raise web.HTTPNotFound

        path = (self._directory / filename).resolve()
        if path.parent != self._directory or not await self._hass.async_add_executor_job(
            path.is_file
        ):
            raise web.HTTPNotFound

        return web.FileResponse(
            path,
            headers={"Cache-Control": "private, max-age=86400, immutable"},
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up storage, authenticated endpoints, services, and daily cleanup."""
    component_config = config[DOMAIN]
    directory = Path(component_config[CONF_DIRECTORY]).expanduser().resolve()
    retention_days = component_config[CONF_RETENTION_DAYS]

    await hass.async_add_executor_job(directory.mkdir, 0o755, True, True)
    capture_lock = asyncio.Lock()

    hass.data[DOMAIN] = {
        CONF_DIRECTORY: directory,
        CONF_RETENTION_DAYS: retention_days,
    }

    hass.http.register_view(TimelineIndexView(hass, directory))
    hass.http.register_view(TimelineImageView(hass, directory))

    async def _async_cleanup() -> int:
        removed = await hass.async_add_executor_job(
            _cleanup_files, directory, retention_days
        )
        if removed:
            _LOGGER.info("Removed %s expired front-door snapshot(s)", removed)
        return removed

    async def handle_capture(call: ServiceCall) -> None:
        entity_id = call.data[ATTR_ENTITY_ID]
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain not in {"camera", "image"}:
            raise HomeAssistantError(
                "front_door_timeline.capture requires a camera or image entity"
            )

        hass.data[DOMAIN]["source_entity"] = entity_id

        # Separate automations may call this service concurrently. Serialize the
        # complete capture/compare/commit operation so neither can save a copy.
        async with capture_lock:
            now = datetime.now().astimezone()
            label = _safe_label(call.data[ATTR_LABEL])
            # Older installed automations use image_update after 60 s. The current
            # bridge may deliver a valid HomeBase cover up to four minutes later.
            # Recover only a bounded, known classification; leave explicit labels
            # and unrelated/manual captures alone.
            detection_id = detection_entity_for(entity_id)
            states = getattr(hass, "states", None)
            if label == "image_update" and detection_id and states is not None:
                source = states.get(entity_id)
                detection = states.get(detection_id)
                if source is not None and detection is not None:
                    inferred = delayed_image_label(
                        source.state, detection.state,
                        detection.attributes.get("event_type"),
                    )
                    if inferred:
                        label = inferred
            filename = (
                f"front_door_{now:%Y-%m-%d_%H-%M-%S}-{now.microsecond:06d}_{label}.jpg"
            )
            destination = directory / filename
            pending = directory / f".front_door_pending_{uuid4().hex}.jpg"

            try:
                await hass.services.async_call(
                    entity_domain,
                    "snapshot",
                    {"filename": str(pending)},
                    blocking=True,
                    context=call.context,
                    target={ATTR_ENTITY_ID: entity_id},
                )
                try:
                    kept = await hass.async_add_executor_job(
                        finalize_snapshot, pending, destination
                    )
                except (OSError, ValueError) as err:
                    raise HomeAssistantError(str(err)) from err
            finally:
                # Also discard partial data if the upstream service raises.
                await hass.async_add_executor_job(pending.unlink, True)

            if kept != destination:
                _LOGGER.info(
                    "Skipped duplicate snapshot from %s (%s); image bytes are "
                    "unchanged from %s",
                    entity_id, label, kept.name,
                )
            else:
                _LOGGER.debug("Saved snapshot from %s to %s", entity_id, filename)

    async def handle_cleanup(call: ServiceCall) -> None:
        await _async_cleanup()

    async def handle_refresh_source(call: ServiceCall) -> None:
        """Use the SDK's supported refresh control without guessing a button ID."""
        source_id = call.data[ATTR_ENTITY_ID]
        registry = er.async_get(hass)
        source = registry.async_get(source_id)
        if not source_id.startswith("image.") or source is None or source.platform != "eufy_sdk" or not source.unique_id.endswith("_last_event"):
            raise HomeAssistantError("Select the Eufy SDK Last event image entity")
        unique_id = source.unique_id[:-len("_last_event")] + "_refresh_last_event"
        button_id = registry.async_get_entity_id("button", "eufy_sdk", unique_id)
        if not button_id:
            raise HomeAssistantError("Eufy SDK Refresh Last Event button was not found; check the integration version and enabled entities")
        await hass.services.async_call(
            "button", "press", {}, blocking=True, context=call.context,
            target={ATTR_ENTITY_ID: button_id},
        )

    hass.services.async_register(
        DOMAIN, SERVICE_CAPTURE, handle_capture, schema=CAPTURE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEANUP, handle_cleanup)
    hass.services.async_register(
        DOMAIN, "refresh_source", handle_refresh_source,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}),
    )

    async def schedule_cleanup(_now: datetime) -> None:
        await _async_cleanup()

    hass.data[DOMAIN]["unsub_cleanup"] = async_track_time_change(
        hass, schedule_cleanup, hour=3, minute=17, second=0
    )

    await _async_cleanup()
    return True

