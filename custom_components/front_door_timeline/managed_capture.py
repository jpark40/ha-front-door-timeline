"""Keep a detection's metadata until a changed event image is available."""

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
    """Persist a byte baseline and the latest live detection, not a fetch-time guess.

    The SDK assigns its image state when it downloads a picture, including a
    retained picture after restart. Only a content change from the baseline
    observed for a live detection makes that detection eligible for capture.
    Any subsequent detection, including a type outside the visible tabs,
    supersedes the pending metadata. All adapters run on HA's event loop.
    """

    def __init__(
        self, source: str, *,
        load: Callable[[], Awaitable[dict | None]],
        save: Callable[[dict], Awaitable[None]],
        read_image: Callable[[], Awaitable[bytes]],
        read_detection: Callable[[], tuple[str | None, str | None]],
        capture: Callable[[bytes, str, datetime], Awaitable[dict]],
        refresh: Callable[[], Awaitable[None]],
        now: Callable[[], datetime] | None = None,
        fallback: Callable[[], Awaitable[tuple[bytes, str]]] | None = None,
        fallback_delay: float = 2,
    ) -> None:
        self.source = source
        self._load = load
        self._save = save
        self._read_image = read_image
        self._read_detection = read_detection
        self._capture = capture
        self._refresh = refresh
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._fallback = fallback
        self._fallback_delay = fallback_delay
        self._fallback_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._digest: str | None = None
        self._last_detection: str | None = None
        self._pending: dict[str, Any] | None = None
        self._recent_previous_digest: str | None = None
        self._changed_at: datetime | None = None
        self._refreshing = False
        self._seeding = True
        self._notified_detections: set[str] = set()
        self.status: dict[str, Any] = {
            "state": "starting", "message": "Starting automatic detection capture.",
        }

    @property
    def initialized(self) -> bool:
        return self._ready.is_set()

    def _record(self) -> dict[str, Any]:
        return {"source": self.source, "digest": self._digest,
                "last_detection": self._last_detection,
                "pending": dict(self._pending) if self._pending else None,
                "status": dict(self.status)}

    def _waiting(self) -> None:
        if self._pending:
            details = ({key: self.status[key] for key in
                        ("refresh_error", "fallback_error", "fallback_state")
                        if key in self.status} if
                       self.status.get("event_timestamp") == self._pending["timestamp"] else {})
            self.status = {
                "state": "waiting_for_image",
                "event_timestamp": self._pending["timestamp"],
                "label": self._pending["label"],
                "message": "Detection received; Eufy has not supplied a changed event image.",
                **details,
            }

    async def start(self) -> None:
        """Restore the pending event before handling queued state changes."""
        try:
            saved = await self._load()
            if isinstance(saved, dict) and saved.get("source") == self.source:
                self._digest = saved.get("digest")
                self._last_detection = _stamp(saved.get("last_detection"))
                pending = saved.get("pending")
                if (isinstance(pending, dict) and _time(pending.get("timestamp"))
                        and pending.get("label") in DETECTION_LABELS):
                    self._pending = dict(pending)
                    self._pending["timestamp"] = _stamp(pending["timestamp"])
                if isinstance(saved.get("status"), dict):
                    self.status = dict(saved["status"])
                if self._pending and self.status.get("fallback_state") == "capturing":
                    self.status.update(
                        fallback_state="interrupted",
                        fallback_error="Live capture was interrupted by restart; waiting for an event image or a new detection.",
                    )
        except Exception as err:
            self.status = {"state": "journal_error", "message": str(err)}
        finally:
            self._ready.set()
        try:
            await self.image_changed()
        finally:
            self._seeding = False
        if self._pending:
            timestamp = self._pending["timestamp"]
            await self._refresh_once(timestamp)
            await self._try_fallback(timestamp)

    async def _refresh_once(self, timestamp: str) -> None:
        """Coalesce a burst of detections into one supported bridge request."""
        if (self._refreshing or self._pending is None
                or self._pending["timestamp"] != timestamp):
            return
        self._refreshing = True
        try:
            await self._refresh()
        except Exception as err:
            async with self._lock:
                if self._pending and self._pending["timestamp"] == timestamp:
                    self._waiting()
                    self.status["refresh_error"] = str(err)
                    await self._save(self._record())
        finally:
            self._refreshing = False
        await self.image_changed()

    async def detection_changed(
        self, timestamp: str | None, label: str | None, observed_at: datetime,
    ) -> None:
        """Freeze a new live event; restored entity states are not new events."""
        detected = _time(timestamp)
        if detected is None:
            return
        timestamp = _stamp(timestamp)
        # A callback can arrive while startup is still loading the journal.
        # Do not then mistake that callback's state for restored metadata when
        # seeding the first image baseline.
        self._notified_detections.add(timestamp)
        await self._ready.wait()
        fresh = -5 <= (observed_at - detected).total_seconds() <= 60
        request_refresh = False
        async with self._lock:
            if timestamp == self._last_detection:
                self._notified_detections.discard(timestamp)
                return
            previous = _time(self._last_detection)
            if previous is not None and detected < previous:
                self._notified_detections.discard(timestamp)
                return  # An older restored state must not replace persisted intent.
            self._last_detection = timestamp
            self._notified_detections.discard(timestamp)
            self._pending = None
            if fresh and label in DETECTION_LABELS:
                baseline = self._digest
                if (self._changed_at is not None and self._recent_previous_digest is not None
                        and -5 <= (self._changed_at - detected).total_seconds() <= 5
                        and 0 <= (observed_at - self._changed_at).total_seconds() <= 5):
                    baseline = self._recent_previous_digest
                self._pending = {"timestamp": timestamp, "label": label,
                                 "baseline": baseline}
                self._waiting()
                request_refresh = True
            elif fresh:
                self.status = {"state": "ignored_detection", "event_timestamp": timestamp,
                               "label": label,
                               "message": "The latest detection is outside Person and Package."}
            else:
                self.status = {"state": "restored_detection", "event_timestamp": timestamp,
                               "label": label,
                               "message": "Restored detection metadata; waiting for a new live detection."}
            await self._save(self._record())

        # Read a baseline first if no image has been available since setup. Never
        # hold the state lock while the bridge's refresh request is in flight.
        await self.image_changed()
        if request_refresh:
            await self._refresh_once(timestamp)
            await self._try_fallback(timestamp)

    def _current_live_pending(self, timestamp: str) -> bool:
        """A current-scene frame must never be filed as a historical detection."""
        if not self._pending or self._pending["timestamp"] != timestamp:
            return False
        detected = _time(timestamp)
        current, label = self._read_detection()
        return (detected is not None and 0 <= (self._now() - detected).total_seconds() <= 60
                and _stamp(current) == timestamp and label == self._pending["label"])

    async def _try_fallback(self, timestamp: str) -> None:
        """Take at most one live frame per new event, without blocking image updates."""
        if self._fallback is None or not self._current_live_pending(timestamp):
            return
        await asyncio.sleep(self._fallback_delay)
        await self.image_changed()
        async with self._fallback_lock:
            async with self._lock:
                if (not self._current_live_pending(timestamp)
                        or self._pending.get("fallback_attempted")):
                    return
                self._pending["fallback_attempted"] = True
                self._waiting()
                self.status["fallback_state"] = "capturing"
                await self._save(self._record())
            try:
                content, camera = await self._fallback()
                if not content:
                    raise ValueError("The live video source returned an empty image")
            except Exception as err:
                async with self._lock:
                    if self._current_live_pending(timestamp):
                        self._waiting()
                        self.status.update(fallback_state="failed", fallback_error=str(err))
                        await self._save(self._record())
                return
            async with self._lock:
                # A primary image or a newer detection may have arrived during
                # stream startup. Discard these bytes rather than guess a label.
                if not self._current_live_pending(timestamp):
                    if self._pending and self._pending["timestamp"] == timestamp:
                        self._waiting()
                        self.status.update(fallback_state="expired",
                                           fallback_error="Live frame arrived more than 60 seconds after detection; it was not archived as that event.")
                        await self._save(self._record())
                    return
                pending = self._pending
                if sha256(content).hexdigest() in {self._digest, pending.get("baseline")}:
                    self._waiting()
                    self.status.update(fallback_state="unchanged",
                                       fallback_error="The live source returned the same image bytes; no duplicate was saved.")
                    await self._save(self._record())
                    return
                try:
                    result = await self._capture(content, pending["label"], _time(timestamp))
                except Exception as err:
                    self.status = {"state": "capture_error", "message": str(err),
                                   "event_timestamp": timestamp, "label": pending["label"],
                                   "image_source": "live_stream", "camera_entity": camera}
                    await self._save(self._record())
                    return
                self._pending = None
                self.status = {**result, "state": result["status"],
                               "event_timestamp": timestamp, "label": pending["label"],
                               "image_source": "live_stream", "camera_entity": camera,
                               "captured_at": self._now().isoformat(),
                               "message": "Saved a live camera frame after detection." if result["status"] == "saved"
                               else "The same live frame is already saved for this category."}
                await self._save(self._record())

    async def image_changed(self) -> None:
        """Save frozen bytes when they change for the still-current detection."""
        await self._ready.wait()
        async with self._lock:
            try:
                content = await self._read_image()
                if not content:
                    raise ValueError("The source image is empty or unavailable")
            except Exception as err:
                self.status = {"state": "image_unavailable", "message": str(err),
                               **({"event_timestamp": self._pending["timestamp"],
                                   "label": self._pending["label"]} if self._pending else {})}
                await self._save(self._record())
                return

            digest = sha256(content).hexdigest()
            previous_digest = self._digest
            self._digest = digest
            if previous_digest is not None and previous_digest != digest:
                eligible = self._pending is None and not self._seeding
                self._recent_previous_digest = previous_digest if eligible else None
                self._changed_at = self._now() if eligible else None
            pending = self._pending
            if pending is None:
                if self.status.get("state") in {"starting", "image_unavailable"}:
                    timestamp, label = self._read_detection()
                    if (_time(timestamp) is not None and self._last_detection is None
                            and _stamp(timestamp) not in self._notified_detections):
                        timestamp = _stamp(timestamp)
                        self._last_detection = timestamp
                        self.status = {"state": "restored_detection", "event_timestamp": timestamp,
                                       "label": label,
                                       "message": "The last detection predates automatic tracking; waiting for a new live detection."}
                    else:
                        self.status = {"state": "ready",
                                       "message": "Waiting for a new Person or Package detection."}
                await self._save(self._record())
                return

            timestamp, label = self._read_detection()
            if _time(timestamp) is None:
                self._waiting()  # Detection entities can restore after image entities.
                await self._save(self._record())
                return
            timestamp = _stamp(timestamp)
            if timestamp != pending["timestamp"] or label != pending["label"]:
                if _time(timestamp) < _time(pending["timestamp"]) or label is None:
                    self._waiting()
                    await self._save(self._record())
                    return
                self._pending = None
                self.status = {"state": "superseded",
                               "message": "A different detection superseded the pending image."}
                await self._save(self._record())
                return

            if pending.get("baseline") is None:
                pending["baseline"] = previous_digest or digest
            if digest == pending["baseline"]:
                self._waiting()
                await self._save(self._record())
                return

            # Pass the bytes already checked. Calling image.snapshot again here
            # could read a later event's image while assigning this event's label.
            try:
                result = await self._capture(content, pending["label"], _time(timestamp))
            except Exception as err:
                self.status = {"state": "capture_error", "message": str(err),
                               "event_timestamp": timestamp, "label": label}
                await self._save(self._record())
                return
            self._pending = None
            self.status = {**result, "state": result["status"],
                           "event_timestamp": timestamp, "label": label,
                           "message": "Event image saved." if result["status"] == "saved"
                           else "The same image is already saved for this category."}
            await self._save(self._record())

    async def flush(self) -> None:
        """Persist the current correlation on orderly HA shutdown."""
        await self._ready.wait()
        async with self._lock:
            await self._save(self._record())
