"""Regress the reported stale-thumbnail case using a separate live source."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_capture import component, FakeHass


class LiveFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc)
        self.detection = ((self.now - timedelta(days=1)).isoformat(), "person")
        self.image = b"yesterday's event thumbnail"
        self.live = b"new live frame"
        self.captured = []
        self.record = None
        self.attempts = 0
        async def load():
            return deepcopy(self.record)
        async def save(record):
            self.record = deepcopy(record)
        async def image():
            if self.image is None:
                raise ValueError("event image unavailable")
            return self.image
        async def capture(content, label, stamp):
            self.captured.append((content, label, stamp))
            return {"status": "saved", "file": "live.jpg"}
        async def fallback():
            self.attempts += 1
            return self.live, "camera.front_door_2"
        self.monitor = component.ManagedCapture(
            "image.front_door_last_event", load=load, save=save,
            read_image=image, read_detection=lambda: self.detection,
            capture=capture, refresh=AsyncMock(), now=lambda: self.now,
            fallback=fallback, fallback_delay=0,
        )
        await self.monitor.start()

    async def detect(self, label="person"):
        self.detection = self.now.isoformat(), label
        await self.monitor.detection_changed(*self.detection, self.now)

    async def test_reported_stale_thumbnail_saves_changed_live_frame(self):
        await self.detect()
        self.assertEqual(self.captured, [(self.live, "person", self.now)])
        self.assertEqual(self.monitor.status["image_source"], "live_stream")
        self.assertEqual(self.monitor.status["camera_entity"], "camera.front_door_2")
        self.assertEqual(self.monitor.status["captured_at"], self.now.isoformat())
        self.assertEqual(self.image, b"yesterday's event thumbnail")
        self.assertEqual(self.attempts, 1)

    async def test_primary_event_image_wins_without_opening_live_video(self):
        async def refresh():
            self.image = b"new primary cover"
        self.monitor._refresh = refresh
        await self.detect("package_delivered")
        self.assertEqual(self.captured[0][:2], (self.image, "package_delivered"))
        self.assertEqual(self.attempts, 0)

    async def test_unavailable_event_image_does_not_block_live_fallback(self):
        self.image = None
        await self.detect()
        self.assertEqual(self.captured[0][0], self.live)

    async def test_old_pending_detection_does_not_snapshot_todays_scene(self):
        async def broken():
            raise OSError("no live frame")
        self.monitor._fallback = broken
        await self.detect()
        self.now += timedelta(hours=8)
        self.monitor._pending.pop("fallback_attempted")
        self.monitor._fallback = AsyncMock(return_value=(self.live, "camera.front_door_2"))
        await self.monitor._try_fallback(self.detection[0])
        self.monitor._fallback.assert_not_awaited()
        self.assertEqual(self.captured, [])

    async def test_live_failure_is_reported_and_later_primary_cover_still_saves(self):
        async def broken():
            self.attempts += 1
            raise OSError("live video unavailable")
        self.monitor._fallback = broken
        await self.detect()
        self.assertEqual(self.monitor.status["fallback_error"], "live video unavailable")
        await self.monitor.image_changed()
        self.assertEqual(self.monitor.status["fallback_error"], "live video unavailable")
        await self.monitor._try_fallback(self.detection[0])
        self.assertEqual(self.attempts, 1, "do not repeatedly wake a camera for the same detection")
        self.now += timedelta(minutes=31)
        self.image = b"finally a new primary event image"
        await self.monitor.image_changed()
        self.assertEqual(self.captured[0][0], self.image)

    async def test_cached_identical_bytes_are_not_accepted_as_a_live_frame(self):
        self.live = self.image
        await self.detect()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["fallback_state"], "unchanged")

    async def test_newer_detection_discards_the_old_inflight_live_frame(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fallback():
            self.attempts += 1
            if self.attempts == 1:
                entered.set()
                await release.wait()
                return b"frame from superseded event", "camera.front_door_2"
            return b"new package frame", "camera.front_door_2"
        self.monitor._fallback = fallback
        first = asyncio.create_task(self.detect())
        await entered.wait()
        self.now += timedelta(seconds=1)
        second = asyncio.create_task(self.detect("package_taken"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.captured, [(b"new package frame", "package_taken", self.now)])
        self.assertEqual(self.attempts, 2)

    async def test_primary_image_arriving_during_live_startup_discards_live_frame(self):
        async def fallback():
            self.image = b"fresh primary event cover"
            await self.monitor.image_changed()
            return self.live, "camera.front_door_2"
        self.monitor._fallback = fallback
        await self.detect()
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0][0], self.image)

    async def test_frame_arriving_too_late_is_not_archived_with_old_event_time(self):
        async def fallback():
            self.now += timedelta(seconds=61)
            return self.live, "camera.front_door_2"
        self.monitor._fallback = fallback
        await self.detect()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["fallback_state"], "expired")

    async def test_primary_refresh_failure_still_tries_separate_live_source(self):
        self.monitor._refresh = AsyncMock(side_effect=OSError("cover refresh failed"))
        await self.detect()
        self.assertEqual(self.captured[0][0], self.live)

    async def test_restart_does_not_leave_an_interrupted_live_attempt_marked_as_capturing(self):
        self.record = {"source": self.monitor.source, "digest": self.monitor._digest,
                       "last_detection": self.now.isoformat(),
                       "pending": {"timestamp": self.now.isoformat(), "label": "person",
                                   "baseline": self.monitor._digest, "fallback_attempted": True},
                       "status": {"state": "waiting_for_image", "event_timestamp": self.now.isoformat(),
                                  "label": "person", "fallback_state": "capturing"}}
        self.detection = self.now.isoformat(), "person"
        await self.monitor.start()
        self.assertEqual(self.monitor.status["fallback_state"], "interrupted")
        self.assertEqual(self.attempts, 0)


class LiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_detection_during_journal_loading_is_not_lost_as_restored_state(self):
        with TemporaryDirectory() as folder:
            hass = FakeHass()
            hooks = {}
            hass.bus.async_listen_once = lambda event, callback: hooks.update({event: callback})
            source, detection = "image.front_door_last_event", "event.front_door_detection"
            entry = SimpleNamespace(platform="eufy_sdk", unique_id="CAM_last_event")
            hass.registry = SimpleNamespace(async_get=lambda eid: entry if eid == source else None,
                                            async_get_entity_id=lambda domain, *args: detection if domain == "event" else "camera.front_door_2",
                                            entities={})
            now = datetime.now(timezone.utc)
            records = {source: SimpleNamespace(state=(now - timedelta(hours=6)).isoformat()),
                       detection: SimpleNamespace(state=(now - timedelta(hours=6)).isoformat(), attributes={"event_type": "person"})}
            hass.states = SimpleNamespace(get=records.get)
            hass.services.async_call = AsyncMock()
            entered, release = asyncio.Event(), asyncio.Event()
            async def load():
                entered.set()
                await release.wait()
                return None
            journal = SimpleNamespace(async_load=load, async_save=AsyncMock())
            with patch.object(component, "Store", return_value=journal), \
                 patch.object(component, "async_live_frame", AsyncMock(return_value=b"live frame at startup")):
                await component.async_setup(hass, {"front_door_timeline": {"directory": folder, "retention_days": 30}})
                monitor = hass.data["front_door_timeline"]["managed_capture"]
                monitor._fallback_delay = 0
                startup = asyncio.create_task(hooks["homeassistant_started"]())
                await entered.wait()
                old = records[detection]
                records[detection] = SimpleNamespace(state=now.isoformat(), attributes={"event_type": "person"})
                self.assertTrue(hass.listeners, "listen before waiting on bridge/startup I/O")
                for watched, callback in hass.listeners:
                    if detection in watched:
                        callback(SimpleNamespace(data={"entity_id": detection, "old_state": old, "new_state": records[detection]}))
                await asyncio.sleep(0)
                release.set()
                await startup
                # Shutdown drains all capture jobs, including the startup job.
                for _ in range(100):
                    if component._build_index(Path(folder), None)["total_count"]:
                        break
                    await asyncio.sleep(0.001)
                await hooks["homeassistant_stop"](None)
                self.assertEqual(component._build_index(Path(folder), None)["counts"]["person"], 1)

    async def test_state_listener_uses_serial_matched_camera_and_saves_without_image_update(self):
        with TemporaryDirectory() as folder:
            hass = FakeHass()
            hass.is_running = True
            source, detection, camera = "image.front_door_last_event", "event.front_door_detection", "camera.front_door_2"
            registry_entry = SimpleNamespace(platform="eufy_sdk", unique_id="CAM123_last_event")
            pairs = {("event", "eufy_sdk", "CAM123_detection_event"): detection,
                     ("button", "eufy_sdk", "CAM123_refresh_last_event"): "button.refresh",
                     ("camera", "eufy_sdk", "CAM123_camera"): camera}
            hass.registry = SimpleNamespace(async_get=lambda eid: registry_entry if eid == source else None,
                                            async_get_entity_id=lambda *key: pairs.get(key), entities={})
            now = datetime.now(timezone.utc)
            records = {source: SimpleNamespace(state=(now - timedelta(hours=6)).isoformat()),
                       detection: SimpleNamespace(state=(now - timedelta(hours=6)).isoformat(), attributes={"event_type": "person"}),
                       camera: SimpleNamespace(state="idle")}
            hass.states = SimpleNamespace(get=records.get)
            hass.services.async_call = AsyncMock()
            tasks = set()
            def run(coro):
                task = asyncio.create_task(coro)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                return task
            hass.async_create_task = run
            with patch.object(component, "async_live_frame", AsyncMock(return_value=b"fresh decoded video frame")) as live:
                await component.async_setup(hass, {"front_door_timeline": {"directory": folder, "retention_days": 30}})
                hass.data["front_door_timeline"]["managed_capture"]._fallback_delay = 0
                old = records[detection]
                records[detection] = SimpleNamespace(state=now.isoformat(), attributes={"event_type": "person"})
                for watched, callback in hass.listeners:
                    if detection in watched:
                        callback(SimpleNamespace(data={"entity_id": detection, "old_state": old, "new_state": records[detection]}))
                while tasks:
                    await asyncio.gather(*list(tasks))
                live.assert_awaited_once_with(hass, camera)
            index = component._build_index(Path(folder), None, "person")
            self.assertEqual(index["counts"]["person"], 1)
            self.assertEqual((Path(folder) / index["images"][0]["file"]).read_bytes(), b"fresh decoded video frame")
            status = component._source_status(hass, source)
            self.assertEqual(status["capture"]["image_source"], "live_stream")
            self.assertEqual(status["fallback"]["camera_entity"], camera)

    async def test_explicit_camera_override_and_no_cross_camera_name_guess(self):
        hass = FakeHass()
        hass.data["front_door_timeline"] = {"fallback_camera": "camera.blue_iris_front_door"}
        self.assertEqual(component._fallback_camera_for(hass, "image.front_door_last_event"), "camera.blue_iris_front_door")
        hass.data["front_door_timeline"] = {}
        self.assertIsNone(component._fallback_camera_for(hass, "image.front_door_last_event"))
