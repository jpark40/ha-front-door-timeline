"""Exercise live detections, late covers, byte reuse, and restart recovery."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from test_capture import component, FakeHass


class ManagedCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 26, 4, 37, 21, tzinfo=timezone.utc)
        self.image = b"previous event"
        self.detection = ((self.now - timedelta(hours=1)).isoformat(), "motion")
        self.saved = None
        self.captured = []
        self.refreshes = 0
        self.capture_failure = False
        self.monitor = self.create_monitor()
        await self.monitor.start()

    def create_monitor(self):
        async def load():
            return deepcopy(self.saved)
        async def save(value):
            self.saved = deepcopy(value)
        async def read_image():
            if self.image is None:
                raise ValueError("image unavailable")
            return self.image
        async def capture(content, label, detected):
            if self.capture_failure:
                raise OSError("storage unavailable")
            self.captured.append((content, label, detected))
            return {"status": "saved", "file": "event.jpg"}
        async def refresh():
            self.refreshes += 1
        return component.ManagedCapture(
            "image.front_door_last_event", load=load, save=save,
            read_image=read_image, read_detection=lambda: self.detection,
            capture=capture, refresh=refresh, now=lambda: self.now,
        )

    async def detect(self, label="person"):
        self.detection = (self.now.isoformat(), label)
        await self.monitor.detection_changed(*self.detection, self.now)

    async def test_31_minute_late_image_uses_frozen_detection_time_and_label(self):
        detected = self.now
        await self.detect()
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(self.captured, [])
        self.now += timedelta(minutes=31)
        self.image = b"new person event"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [(b"new person event", "person", detected)])
        self.assertEqual(self.monitor.status["state"], "saved")

    async def test_unchanged_cache_and_repeated_fetch_timestamps_do_not_create_events(self):
        await self.detect()
        for _ in range(4):
            self.now += timedelta(minutes=10)
            await self.monitor.image_changed()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["state"], "waiting_for_image")
        self.assertIn("has not supplied a changed", self.monitor.status["message"])

    async def test_pending_metadata_and_byte_baseline_survive_restart(self):
        detected = self.now
        await self.detect("stranger")
        self.now += timedelta(minutes=45)
        self.monitor = self.create_monitor()
        await self.monitor.start()
        self.assertEqual(self.captured, [])
        self.image = b"new stranger cover after restart"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [(self.image, "stranger", detected)])

    async def test_a_stored_detection_is_not_a_new_event_on_install(self):
        old = self.now - timedelta(minutes=31)
        self.detection = (old.isoformat(), "person")
        await self.monitor.detection_changed(*self.detection, self.now)
        self.image = b"a retained image downloaded at startup"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.refreshes, 0)
        self.assertEqual(self.monitor.status["state"], "restored_detection")

    async def test_image_can_arrive_just_before_the_detection_callback(self):
        self.image = b"new person event"
        await self.monitor.image_changed()
        self.now += timedelta(seconds=2)
        await self.detect()
        self.assertEqual(self.captured, [(self.image, "person", self.now)])

    async def test_an_image_from_long_before_a_new_detection_is_not_reused(self):
        self.image = b"unrelated earlier image"
        await self.monitor.image_changed()
        self.now += timedelta(minutes=10)
        await self.detect()
        self.assertEqual(self.captured, [])

    async def test_motion_or_other_types_supersede_pending_person_metadata(self):
        await self.detect()
        self.now += timedelta(seconds=10)
        await self.detect("vehicle")
        self.image = b"new vehicle picture"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["state"], "ignored_detection")

    async def test_new_package_detection_does_not_inherit_a_person_label(self):
        await self.detect()
        self.now += timedelta(seconds=10)
        await self.detect("package_delivered")
        self.image = b"new package cover"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [(self.image, "package_delivered", self.now)])

    async def test_newer_metadata_cannot_be_captured_with_old_queued_label(self):
        await self.detect()
        self.detection = ((self.now + timedelta(seconds=10)).isoformat(), "package_taken")
        self.image = b"new package picture"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["state"], "superseded")

    async def test_restored_older_metadata_does_not_destroy_pending_intent(self):
        await self.detect()
        expected = self.detection
        self.detection = ((self.now - timedelta(hours=1)).isoformat(), "person")
        await self.monitor.image_changed()
        self.detection = expected
        self.image = b"new cover"
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_source_unavailable_seeds_a_baseline_then_waits_for_changed_bytes(self):
        self.image = None
        self.saved = None
        self.monitor = self.create_monitor()
        await self.monitor.start()
        await self.detect()
        self.image = b"retained old image"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [])
        self.image = b"fresh event image"
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_failed_file_write_is_reported_and_can_retry_the_same_changed_image(self):
        await self.detect()
        self.capture_failure = True
        self.image = b"new person image"
        await self.monitor.image_changed()
        self.assertEqual(self.monitor.status["state"], "capture_error")
        self.capture_failure = False
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_same_event_and_cover_are_not_archived_twice(self):
        await self.detect()
        self.image = b"new cover"
        await self.monitor.image_changed()
        await self.monitor.detection_changed(*self.detection, self.now)
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_completed_person_picture_is_not_reused_for_a_rapid_package_event(self):
        await self.detect()
        self.image = b"new person picture"
        await self.monitor.image_changed()
        self.now += timedelta(seconds=1)
        await self.detect("package_delivered")
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.monitor.status["state"], "waiting_for_image")

    async def test_concurrent_detections_share_the_inflight_refresh_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def refresh():
            self.refreshes += 1
            entered.set()
            await release.wait()
        self.monitor._refresh = refresh
        first = asyncio.create_task(self.detect())
        await entered.wait()
        self.now += timedelta(seconds=1)
        await self.detect("package_delivered")
        release.set()
        await first
        self.assertEqual(self.refreshes, 1)
        self.image = b"new package image"
        await self.monitor.image_changed()
        self.assertEqual(self.captured[0][1], "package_delivered")

    async def test_refresh_error_is_visible_and_a_later_image_still_saves(self):
        async def refresh():
            raise OSError("bridge unavailable")
        self.monitor._refresh = refresh
        await self.detect()
        self.assertEqual(self.monitor.status["refresh_error"], "bridge unavailable")
        self.image = b"new image from later push"
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_initial_cache_reload_is_not_a_reverse_order_new_event_image(self):
        self.image = b"different retained picture at restart"
        self.monitor = self.create_monitor()
        await self.monitor.start()
        self.now += timedelta(seconds=1)
        await self.detect()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["state"], "waiting_for_image")


class ManagedAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_registers_after_initialization_and_shutdown_flushes_then_unsubscribes(self):
        with TemporaryDirectory() as folder:
            hass = FakeHass()
            hooks = {}
            hass.bus = SimpleNamespace(async_listen_once=lambda event, callback: hooks.update({event: callback}))
            entry = SimpleNamespace(platform="eufy_sdk", unique_id="CAM_last_event")
            hass.registry.async_get = lambda entity_id: entry
            old = datetime.now(timezone.utc) - timedelta(minutes=31)
            records = {
                "image.front_door_last_event": SimpleNamespace(state=datetime.now(timezone.utc).isoformat()),
                "event.front_door_detection": SimpleNamespace(state=old.isoformat(), attributes={"event_type": "person"}),
            }
            hass.states = SimpleNamespace(get=records.get)
            await component.async_setup(hass, {"front_door_timeline": {
                "directory": folder, "retention_days": 30,
            }})
            self.assertEqual(hass.listeners, [])
            await hooks["homeassistant_started"]()
            self.assertEqual(len(hass.listeners), 1)
            self.assertEqual(component._build_index(Path(folder), None)["total_count"], 0)
            self.assertEqual(hass.data["front_door_timeline"]["managed_capture"].status["state"], "restored_detection")
            await hooks["homeassistant_stop"](None)
            self.assertEqual(hass.listeners, [])
            self.assertIsNotNone(hass.storage["front_door_timeline.managed_capture"]["digest"])

    async def test_real_state_listener_saves_frozen_bytes_in_ha_local_time_without_automation(self):
        with TemporaryDirectory() as folder:
            hass = FakeHass()
            hass.is_running = True
            hass.config.time_zone = "America/Los_Angeles"
            source = "image.front_door_last_event"
            detection = "event.front_door_detection"
            source_entry = SimpleNamespace(platform="eufy_sdk", unique_id="CAM_last_event")
            hass.registry = SimpleNamespace(
                async_get=lambda eid: source_entry if eid == source else None,
                async_get_entity_id=lambda domain, platform, uid:
                    detection if domain == "event" else "button.refresh", entities={},
            )
            now = datetime.now(timezone.utc)
            records = {
                source: SimpleNamespace(state=(now - timedelta(hours=1)).isoformat()),
                detection: SimpleNamespace(state=(now - timedelta(hours=1)).isoformat(),
                                           attributes={"event_type": "motion"}),
            }
            hass.states = SimpleNamespace(get=records.get)
            calls = []
            async def press(domain, service, data, **kwargs):
                calls.append((domain, service))
            hass.services.async_call = press
            tasks = set()
            def create_task(coro):
                task = asyncio.create_task(coro)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                return task
            hass.async_create_task = create_task
            await component.async_setup(hass, {"front_door_timeline": {
                "directory": folder, "retention_days": 30,
            }})
            def emit(eid, state):
                old = records[eid]
                records[eid] = state
                for watched, callback in hass.listeners:
                    if eid in watched:
                        callback(SimpleNamespace(data={"entity_id": eid, "old_state": old,
                                                       "new_state": state}))
            async def drain():
                while tasks:
                    await asyncio.gather(*list(tasks))
            emit(detection, SimpleNamespace(state=now.isoformat(), attributes={"event_type": "person"}))
            await drain()
            self.assertEqual(calls, [("button", "press")])
            self.assertEqual(component._build_index(Path(folder), None)["total_count"], 0)
            hass.services.image = b"new person event bytes"
            emit(source, SimpleNamespace(state=(now + timedelta(minutes=31)).isoformat()))
            await drain()
            index = component._build_index(Path(folder), None)
            self.assertEqual(index["counts"]["person"], 1)
            saved = Path(folder) / index["images"][0]["file"]
            self.assertEqual(saved.read_bytes(), hass.services.image)
            self.assertEqual(index["images"][0]["date"], now.astimezone(ZoneInfo(hass.config.time_zone)).strftime("%Y-%m-%d"))
            self.assertEqual(calls, [("button", "press")], "frozen bytes must not be re-fetched with snapshot")
            self.assertEqual(component._source_status(hass, source)["capture"]["state"], "saved")

    async def test_automatic_capture_can_be_disabled_explicitly(self):
        with TemporaryDirectory() as folder:
            hass = FakeHass()
            hass.is_running = True
            await component.async_setup(hass, {"front_door_timeline": {
                "directory": folder, "retention_days": 30, "auto_capture": False,
            }})
            self.assertNotIn("managed_capture", hass.data["front_door_timeline"])
            self.assertEqual(hass.listeners, [])
