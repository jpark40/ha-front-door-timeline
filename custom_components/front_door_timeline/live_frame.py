"""Read one JPEG from live video, bypassing a camera's cached still endpoint."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from homeassistant.components.camera import async_get_stream_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.exceptions import HomeAssistantError


LIVE_TIMEOUT_SECONDS = 30


async def async_live_frame(hass, camera_entity: str) -> bytes:
    """Decode a bounded live connection; always close its decoder and consumer.

    camera.async_get_image / camera.snapshot are intentionally not used: the
    Eufy bridge's /snapshot can return a persisted picture even in live mode.
    No HA stream worker is started, so battery cameras are not left preloaded.
    """
    process = None
    try:
        async with asyncio.timeout(LIVE_TIMEOUT_SECONDS):
            source = await async_get_stream_source(hass, camera_entity)
            if not source or urlsplit(source).scheme.lower() not in {
                "rtsp", "rtsps", "http", "https",
            }:
                raise HomeAssistantError(
                    f"No supported live video source is available for {camera_entity}"
                )
            args = [get_ffmpeg_manager(hass).binary, "-hide_banner", "-loglevel", "error",
                    "-nostdin", "-threads", "1"]
            if urlsplit(source).scheme.lower() in {"rtsp", "rtsps"}:
                args.extend(["-rtsp_transport", "tcp", "-timeout", "25000000"])
            else:
                args.extend(["-rw_timeout", "25000000"])
            args.extend([
                "-i", source, "-an", "-frames:v", "1", "-threads", "1",
                "-vf", "scale=w='min(1920,iw)':h=-2", "-q:v", "3",
                "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
            ])
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            content, _ = await process.communicate()
            if process.returncode or not content.startswith(b"\xff\xd8\xff"):
                raise HomeAssistantError(
                    f"Live video from {camera_entity} did not produce a JPEG frame"
                )
            return content
    except TimeoutError as err:
        raise HomeAssistantError(
            f"Live video from {camera_entity} produced no frame within {LIVE_TIMEOUT_SECONDS} seconds"
        ) from err
    except OSError as err:
        raise HomeAssistantError(
            f"Could not start the live video decoder for {camera_entity}"
        ) from err
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
