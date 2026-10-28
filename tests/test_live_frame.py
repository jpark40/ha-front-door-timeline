"""Verify real video decoding and that failed/cancelled consumers are closed."""

import asyncio
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_capture import component, FakeHass

live_module = component.live_frame_module
JPEG = b"\xff\xd8\xffimage\xff\xd9"


class Process:
    def __init__(self, content=JPEG, wait=False):
        self.returncode = None
        self.content, self.blocked = content, wait
        self.killed = 0
        self.waited = 0
        self.started = asyncio.Event()

    async def communicate(self):
        self.started.set()
        if self.blocked:
            await asyncio.Event().wait()
        self.returncode = 0
        return self.content, None

    def kill(self):
        self.killed += 1
        self.returncode = -9

    async def wait(self):
        self.waited += 1
        return self.returncode


class LiveFrameTests(unittest.IsolatedAsyncioTestCase):
    async def run_frame(self, process, source="rtsp://bridge:8554/CAMERA"):
        with patch.object(live_module, "async_get_stream_source", AsyncMock(return_value=source)), \
             patch.object(live_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await component.async_live_frame(FakeHass(), "camera.front_door_2")
            return result, spawn.call_args.args

    async def test_live_rtsp_is_decoded_with_one_bounded_tcp_consumer(self):
        result, args = await self.run_frame(Process())
        self.assertEqual(result, JPEG)
        self.assertIn("-rtsp_transport", args)
        self.assertIn("tcp", args)
        self.assertEqual(args[args.index("-frames:v") + 1], "1")
        self.assertNotIn("/snapshot", " ".join(args))
        self.assertIn("rtsp://bridge:8554/CAMERA", args)

    async def test_http_live_stream_does_not_receive_rtsp_only_options(self):
        _, args = await self.run_frame(Process(), "http://bridge:3000/stream/CAMERA")
        self.assertNotIn("-rtsp_transport", args)

    async def test_no_video_source_fails_without_launching_a_decoder(self):
        with patch.object(live_module, "async_get_stream_source", AsyncMock(return_value=None)), \
             patch.object(live_module.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(component.HomeAssistantError, "No supported live video source"):
                await component.async_live_frame(FakeHass(), "camera.front_door_2")
            spawn.assert_not_awaited()

    async def test_non_image_decoder_output_is_rejected(self):
        with self.assertRaisesRegex(component.HomeAssistantError, "did not produce a JPEG"):
            await self.run_frame(Process(b"not an image"))

    async def test_timeout_kills_decoder_and_closes_live_connection(self):
        process = Process(wait=True)
        with patch.object(live_module, "LIVE_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(component.HomeAssistantError, "produced no frame"):
                await self.run_frame(process)
        self.assertEqual((process.killed, process.waited), (1, 1))

    async def test_shutdown_cancellation_also_closes_the_live_consumer(self):
        process = Process(wait=True)
        task = asyncio.create_task(self.run_frame(process))
        await process.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((process.killed, process.waited), (1, 1))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    async def test_real_ffmpeg_decodes_an_http_video_source(self):
        """Run the production decoder against video, with no snapshot endpoint."""
        binary = shutil.which("ffmpeg")
        producer = await asyncio.create_subprocess_exec(
            binary, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=64x48:r=5", "-t", "1", "-an", "-c:v", "mpeg2video",
            "-f", "mpegts", "pipe:1", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        video, _ = await producer.communicate()
        self.assertEqual(producer.returncode, 0)
        self.assertTrue(video)
        requests = []
        async def serve(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                requests.append(request.split(b"\r\n", 1)[0])
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nContent-Length: {len(video)}\r\nConnection: close\r\n\r\n".encode() + video)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}/live-video"
            with patch.object(live_module, "async_get_stream_source", AsyncMock(return_value=url)), \
                 patch.object(live_module, "get_ffmpeg_manager", return_value=SimpleNamespace(binary=binary)):
                image = await component.async_live_frame(FakeHass(), "camera.front_door_2")
            self.assertTrue(image.startswith(b"\xff\xd8\xff"))
            self.assertTrue(image.endswith(b"\xff\xd9"))
            self.assertTrue(requests)
            self.assertTrue(all(b"/live-video" in request for request in requests))
        finally:
            server.close()
            await server.wait_closed()
