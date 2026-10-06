"""Private, local snapshot storage and API for a Home Assistant timeline card."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
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

_LOGGER = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SNAPSHOT_RE = re.compile(
    r"^front_door_"
    r"(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})"
    r"(?:-(?P<fraction>\d{1,6}))?"
    r"(?:_(?P<label>[A-Za-z0-9_-]+))?"
    r"\.(?P<extension>jpe?g|png)$",
    re.IGNORECASE,
)

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

        now = datetime.now().astimezone()
        label = _safe_label(call.data[ATTR_LABEL])
        filename = (
            f"front_door_{now:%Y-%m-%d_%H-%M-%S}-{now.microsecond:06d}_{label}.jpg"
        )
        destination = directory / filename

        await hass.services.async_call(
            entity_domain,
            "snapshot",
            {"filename": str(destination)},
            blocking=True,
            context=call.context,
            target={ATTR_ENTITY_ID: entity_id},
        )

        def _valid_snapshot() -> bool:
            try:
                return destination.is_file() and destination.stat().st_size > 0
            except OSError:
                return False

        if not await hass.async_add_executor_job(_valid_snapshot):
            raise HomeAssistantError(
                f"Snapshot action completed but no image was written to {destination}"
            )

    async def handle_cleanup(call: ServiceCall) -> None:
        await _async_cleanup()

    hass.services.async_register(
        DOMAIN, SERVICE_CAPTURE, handle_capture, schema=CAPTURE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEANUP, handle_cleanup)

    async def schedule_cleanup(_now: datetime) -> None:
        await _async_cleanup()

    hass.data[DOMAIN]["unsub_cleanup"] = async_track_time_change(
        hass, schedule_cleanup, hour=3, minute=17, second=0
    )

    await _async_cleanup()
    return True
