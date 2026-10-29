import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "timeline_event_label", Path(__file__).resolve().parents[1] /
    "custom_components/front_door_timeline/event_label.py")
labels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labels)


class EventLabelTests(unittest.TestCase):
    def test_bridge_four_minute_retry_and_small_reverse_order_are_supported(self):
        for kind in ('person', 'stranger', 'package_delivered', 'package_taken'):
            for image_time in ('12:04:00', '11:59:58'):
                self.assertEqual(labels.delayed_image_label(
                    '2026-10-19T' + image_time + '+00:00',
                    '2026-10-19T12:00:00Z', kind), kind)

    def test_unrelated_old_detection_and_unknown_values_are_not_used(self):
        for state in ('unknown', 'unavailable', '', '2026-10-19T12:06:00Z', '2026-10-19T11:59:54Z'):
            self.assertIsNone(labels.delayed_image_label(state, '2026-10-19T12:00:00Z', 'person'))
        self.assertIsNone(labels.delayed_image_label('2026-10-19T12:01:00Z', '2026-10-19T12:00:00Z', 'sound'))
        self.assertIsNone(labels.delayed_image_label('2026-10-19T12:01:00Z', '2026-10-19T12:00:00Z', 'package_stranded'))

    def test_source_naming_is_limited_to_sdk_last_event_images(self):
        self.assertEqual(labels.detection_entity_for('image.front_door_last_event'), 'event.front_door_detection')
        self.assertIsNone(labels.detection_entity_for('camera.front_door_2'))
        self.assertIsNone(labels.detection_entity_for('image.another_photo'))
