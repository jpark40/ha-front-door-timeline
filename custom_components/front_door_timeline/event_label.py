"""Correlate a delayed SDK thumbnail with bounded detection metadata."""

from datetime import datetime


MAX_IMAGE_DELAY_SECONDS = 300  # Bridge local-cover retries run for up to 240 s.
DETECTION_LABELS = frozenset({
    "person", "stranger", "package_delivered", "package_taken", "package_stranded",
})


def detection_entity_for(image_entity: str) -> str | None:
    """Use the SDK's documented last_event/detection entity naming pair."""
    if not image_entity.startswith("image.") or not image_entity.endswith("_last_event"):
        return None
    return "event." + image_entity[6:-len("_last_event")] + "_detection"


def delayed_image_label(image_state: str, detection_state: str, kind: str) -> str | None:
    """Accept only known classifications near this image's update timestamp.

    The old automation's 60-second bound is shorter than the bridge retry
    schedule. Compare entity timestamps, never the time the capture queue runs.
    A small reverse-order allowance covers independent HA state writes.
    """
    if kind not in DETECTION_LABELS:
        return None
    try:
        image_time = datetime.fromisoformat(image_state.replace("Z", "+00:00"))
        detection_time = datetime.fromisoformat(detection_state.replace("Z", "+00:00"))
        if image_time.tzinfo is None or detection_time.tzinfo is None:
            return None
        delay = (image_time - detection_time).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return None
    return kind if -5 <= delay <= MAX_IMAGE_DELAY_SECONDS else None
