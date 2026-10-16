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

    module("voluptuous", Schema=lambda value, **kw: value,
           Required=lambda value, **kw: value, Optional=lambda value, **kw: value,
           All=lambda *args: args, Coerce=lambda value: value,
           Range=lambda **kw: kw, ALLOW_EXTRA=object())
    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.http", HomeAssistantView=object)
    module("homeassistant.const", ATTR_ENTITY_ID="entity_id")
    module("homeassistant.core", HomeAssistant=object, ServiceCall=object)
    module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    module("homeassistant.helpers")
    module("homeassistant.helpers.config_validation", string=str, entity_id=str)
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


if __name__ == "__main__":
    unittest.main()
