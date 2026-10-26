"""Private, local snapshot storage and API for a Home Assistant timeline card."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.image import async_get_image
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store

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
from .managed_capture import ManagedCapture

_LOGGER = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REFRESH_SYNC_TIMEOUT_SECONDS = 20

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_DIRECTORY, default=DEFAULT_DIRECTORY): cv.string,
                vol.Optional(
                    CONF_RETENTION_DAYS, default=DEFAULT_RETENTION_DAYS
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
                vol.Optional("auto_capture", default=True): cv.boolean,
                vol.Optional("source_entity", default="image.front_door_last_event"): cv.entity_id,
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


def _detection_entity_for(hass: HomeAssistant, image_entity: str) -> str | None:
    """Resolve the SDK device pair even when HA has renamed an entity."""
    if image_entity.startswith("image."):
        registry = er.async_get(hass)
        source = registry.async_get(image_entity)
        if source and source.platform == "eufy_sdk" and source.unique_id.endswith("_last_event"):
            unique_id = source.unique_id[:-len("_last_event")] + "_detection_event"
            detection_id = registry.async_get_entity_id("event", "eufy_sdk", unique_id)
            if detection_id:
                return detection_id
    return detection_entity_for(image_entity)


def _source_status(hass: HomeAssistant, source_id: str) -> dict[str, Any]:
    """Report entity metadata without downloading or refreshing an image."""
    detection_id = _detection_entity_for(hass, source_id)
    source = hass.states.get(source_id)
    detection = hass.states.get(detection_id) if detection_id else None
    kind = detection.attributes.get("event_type") if detection else None
    result = {
        "image_entity": source_id,
        "image_updated": source.state if source else None,
        "detection_entity": detection_id,
        "detection_updated": detection.state if detection else None,
        "event_type": kind,
        "matched_label": delayed_image_label(source.state, detection.state, kind)
        if source and detection else None,
    }
    monitor = hass.data[DOMAIN].get("managed_capture")
    if monitor is not None and monitor.source == source_id:
        result["capture"] = dict(monitor.status)
    return result


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


def _build_index(
    directory: Path, requested_date: str | None, category: str | None = None,
) -> dict[str, Any]:
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
    category_dates: dict[str, Counter] = {"person": Counter(), "package": Counter()}
    for item in all_items:
        label = item["label"].lower().replace("_", " ").replace("-", " ")
        item_category = None
        if re.match(r"^(person|stranger)( |$)", label):
            item_category = "person"
        elif re.match(r"^package( |$)", label):
            item_category = "package"
        if item_category:
            category_counts[item_category] += 1
            category_dates[item_category][item["date"]] += 1
    dates = [{"date": date, "count": counts[date], "counts": {
        name: date_counts[date] for name, date_counts in category_dates.items()
    }} for date in sorted(counts)]
    latest_dates = {name: max(date_counts, default=None)
                    for name, date_counts in category_dates.items()}
    latest_date = (latest_dates[category] if category in latest_dates
                   else dates[-1]["date"] if dates else None)

    selected_date = requested_date if requested_date is not None else latest_date

    images = (
        [item for item in all_items if item["date"] == selected_date]
        if selected_date
        else []
    )

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "selected_date": selected_date,
        "latest_date": latest_date,
        "latest_dates": latest_dates,
        "total_count": len(all_items),
        "hidden_count": len(all_items) - sum(category_counts.values()),
        "label_counts": dict(Counter(item["label"] for item in all_items)),
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
        category = request.query.get("category")
        if category is not None and category not in {"person", "package"}:
            raise web.HTTPBadRequest(text="category must be person or package")

        payload = await self._hass.async_add_executor_job(
            _build_index, self._directory, requested_date, category
        )
        source_id = self._hass.data[DOMAIN].get("source_entity", "image.front_door_last_event")
        payload["source_status"] = _source_status(self._hass, source_id)
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
        "source_entity": component_config.get("source_entity", "image.front_door_last_event"),
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

    async def _async_capture(
        entity_id: str, requested_label: str, context: Any,
        image_time: datetime | None = None,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain not in {"camera", "image"}:
            raise HomeAssistantError(
                "front_door_timeline.capture requires a camera or image entity"
            )

        hass.data[DOMAIN]["source_entity"] = entity_id

        # Separate automations may call this service concurrently. Serialize the
        # complete capture/compare/commit operation so neither can save a copy.
        async with capture_lock:
            local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
            now = (image_time.astimezone(ZoneInfo(local_zone)) if image_time and local_zone
                   else image_time.astimezone() if image_time else datetime.now().astimezone())
            label = _safe_label(requested_label)
            # Older installed automations use image_update after 60 s. The current
            # bridge may deliver a valid HomeBase cover up to four minutes later.
            # Recover only a bounded, known classification; leave explicit labels
            # and unrelated/manual captures alone.
            detection_id = _detection_entity_for(hass, entity_id)
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
            already_existed = await hass.async_add_executor_job(destination.is_file)
            pending = directory / f".front_door_pending_{uuid4().hex}.jpg"

            try:
                if image_bytes is not None:
                    await hass.async_add_executor_job(pending.write_bytes, image_bytes)
                else:
                    await hass.services.async_call(
                        entity_domain,
                        "snapshot",
                        {"filename": str(pending)},
                        blocking=True,
                        context=context,
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
            return {
                "status": "saved" if kept == destination and not already_existed else "unchanged",
                "file": kept.name,
                "label": label,
                "image_timestamp": image_time.isoformat() if image_time and image_bytes is None else None,
                "event_timestamp": image_time.isoformat() if image_time and image_bytes is not None else None,
            }

    async def handle_capture(call: ServiceCall) -> None:
        await _async_capture(call.data[ATTR_ENTITY_ID], call.data[ATTR_LABEL], call.context)

    async def handle_cleanup(call: ServiceCall) -> None:
        await _async_cleanup()

    async def handle_diagnostics(call: ServiceCall) -> dict[str, Any]:
        """Inspect retained files and SDK entities without changing either."""
        source_id = call.data.get(ATTR_ENTITY_ID) or hass.data[DOMAIN].get(
            "source_entity", "image.front_door_last_event"
        )
        storage: dict[str, Any] = {
            "directory": str(directory), "retention_days": retention_days,
        }
        try:
            index = await hass.async_add_executor_job(_build_index, directory, None)
            for key in ("total_count", "counts", "hidden_count", "label_counts",
                        "latest_dates", "dates"):
                storage[key] = index[key]
            storage["latest_day_snapshots"] = [
                {key: item[key] for key in ("file", "date", "time", "label")}
                for item in index["images"][-5:]
            ]
        except OSError as err:
            storage["error"] = str(err)
        registry = er.async_get(hass)
        candidates = []
        for entry in registry.entities.values():
            if (entry.platform != "eufy_sdk" or not entry.entity_id.startswith("image.")
                    or not entry.unique_id.endswith("_last_event")):
                continue
            state = hass.states.get(entry.entity_id)
            candidates.append({"entity_id": entry.entity_id,
                               "state": state.state if state else None})
        return {"storage": storage, "source": _source_status(hass, source_id),
                "eufy_image_entities": sorted(candidates, key=lambda item: item["entity_id"])}

    async def handle_refresh_source(call: ServiceCall) -> dict[str, Any]:
        """Request a refresh AND archive the image; do not depend on an automation edge."""
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
        explicit_label = call.data.get(ATTR_LABEL)
        if explicit_label and not re.match(r"^(person|stranger|package)(_|-|$)", _safe_label(explicit_label)):
            raise HomeAssistantError("Choose a person or package label for the visible timeline tabs")
        states = hass.states
        detection_id = _detection_entity_for(hass, source_id)
        deadline = asyncio.get_running_loop().time() + REFRESH_SYNC_TIMEOUT_SECONDS
        last_state = None
        result = None
        classification_missing = False
        while True:
            image = states.get(source_id)
            source_state = image.state if image is not None else None
            try:
                image_time = datetime.fromisoformat(source_state.replace("Z", "+00:00"))
                if image_time.tzinfo is None:
                    image_time = None
            except (ValueError, TypeError, AttributeError):
                image_time = None
            if image_time is not None and source_state != last_state:
                last_state = source_state
                detection = states.get(detection_id) if detection_id else None
                label = _safe_label(explicit_label) if explicit_label else (
                    delayed_image_label(source_state, detection.state,
                                        detection.attributes.get("event_type"))
                    if detection is not None else None
                )
                classification_missing = label is None
                if label:
                    result = await _async_capture(source_id, label, call.context, image_time)
                    if result["status"] == "saved":
                        return result
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)
        if classification_missing:
            raise HomeAssistantError(
                f"Eufy image timestamp is {last_state}, but no matching person/package detection "
                "metadata is available. Specify label: person or label: package only if you "
                "have verified what the image shows. No unclassified timeline entry was added."
            )
        if result is not None:
            raise HomeAssistantError(
                f"Eufy supplied no newer image during the refresh wait (source timestamp: {last_state}). "
                f"The timeline already contains the same image in {result['file']}. "
                "A successful bridge request does not mean a fresh cover was delivered."
            )
        raise HomeAssistantError(
            f"No usable Eufy Last event image arrived (state: {last_state or source_state}). "
            "Check the Eufy SDK Bridge event-image/cover log."
        )

    hass.services.async_register(
        DOMAIN, SERVICE_CAPTURE, handle_capture, schema=CAPTURE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEANUP, handle_cleanup)
    hass.services.async_register(
        DOMAIN, "diagnostics", handle_diagnostics,
        schema=vol.Schema({vol.Optional(ATTR_ENTITY_ID): cv.entity_id}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "refresh_source", handle_refresh_source,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id,
                           vol.Optional(ATTR_LABEL): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    if component_config.get("auto_capture", True):
        source_id = hass.data[DOMAIN]["source_entity"]
        journal = Store(hass, 1, f"{DOMAIN}.managed_capture")

        def read_detection() -> tuple[str | None, str | None]:
            detection_id = _detection_entity_for(hass, source_id)
            state = hass.states.get(detection_id) if detection_id else None
            return (state.state, state.attributes.get("event_type")) if state else (None, None)

        async def read_image() -> bytes:
            entry = er.async_get(hass).async_get(source_id)
            state = hass.states.get(source_id)
            if (entry is None or entry.platform != "eufy_sdk"
                    or not entry.unique_id.endswith("_last_event")
                    or state is None or state.state in {"unknown", "unavailable"}):
                raise HomeAssistantError("Automatic capture is waiting for the Eufy SDK Last event image")
            return bytes((await async_get_image(hass, source_id)).content)

        async def capture_bytes(content: bytes, label: str, detected: datetime) -> dict:
            return await _async_capture(source_id, label, None, detected, content)

        async def refresh_image() -> None:
            registry = er.async_get(hass)
            entry = registry.async_get(source_id)
            if (entry is None or entry.platform != "eufy_sdk"
                    or not entry.unique_id.endswith("_last_event")):
                raise HomeAssistantError("Eufy SDK source entity is unavailable")
            unique_id = entry.unique_id[:-len("_last_event")] + "_refresh_last_event"
            button_id = registry.async_get_entity_id("button", "eufy_sdk", unique_id)
            if not button_id:
                raise HomeAssistantError("Eufy SDK Refresh Last event button is unavailable")
            await hass.services.async_call(
                "button", "press", {}, blocking=True,
                target={ATTR_ENTITY_ID: button_id},
            )

        monitor = ManagedCapture(
            source_id, load=journal.async_load, save=journal.async_save,
            read_image=read_image, read_detection=read_detection,
            capture=capture_bytes, refresh=refresh_image,
        )
        hass.data[DOMAIN]["managed_capture"] = monitor
        remove_listener = None
        jobs: set[asyncio.Task] = set()

        def run_job(coro) -> None:
            async def guarded() -> None:
                try:
                    await coro
                except Exception as err:
                    monitor.status = {"state": "journal_error", "message": str(err)}
                    _LOGGER.exception("Automatic front-door capture failed")
            task = hass.async_create_task(guarded())
            jobs.add(task)
            task.add_done_callback(jobs.discard)

        @callback
        def source_changed(event) -> None:
            state = event.data.get("new_state")
            if state is None:
                return
            entity_id = event.data["entity_id"]
            old = event.data.get("old_state")
            if old is not None and old.state == state.state:
                return
            if entity_id == source_id:
                run_job(monitor.image_changed())
            else:
                run_job(monitor.detection_changed(
                    state.state, state.attributes.get("event_type"), datetime.now(timezone.utc)
                ))

        async def start_monitor(_event=None) -> None:
            nonlocal remove_listener
            await monitor.start()
            detection_id = _detection_entity_for(hass, source_id)
            watched = [source_id] + ([detection_id] if detection_id else [])
            remove_listener = async_track_state_change_event(hass, watched, source_changed)

        async def stop_monitor(_event) -> None:
            if remove_listener:
                remove_listener()
            if monitor.initialized:
                await monitor.flush()
            for job in list(jobs):
                job.cancel()

        if hass.is_running:
            await start_monitor()
        else:
            hass.bus.async_listen_once("homeassistant_started", start_monitor)
        hass.bus.async_listen_once("homeassistant_stop", stop_monitor)

    async def schedule_cleanup(_now: datetime) -> None:
        await _async_cleanup()

    hass.data[DOMAIN]["unsub_cleanup"] = async_track_time_change(
        hass, schedule_cleanup, hour=3, minute=17, second=0
    )

    await _async_cleanup()
    return True

