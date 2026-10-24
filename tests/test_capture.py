"""Exercise capture service orchestration with a minimal fake HA boundary."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def load_component():
    """Import real component code; only replace unavailable HA dependencies."""
    modules = {}

    def module(name, **members):
        result = ModuleType(name)
        result.__dict__.update(members)
        modules[name] = result

    class HomeAssistantError(Exception):
        pass

    module("aiohttp", web=SimpleNamespace())
    module("voluptuous", Schema=lambda value, **kw: value,
           Required=lambda value, **kw: value, Optional=lambda value, **kw: value,
           All=lambda *args: args, Coerce=lambda value: value,
           Range=lambda **kw: kw, ALLOW_EXTRA=object())
    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.http", HomeAssistantView=object)
    module("homeassistant.const", ATTR_ENTITY_ID="entity_id")
    module("homeassistant.core", HomeAssistant=object, ServiceCall=object,
           SupportsResponse=SimpleNamespace(OPTIONAL="optional", ONLY="only"))
    module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    module("homeassistant.helpers")
    module("homeassistant.helpers.config_validation", string=str, entity_id=str)
    module("homeassistant.helpers.entity_registry", async_get=lambda hass: hass.registry)
    module("homeassistant.helpers.event", async_track_time_change=lambda *a, **kw: lambda: None)
    component = Path(__file__).resolve().parents[1] / "custom_components/front_door_timeline"
    spec = importlib.util.spec_from_file_location(
        "_capture_test_component", component / "__init__.py",
        submodule_search_locations=[str(component)],
    )
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, spec.name: result}):
        spec.loader.exec_module(result)
    return result


component = load_component()


class FakeServices:
    def __init__(self):
        self.handlers = {}
        self.image = b"fresh image"
        self.fail = False
        self.active = 0
        self.max_active = 0
        self.calls = []

    def async_register(self, domain, service, handler, **kwargs):
        self.handlers[service] = handler

    async def async_call(self, domain, service, data, *, blocking, context, target):
        self.calls.append((domain, service, data, target))
        assert blocking and service == "snapshot"
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            # Allow competing capture tasks to run while an image is downloaded.
            await asyncio.sleep(0.01)
            if self.image is not None:
                Path(data["filename"]).write_bytes(self.image)
            if self.fail:
                raise RuntimeError("upstream image download failed")
        finally:
            self.active -= 1


class FakeHass:
    def __init__(self):
        self.data = {}
        self.services = FakeServices()
        self.http = SimpleNamespace(register_view=lambda view: None)
        self.states = SimpleNamespace(get=lambda entity_id: None)
        self.registry = SimpleNamespace(
            async_get=lambda entity_id: None,
            async_get_entity_id=lambda *args: None,
            entities={},
        )

    async def async_add_executor_job(self, function, *args):
        return await asyncio.to_thread(function, *args)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.hass = FakeHass()
        await component.async_setup(self.hass, {"front_door_timeline": {
            "directory": self.temp.name, "retention_days": 30,
        }})

    async def capture(self, label="person", entity="image.front_door_last_event"):
        call = SimpleNamespace(data={"entity_id": entity, "label": label}, context=object())
        await self.hass.services.handlers["capture"](call)

    def index(self):
        return component._build_index(self.directory, None)

    async def test_parallel_duplicate_calls_save_once_and_count_once(self):
        with self.assertLogs(component._LOGGER, level="INFO") as logs:
            await asyncio.gather(self.capture(), self.capture())
        self.assertEqual(self.hass.services.max_active, 1)
        self.assertEqual(self.index()["total_count"], 1)
        self.assertEqual(self.index()["counts"]["person"], 1)
        self.assertTrue(any("Skipped duplicate" in line for line in logs.output))
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    async def test_new_bytes_save_and_distinct_package_metadata_remains(self):
        await self.capture()
        await self.capture("package_delivered")
        self.hass.services.image = b"a different fresh image"
        await self.capture()
        self.assertEqual(self.index()["total_count"], 3)
        self.assertEqual(self.index()["counts"], {"person": 2, "package": 1})

    async def test_upstream_failure_discards_partial_file(self):
        await self.capture()
        self.hass.services.fail = True
        with self.assertRaisesRegex(RuntimeError, "upstream image download failed"):
            await self.capture()
        self.assertEqual(self.index()["total_count"], 1)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    async def test_missing_image_is_reported_instead_of_silent_success(self):
        self.hass.services.image = None
        with self.assertRaisesRegex(component.HomeAssistantError, "no image was written"):
            await self.capture()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_camera_domain_uses_same_duplicate_protection(self):
        await self.capture(entity="camera.front_door_2")
        with self.assertLogs(component._LOGGER, level="INFO"):
            await self.capture(entity="camera.front_door_2")
        self.assertEqual(self.index()["total_count"], 1)
        self.assertEqual(self.hass.services.calls[0][0], "camera")

    async def test_late_eufy_cover_stays_in_person_tab_with_old_automation(self):
        records = {
            "image.front_door_last_event": SimpleNamespace(state="2026-10-19T12:04:00+00:00"),
            "event.front_door_detection": SimpleNamespace(
                state="2026-10-19T12:00:00+00:00", attributes={"event_type": "person"}),
        }
        self.hass.states = SimpleNamespace(get=records.get)
        await self.capture("image_update")
        self.assertEqual(self.index()["counts"]["person"], 1)
        self.assertEqual(self.index()["images"][0]["label"], "Person")

    async def test_stale_detection_does_not_mislabel_a_new_thumbnail(self):
        records = {
            "image.front_door_last_event": SimpleNamespace(state="2026-10-19T12:10:00+00:00"),
            "event.front_door_detection": SimpleNamespace(
                state="2026-10-19T12:00:00+00:00", attributes={"event_type": "person"}),
        }
        self.hass.states = SimpleNamespace(get=records.get)
        await self.capture("image_update")
        self.assertEqual(self.index()["counts"]["person"], 0)
        self.assertEqual(self.index()["images"][0]["label"], "Image Update")

    async def test_refresh_resolves_sdk_button_even_after_entity_rename(self):
        self.hass.registry = SimpleNamespace(
            async_get=lambda eid: SimpleNamespace(platform="eufy_sdk", unique_id="CAM123_last_event"),
            async_get_entity_id=lambda domain, platform, unique_id:
                "button.renamed_refresh" if (domain, platform, unique_id) ==
                ("button", "eufy_sdk", "CAM123_refresh_last_event") else None,
        )
        called = []
        original = self.hass.services.async_call
        async def press(*args, **kwargs):
            if args[1] == "press":
                called.append((args, kwargs))
            else:
                await original(*args, **kwargs)
        self.hass.services.async_call = press
        records = {"image.front_door_last_event": SimpleNamespace(state="2026-10-19T12:02:00+00:00"),
                   "event.front_door_detection": SimpleNamespace(state="2026-10-19T12:00:00+00:00", attributes={"event_type": "person"})}
        self.hass.states = SimpleNamespace(get=records.get)
        call = SimpleNamespace(data={"entity_id": "image.front_door_last_event"}, context=object())
        response = await self.hass.services.handlers["refresh_source"](call)
        self.assertEqual(called[0][0], ("button", "press", {}))
        self.assertEqual(called[0][1]["target"], {"entity_id": "button.renamed_refresh"})
        self.assertEqual(self.index()["total_count"], 1)
        self.assertEqual(response["status"], "saved")
        self.assertEqual(response["image_timestamp"], records["image.front_door_last_event"].state)
        self.assertEqual(self.index()["images"][0]["date"], "2026-10-19")

        # Pressing again with an already-loaded image has no new state edge. It
        # must report unchanged, rather than silently pretending to have synced.
        with patch.object(component, "REFRESH_SYNC_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(component.HomeAssistantError, "already contains the same image"):
                await self.hass.services.handlers["refresh_source"](call)
        self.assertEqual(self.index()["total_count"], 1)


    def prepare_refresh(self, records):
        self.hass.registry = SimpleNamespace(
            async_get=lambda eid: SimpleNamespace(platform="eufy_sdk", unique_id="CAM_last_event"),
            async_get_entity_id=lambda domain, *args:
                "button.front_door_refresh_last_event" if domain == "button" else None)
        self.hass.states = SimpleNamespace(get=records.get)
        original = self.hass.services.async_call
        async def route(domain, service, *args, **kwargs):
            if service != "press":
                await original(domain, service, *args, **kwargs)
        self.hass.services.async_call = route
        return SimpleNamespace(data={"entity_id": "image.front_door_last_event"}, context=object())

    async def test_refresh_does_not_save_a_hidden_unclassified_image(self):
        records = {
            "image.front_door_last_event": SimpleNamespace(state="2026-10-19T12:30:00Z"),
            "event.front_door_detection": SimpleNamespace(
                state="2026-10-19T12:00:00Z", attributes={"event_type": "person"})}
        call = self.prepare_refresh(records)
        with patch.object(component, "REFRESH_SYNC_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(component.HomeAssistantError, "no matching person/package"):
                await self.hass.services.handlers["refresh_source"](call)
        self.assertEqual(self.index()["total_count"], 0)
        call.data["label"] = "package"
        result = await self.hass.services.handlers["refresh_source"](call)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.index()["counts"]["package"], 1)

    async def test_refresh_waits_for_background_image_without_automation(self):
        records = {"image.front_door_last_event": SimpleNamespace(state="unavailable")}
        call = self.prepare_refresh(records)
        async def deliver():
            await asyncio.sleep(0.02)
            records["image.front_door_last_event"] = SimpleNamespace(state="2026-10-19T12:02:00Z")
            records["event.front_door_detection"] = SimpleNamespace(
                state="2026-10-19T12:00:00Z", attributes={"event_type": "person"})
        task = asyncio.create_task(deliver())
        with patch.object(component, "REFRESH_SYNC_TIMEOUT_SECONDS", 2):
            response = await self.hass.services.handlers["refresh_source"](call)
        await task
        self.assertEqual(response["status"], "saved")
        self.assertEqual(self.index()["counts"]["person"], 1)

    async def test_refresh_reports_no_image_instead_of_silent_success(self):
        call = self.prepare_refresh({"image.front_door_last_event": SimpleNamespace(state="unavailable")})
        with patch.object(component, "REFRESH_SYNC_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(component.HomeAssistantError, "No usable Eufy Last event image"):
                await self.hass.services.handlers["refresh_source"](call)
        self.assertEqual(self.index()["total_count"], 0)

    async def test_renamed_sdk_source_uses_detection_registry_pair(self):
        source_id = "image.front_door_last_event_2"
        detection_id = "event.front_door_detection_2"
        source_entry = SimpleNamespace(platform="eufy_sdk", unique_id="CAM_last_event")
        self.hass.registry = SimpleNamespace(
            async_get=lambda entity_id: source_entry if entity_id == source_id else None,
            async_get_entity_id=lambda domain, platform, unique_id: detection_id
            if (domain, platform, unique_id) == ("event", "eufy_sdk", "CAM_detection_event") else None,
            entities={},
        )
        records = {
            source_id: SimpleNamespace(state="2026-10-23T12:02:00Z"),
            detection_id: SimpleNamespace(state="2026-10-23T12:00:00Z",
                                          attributes={"event_type": "stranger"}),
        }
        self.hass.states = SimpleNamespace(get=records.get)
        await self.capture("image_update", source_id)
        self.assertEqual(self.index()["counts"]["person"], 1)
        self.assertEqual(self.index()["images"][0]["label"], "Stranger")

    async def test_diagnostics_reports_hidden_files_and_candidates_without_mutation(self):
        await self.capture("person")
        await self.capture("image_update")
        source_id = "image.front_door_last_event_2"
        entry = SimpleNamespace(entity_id=source_id, platform="eufy_sdk",
                                unique_id="CAM_last_event")
        self.hass.registry.entities = {source_id: entry}
        self.hass.states = SimpleNamespace(get=lambda entity_id:
            SimpleNamespace(state="2026-10-23T12:00:00Z") if entity_id == source_id else None)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        call = SimpleNamespace(data={}, context=object())
        calls_before = len(self.hass.services.calls)
        result = await self.hass.services.handlers["diagnostics"](call)
        self.assertEqual(result["storage"]["directory"], str(self.directory))
        self.assertEqual(result["storage"]["total_count"], 2)
        self.assertEqual(result["storage"]["counts"], {"person": 1, "package": 0})
        self.assertEqual(result["storage"]["hidden_count"], 1)
        self.assertEqual(result["storage"]["label_counts"], {"Person": 1, "Image Update": 1})
        self.assertEqual(result["eufy_image_entities"], [
            {"entity_id": source_id, "state": "2026-10-23T12:00:00Z"}])
        self.assertEqual(result["source"]["image_entity"], "image.front_door_last_event")
        self.assertIsNone(result["source"]["image_updated"])
        self.assertEqual(len(self.hass.services.calls), calls_before)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in self.directory.iterdir()}, before)

    async def test_diagnostics_reports_missing_storage_instead_of_zero_snapshots(self):
        self.directory.rmdir()
        call = SimpleNamespace(data={}, context=object())
        result = await self.hass.services.handlers["diagnostics"](call)
        self.assertIn("error", result["storage"])
        self.assertNotIn("total_count", result["storage"])
        self.assertFalse(self.directory.exists())

    async def test_index_view_selects_latest_person_date_and_returns_source_status(self):
        for date, label in (("2026-10-19", "stranger"), ("2026-10-23", "image_update")):
            (self.directory / f"front_door_{date}_12-00-00_{label}.jpg").write_bytes(b"image")
        view = component.TimelineIndexView(self.hass, self.directory)
        with patch.object(component.web, "json_response", lambda payload: payload, create=True):
            result = await view.get(SimpleNamespace(query={"category": "person"}))
        self.assertEqual(result["selected_date"], "2026-10-19")
        self.assertEqual(result["images"][0]["label"], "Stranger")
        self.assertEqual(result["source_status"]["image_entity"], "image.front_door_last_event")

    async def test_index_view_rejects_unknown_category(self):
        class BadRequest(Exception):
            def __init__(self, *, text):
                super().__init__(text)
        view = component.TimelineIndexView(self.hass, self.directory)
        with patch.object(component.web, "HTTPBadRequest", BadRequest, create=True):
            with self.assertRaisesRegex(BadRequest, "category must be person or package"):
                await view.get(SimpleNamespace(query={"category": "all"}))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def save(self, date, label):
        (self.directory / f"front_door_{date}_12-00-00-000001_{label}.jpg").write_bytes(b"image")

    def test_latest_category_date_ignores_newer_hidden_and_other_category_images(self):
        self.save("2026-10-19", "stranger")
        self.save("2026-10-22", "package_delivered")
        self.save("2026-10-23", "image_update")
        result = component._build_index(self.directory, None, "person")
        self.assertEqual(result["selected_date"], "2026-10-19")
        self.assertEqual(result["latest_date"], "2026-10-19")
        self.assertEqual(result["latest_dates"], {"person": "2026-10-19", "package": "2026-10-22"})
        self.assertEqual([item["label"] for item in result["images"]], ["Stranger"])
        self.assertEqual(result["dates"][-1]["counts"], {"person": 0, "package": 0})
        result = component._build_index(self.directory, None, "package")
        self.assertEqual(result["selected_date"], "2026-10-22")
        self.assertEqual(result["counts"], {"person": 1, "package": 1})
        # Existing API callers without a category retain their previous result.
        self.assertEqual(component._build_index(self.directory, None)["selected_date"], "2026-10-23")

    def test_explicit_empty_category_date_is_preserved(self):
        self.save("2026-10-19", "person")
        self.save("2026-10-23", "image_update")
        result = component._build_index(self.directory, "2026-10-23", "person")
        self.assertEqual(result["selected_date"], "2026-10-23")
        self.assertEqual(result["images"][0]["label"], "Image Update")
        self.assertEqual(result["latest_date"], "2026-10-19")

    def test_only_hidden_files_do_not_claim_to_be_latest_person_or_package_history(self):
        self.save("2026-10-23", "image_update")
        result = component._build_index(self.directory, None, "person")
        self.assertIsNone(result["selected_date"])
        self.assertIsNone(result["latest_date"])
        self.assertEqual(result["images"], [])
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["hidden_count"], 1)
        self.assertEqual(result["label_counts"], {"Image Update": 1})


if __name__ == "__main__":
    unittest.main()
