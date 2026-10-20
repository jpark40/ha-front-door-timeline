"""Validate and commit snapshots without repeating an unchanged image."""

from __future__ import annotations

from hashlib import file_digest
from pathlib import Path
import re


SNAPSHOT_RE = re.compile(
    r"^front_door_"
    r"(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})"
    r"(?:-(?P<fraction>\d{1,6}))?"
    r"(?:_(?P<label>[A-Za-z0-9_-]+))?"
    r"\.(?P<extension>jpe?g|png)$",
    re.IGNORECASE,
)


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").digest()


def _latest_with_label(directory: Path, label: str) -> Path | None:
    """Find the previous capture, including captures from before an HA restart."""
    latest = None
    latest_key = None
    for path in directory.iterdir():
        match = SNAPSHOT_RE.fullmatch(path.name)
        if match is None or (match.group("label") or "event").lower() != label:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        key = (
            match.group("date"),
            match.group("hour"),
            match.group("minute"),
            match.group("second"),
            (match.group("fraction") or "").ljust(6, "0"),
            path.name,
        )
        if latest_key is None or key > latest_key:
            latest, latest_key = path, key
    return latest


def finalize_snapshot(pending: Path, destination: Path) -> Path:
    """Keep a changed image or return the existing identical capture.

    Only compare the previous capture with the same event label: a package
    event must remain available even if it uses a person's thumbnail. Matching
    bytes are suppressed until that label receives a different image. The
    pending name is outside SNAPSHOT_RE so API scans never expose partial files.
    Call from an executor under the integration's capture lock.
    """
    try:
        if not pending.is_file() or pending.stat().st_size == 0:
            raise ValueError(
                f"Snapshot action completed but no image was written for {destination}"
            )
        match = SNAPSHOT_RE.fullmatch(destination.name)
        if match is None:
            raise ValueError("Invalid timeline snapshot filename")
        if destination.exists():
            if _digest(pending) == _digest(destination):
                return destination
            raise FileExistsError(destination)

        label = (match.group("label") or "event").lower()
        previous = _latest_with_label(destination.parent, label)
        if previous is not None:
            try:
                same_image = _digest(pending) == _digest(previous)
            except OSError:
                # An unreadable/removed older file must not block a new capture.
                same_image = False
            if same_image:
                return previous

        pending.replace(destination)
        return destination
    finally:
        pending.unlink(missing_ok=True)
