"""Regress detection-triggered capture from the two configured HA entities."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_capture import component, FakeHass


class ManagedCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)
        self.image = b"the image visible on the dashboard"
        self.detection = ((self.now - timedelta(hours=6)).isoformat(), "person")
        self.saved = None
        self.captured = []
        self.capture_failure = False
        self.monitor = self.create_monitor()
        await self.monitor.start()

    def create_monitor(self):
        async def load():
            return deepcopy(self.saved)
        async def save(record):
            self.saved = deepcopy(record)
        async def read_image():
            if self.image is None:
                raise ValueError("image unavailable")
            return self.image
        async def capture(content, label, detected):
            if self.capture_failure:
                raise OSError("storage unavailable")
            self.captured.append((content, label, detected))
            return {"status": "saved", "file": f"{detected.isoformat()}_{label}.jpg"}
        return component.ManagedCapture(
            "image.front_door_last_event", load=load, save=save,
            read_image=read_image, read_detection=lambda: self.detection,
            capture=capture, now=lambda: self.now,
        )

    async def detect(self, label="person"):
        self.detection = (self.now.isoformat(), label)
        await self.monitor.detection_changed(*self.detection, self.now)

    async def test_detection_saves_visible_image_without_image_timestamp_or_byte_change(self):
        await self.detect()
        self.assertEqual(self.captured, [(self.image, "person", self.now)])
        self.assertEqual(self.monitor.status["image_source"], "event_image")
        self.assertEqual(self.monitor.status["state"], "saved")

    async def test_stranger_and_both_requested_package_events_are_saved(self):
        for label in ("person", "stranger", "package_delivered", "package_taken"):
            self.now += timedelta(seconds=1)
            await self.detect(label)
        self.assertEqual([item[1] for item in self.captured],
                         ["person", "stranger", "package_delivered", "package_taken"])

    async def test_other_types_do_not_capture_or_inherit_the_previous_label(self):
        await self.detect()
        for label in ("package_stranded", "motion", "vehicle", "dog", "pet", "sound", "crying", None):
            self.now += timedelta(seconds=1)
            await self.detect(label)
            self.image = b"unrelated image"
            await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.monitor.status["state"], "ignored_detection")

    async def test_a_second_detection_with_identical_image_bytes_is_a_distinct_event(self):
        await self.detect()
        self.now += timedelta(minutes=31)
        await self.detect()
        self.assertEqual(len(self.captured), 2)
        self.assertNotEqual(self.captured[0][2], self.captured[1][2])
        self.assertEqual(self.captured[0][0], self.captured[1][0])

    async def test_repeated_callbacks_and_image_token_changes_do_not_duplicate_event(self):
        await self.detect()
        await self.monitor.detection_changed(*self.detection, self.now)
        for _ in range(4):
            await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_late_image_updates_same_detection_identity(self):
        await self.detect("stranger")
        detected = self.now
        self.now += timedelta(minutes=31)
        self.image = b"late stranger image"
        await self.monitor.image_changed()
        self.assertEqual(self.captured[-1], (self.image, "stranger", detected))
        self.assertEqual(self.captured[0][2], self.captured[-1][2])

    async def test_image_arriving_before_detection_is_read_when_detection_fires(self):
        self.image = b"new image before event callback"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [])
        await self.detect("package_taken")
        self.assertEqual(self.captured, [(self.image, "package_taken", self.now)])

    async def test_unavailable_image_is_retried_on_recovery_without_new_detection(self):
        self.image = None
        await self.detect()
        self.assertEqual(self.captured, [])
        self.assertEqual(self.monitor.status["state"], "image_unavailable")
        self.image = b"recovered image"
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [(self.image, "person", self.now)])

    async def test_write_failure_can_retry_same_image_for_same_event(self):
        self.capture_failure = True
        await self.detect()
        self.assertEqual(self.monitor.status["state"], "capture_error")
        self.capture_failure = False
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 1)

    async def test_restart_and_equivalent_timestamp_formats_do_not_duplicate(self):
        self.now = self.now.replace(microsecond=250000)
        await self.detect()
        self.detection = ("2026-10-29T19:00:00.250Z", "person")
        self.monitor = self.create_monitor()
        await self.monitor.start()
        await self.monitor.detection_changed(*self.detection, self.now)
        self.assertEqual(len(self.captured), 1)
        self.image = b"image updated after restart"
        await self.monitor.image_changed()
        self.assertEqual(len(self.captured), 2)
        self.assertEqual(self.captured[0][2], self.captured[1][2])

    async def test_old_restored_detection_is_not_a_new_event(self):
        self.image = b"retained picture"
        await self.monitor.detection_changed(*self.detection, self.now)
        await self.monitor.image_changed()
        self.assertEqual(self.captured, [])

    async def test_explicit_sync_can_save_current_restored_detection_without_timestamp_match(self):
        result = await self.monitor.sync_current()
        self.assertEqual(self.captured, [(self.image, "person", datetime.fromisoformat(self.detection[0]))])
        self.assertEqual(result["state"], "saved")

    async def test_old_pending_record_migrates_without_requiring_changed_bytes(self):
        self.detection = (self.now.isoformat(), "person")
        self.saved = {"source": self.monitor.source, "last_detection": self.detection[0],
                      "pending": {"timestamp": self.detection[0], "label": "person",
                                  "baseline": "an old hash"},
                      "status": {"state": "waiting_for_image", "label": "person"}}
        self.monitor = self.create_monitor()
        await self.monitor.start()
        self.assertEqual(self.captured, [(self.image, "person", self.now)])

    async def test_type_change_with_same_timestamp_is_handled(self):
        await self.detect("person")
        await self.detect("stranger")
        self.assertEqual([item[1] for item in self.captured], ["person", "stranger"])

    async def test_newer_detection_during_image_read_does_not_save_with_old_label(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read_image():
            entered.set()
            await release.wait()
            return b"new package image"
        self.monitor._read_image = read_image
        first = asyncio.create_task(self.detect("person"))
        await entered.wait()
        self.now += timedelta(seconds=1)
        self.detection = (self.now.isoformat(), "package_taken")
        second = asyncio.create_task(self.monitor.detection_changed(*self.detection, self.now))
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.captured, [(b"new package image", "package_taken", self.now)])


class ManagedAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def set_up_hass(self, folder, running=True):
        hass = FakeHass()
        hass.is_running = running
        hass.config.time_zone = "America/Los_Angeles"
        source, detection = "image.front_door_last_event", "event.front_door_detection"
        now = datetime.now(timezone.utc)
        records = {
            source: SimpleNamespace(state=(now - timedelta(hours=6)).isoformat(), attributes={}),
            detection: SimpleNamespace(state=(now - timedelta(hours=6)).isoformat(),
                                       attributes={"event_type": "person"}),
        }
        hass.states = SimpleNamespace(get=records.get)
        tasks, hooks = set(), {}
        hass.bus.async_listen_once = lambda event, callback: hooks.update({event: callback})
        def create_task(coro):
            task = asyncio.create_task(coro)
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return task
        hass.async_create_task = create_task
        await component.async_setup(hass, {"front_door_timeline": {
            "directory": folder, "retention_days": 30,
            "source_entity": source, "detection_entity": detection,
            # Old video settings must not enable an alternative image source.
            "live_fallback": True, "fallback_camera": "camera.front_door_2",
        }})
        def emit(entity, state):
            old = records[entity]
            records[entity] = state
            for watched, callback in hass.listeners:
                if entity in watched:
                    callback(SimpleNamespace(data={"entity_id": entity, "old_state": old,
                                                   "new_state": state}))
        async def drain():
            while tasks:
                await asyncio.gather(*list(tasks))
        return hass, records, emit, drain, hooks

    async def test_state_listener_archives_visible_image_then_updates_file_without_extra_entry(self):
        with TemporaryDirectory() as folder:
            hass, records, emit, drain, hooks = await self.set_up_hass(folder)
            detected = datetime.now(timezone.utc)
            image_state = records["image.front_door_last_event"].state
            emit("event.front_door_detection", SimpleNamespace(
                state=detected.isoformat(), attributes={"event_type": "stranger"}))
            await drain()
            index = component._build_index(Path(folder), None)
            self.assertEqual(index["counts"], {"person": 1, "package": 0})
            item = index["images"][0]
            path = Path(folder) / item["file"]
            self.assertEqual(path.read_bytes(), hass.services.image)
            self.assertEqual(item["date"], detected.astimezone(ZoneInfo(hass.config.time_zone)).strftime("%Y-%m-%d"))
            self.assertEqual(records["image.front_door_last_event"].state, image_state)
            self.assertEqual(hass.services.calls, [], "no camera, bridge button, or snapshot service is used")
            hass.services.image = b"updated Last event image after detection"
            emit("image.front_door_last_event", SimpleNamespace(state=image_state, attributes={"access_token": "changed"}))
            await drain()
            refreshed = component._build_index(Path(folder), None)
            self.assertEqual(refreshed["total_count"], 1)
            self.assertEqual(refreshed["images"][0]["file"], item["file"])
            self.assertNotEqual(refreshed["images"][0]["url"], item["url"])
            self.assertEqual(path.read_bytes(), hass.services.image)
            self.assertEqual(component._source_status(hass, "image.front_door_last_event")["capture"]["state"], "updated")
            await hooks["homeassistant_stop"](None)
            self.assertEqual(hass.listeners, [])

    async def test_all_four_types_capture_once_each_and_same_bytes_do_not_hide_new_events(self):
        with TemporaryDirectory() as folder:
            hass, records, emit, drain, hooks = await self.set_up_hass(folder)
            now = datetime.now(timezone.utc)
            for n, kind in enumerate(("person", "stranger", "package_delivered", "package_taken")):
                state = SimpleNamespace(state=(now + timedelta(microseconds=n)).isoformat(), attributes={"event_type": kind})
                emit("event.front_door_detection", state)
                await drain()
                emit("event.front_door_detection", state)
                await drain()
            index = component._build_index(Path(folder), None)
            self.assertEqual(index["counts"], {"person": 2, "package": 2})
            self.assertEqual(index["total_count"], 4)
            for item in index["images"]:
                self.assertEqual((Path(folder) / item["file"]).read_bytes(), hass.services.image)
            self.assertEqual(hass.services.calls, [])
            await hooks["homeassistant_stop"](None)

    async def test_unknown_image_timestamp_does_not_block_available_proxy_bytes(self):
        with TemporaryDirectory() as folder:
            hass, records, emit, drain, hooks = await self.set_up_hass(folder)
            records["image.front_door_last_event"].state = "unknown"
            emit("event.front_door_detection", SimpleNamespace(
                state=datetime.now(timezone.utc).isoformat(), attributes={"event_type": "package_taken"}))
            await drain()
            self.assertEqual(component._build_index(Path(folder), None)["counts"]["package"], 1)
            await hooks["homeassistant_stop"](None)

    async def test_listener_handles_event_type_attribute_change_at_same_timestamp(self):
        with TemporaryDirectory() as folder:
            hass, records, emit, drain, hooks = await self.set_up_hass(folder)
            timestamp = datetime.now(timezone.utc).isoformat()
            emit("event.front_door_detection", SimpleNamespace(state=timestamp, attributes={"event_type": "motion"}))
            await drain()
            emit("event.front_door_detection", SimpleNamespace(state=timestamp, attributes={"event_type": "stranger"}))
            await drain()
            self.assertEqual(component._build_index(Path(folder), None)["counts"]["person"], 1)
            await hooks["homeassistant_stop"](None)

    async def test_sync_action_uses_only_the_image_and_detection_even_with_hours_between_timestamps(self):
        with TemporaryDirectory() as folder:
            hass, records, emit, drain, hooks = await self.set_up_hass(folder)
            source = "image.front_door_last_event"
            records[source].state = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            response = await hass.services.handlers["refresh_source"](
                SimpleNamespace(data={"entity_id": source}, context=None))
            self.assertEqual(response["state"], "saved")
            index = component._build_index(Path(folder), None)
            self.assertEqual(index["counts"]["person"], 1)
            self.assertEqual((Path(folder) / index["images"][0]["file"]).read_bytes(), hass.services.image)
            again = await hass.services.handlers["refresh_source"](
                SimpleNamespace(data={"entity_id": source}, context=None))
            self.assertEqual(again["state"], "unchanged")
            self.assertEqual(component._build_index(Path(folder), None)["total_count"], 1)
            self.assertEqual(hass.services.calls, [])
            await hooks["homeassistant_stop"](None)

    async def test_detection_during_journal_loading_is_not_lost(self):
        with TemporaryDirectory() as folder:
            entered, release = asyncio.Event(), asyncio.Event()
            async def load():
                entered.set()
                await release.wait()
                return None
            journal = SimpleNamespace(async_load=load, async_save=AsyncMock())
            with patch.object(component, "Store", return_value=journal):
                hass, records, emit, drain, hooks = await self.set_up_hass(folder, running=False)
                startup = asyncio.create_task(hooks["homeassistant_started"]())
                await entered.wait()
                emit("event.front_door_detection", SimpleNamespace(
                    state=datetime.now(timezone.utc).isoformat(), attributes={"event_type": "person"}))
                release.set()
                await startup
                await drain()
                self.assertEqual(component._build_index(Path(folder), None)["counts"]["person"], 1)
                await hooks["homeassistant_stop"](None)

    async def test_automatic_capture_can_be_disabled(self):
        with TemporaryDirectory() as folder:
            hass = FakeHass()
            hass.is_running = True
            await component.async_setup(hass, {"front_door_timeline": {
                "directory": folder, "retention_days": 30, "auto_capture": False,
            }})
            self.assertNotIn("managed_capture", hass.data["front_door_timeline"])
            self.assertEqual(hass.listeners, [])
