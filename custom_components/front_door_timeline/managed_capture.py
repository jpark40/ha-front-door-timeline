"""Archive the Last event image for each relevant Eufy detection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Awaitable, Callable

from .event_label import DETECTION_LABELS


def _time(value: str | None) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _stamp(value: str | None) -> str | None:
    parsed = _time(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed else None


class ManagedCapture:
    """Use detection time/type as identity; image bytes are the snapshot source.

    A new detection must save the image currently available from the image
    entity, even if its timestamp or bytes have not changed. A later image
    update replaces that same event's file while the detection is still current.
    Image-only changes never create extra timeline events.
    """

    def __init__(
        self, source: str, *,
        load: Callable[[], Awaitable[dict | None]],
        save: Callable[[dict], Awaitable[None]],
        read_image: Callable[[], Awaitable[bytes]],
        read_detection: Callable[[], tuple[str | None, str | None]],
        capture: Callable[[bytes, str, datetime], Awaitable[dict]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self._load, self._save = load, save
        self._read_image, self._read_detection = read_image, read_detection
        self._capture = capture
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._last_detection: str | None = None
        self._last_label: str | None = None
        self._event: dict[str, Any] | None = None
        self._notified: set[tuple[str, str | None]] = set()
        self.status: dict[str, Any] = {
            "state": "starting", "message": "Starting automatic detection capture.",
        }

    @property
    def initialized(self) -> bool:
        return self._ready.is_set()

    def _record(self) -> dict[str, Any]:
        return {"source": self.source, "last_detection": self._last_detection,
                "last_label": self._last_label,
                "event": dict(self._event) if self._event else None,
                "status": dict(self.status)}

    def note_detection(self, timestamp: str | None, label: str | None) -> None:
        """Mark callbacks synchronously so startup cannot seed them as restored."""
        timestamp = _stamp(timestamp)
        if timestamp is not None:
            self._notified.add((timestamp, label))

    async def start(self) -> None:
        """Restore tracked intent; do not invent an event from restored HA state."""
        try:
            saved = await self._load()
            if isinstance(saved, dict) and saved.get("source") == self.source:
                self._last_detection = _stamp(saved.get("last_detection"))
                if isinstance(saved.get("status"), dict):
                    self.status = dict(saved["status"])
                # Migrate the waiting detections written by 1.0.12/1.0.13.
                event = saved.get("event") or saved.get("pending")
                if (isinstance(event, dict) and _time(event.get("timestamp"))
                        and event.get("label") in DETECTION_LABELS):
                    self._event = {"timestamp": _stamp(event["timestamp"]),
                                   "label": event["label"], "digest": event.get("digest")}
                self._last_label = saved.get("last_label", self.status.get("label"))
            timestamp, label = self._read_detection()
            key = (_stamp(timestamp), label)
            if (self._last_detection is None and key[0] is not None
                    and key not in self._notified):
                self._last_detection, self._last_label = key
                self.status = {"state": "restored_detection", "event_timestamp": key[0],
                               "label": label,
                               "message": "Waiting for a new detection; restored history was not captured again."}
            elif self._event is None and self.status.get("state") == "starting":
                self.status = {"state": "ready", "message": "Waiting for a Person, Stranger, or Package detection."}
            await self._save(self._record())
        except Exception as err:
            self.status = {"state": "journal_error", "message": str(err)}
        finally:
            self._ready.set()
        if self._event:
            await self.image_changed()

    async def detection_changed(
        self, timestamp: str | None, label: str | None, observed_at: datetime,
    ) -> None:
        """Save the available source image for each new relevant live detection."""
        detected = _time(timestamp)
        if detected is None:
            return
        timestamp = _stamp(timestamp)
        key = (timestamp, label)
        self.note_detection(timestamp, label)
        await self._ready.wait()
        eligible = False
        async with self._lock:
            self._notified.discard(key)
            if key == (self._last_detection, self._last_label):
                return
            previous = _time(self._last_detection)
            if previous is not None and detected < previous:
                return
            self._last_detection, self._last_label = key
            self._event = None
            fresh = -5 <= (observed_at - detected).total_seconds() <= 60
            if fresh and label in DETECTION_LABELS:
                self._event = {"timestamp": timestamp, "label": label, "digest": None}
                self.status = {"state": "capturing", "event_timestamp": timestamp,
                               "label": label, "message": "Saving the Last event image."}
                eligible = True
            elif fresh:
                self.status = {"state": "ignored_detection", "event_timestamp": timestamp,
                               "label": label, "message": "This detection type is not included in the timeline."}
            else:
                self.status = {"state": "restored_detection", "event_timestamp": timestamp,
                               "label": label, "message": "Restored detection; waiting for a new live detection."}
            await self._save(self._record())
        if eligible:
            await self.image_changed()

    def _is_current(self, event: dict[str, Any]) -> bool:
        timestamp, label = self._read_detection()
        return (_stamp(timestamp), label) == (event["timestamp"], event["label"])

    async def sync_current(self) -> dict[str, Any]:
        """Explicitly sync the displayed image with the current relevant detection."""
        await self._ready.wait()
        async with self._lock:
            timestamp, label = self._read_detection()
            timestamp = _stamp(timestamp)
            if timestamp is None or label not in DETECTION_LABELS:
                raise ValueError("No Person, Stranger, Package Delivered, or Package Taken detection is available")
            self._last_detection, self._last_label = timestamp, label
            self._event = {"timestamp": timestamp, "label": label, "digest": None}
            self.status = {"state": "capturing", "event_timestamp": timestamp,
                           "label": label, "message": "Syncing the Last event image."}
            await self._save(self._record())
        await self.image_changed()
        if (self.status.get("event_timestamp"), self.status.get("label")) != (timestamp, label):
            raise ValueError("The detection changed during sync; retry for the current event")
        if self.status.get("state") not in {"saved", "updated", "unchanged"}:
            raise ValueError(self.status.get("message", "The Last event image could not be saved"))
        return dict(self.status)

    async def image_changed(self) -> None:
        """Save or update one event file using the same image API as HA's proxy."""
        await self._ready.wait()
        async with self._lock:
            event = self._event
            if event is None or not self._is_current(event):
                return
            try:
                content = await self._read_image()
                if not content:
                    raise ValueError("The Last event image is empty or unavailable")
            except Exception as err:
                self.status = {"state": "image_unavailable", "message": str(err),
                               "event_timestamp": event["timestamp"], "label": event["label"]}
                await self._save(self._record())
                return
            # A newer detection can arrive while the image read is awaiting I/O.
            if not self._is_current(event):
                return
            digest = sha256(content).hexdigest()
            if (digest == event.get("digest")
                    and self.status.get("state") in {"saved", "updated", "unchanged"}):
                return
            try:
                result = await self._capture(content, event["label"], _time(event["timestamp"]))
            except Exception as err:
                self.status = {"state": "capture_error", "message": str(err),
                               "event_timestamp": event["timestamp"], "label": event["label"]}
                await self._save(self._record())
                return
            event["digest"] = digest
            self.status = {**result, "state": result["status"],
                           "event_timestamp": event["timestamp"], "label": event["label"],
                           "image_source": "event_image", "image_entity": self.source,
                           "captured_at": self._now().isoformat(),
                           "message": "Last event image saved for this detection."}
            await self._save(self._record())

    async def flush(self) -> None:
        """Persist event identity and the latest saved bytes on shutdown."""
        await self._ready.wait()
        async with self._lock:
            await self._save(self._record())
