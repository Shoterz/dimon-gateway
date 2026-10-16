import asyncio
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from apps.dimon_live.gpu_overlord_status import (
    GpuOverlordStatusClient,
    GpuStatusError,
    parse_gpu_status,
)


def valid_response() -> dict[str, object]:
    return {
        "ok": True,
        "status": {
            "gpu": {
                "index": 0,
                "total_mb": 12_288,
                "used_mb": 4_096,
                "free_mb": 8_192,
                "startup_reserved_mb": 0,
                "safety_reserve_mb": 512,
                "secret_telemetry": "discard me",
            },
            "services": [
                {
                    "service": "whisper",
                    "state": "ready",
                    "active_jobs": 0,
                    "worker_pid": 999,
                    "profile": "large-v3",
                    "budget_mb": 6_144,
                    "observed_vram_mb": 3_456,
                    "heartbeat_age_seconds": 0.1,
                    "token": "must-not-survive",
                }
            ],
        },
        "internal": "/private/socket/path",
    }


class GpuOverlordStatusClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temporary_directory.name) / "overlord.sock"
        self.servers: list[asyncio.AbstractServer] = []

    async def asyncTearDown(self) -> None:
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.temporary_directory.cleanup()

    async def serve(self, handler) -> None:
        server = await asyncio.start_unix_server(handler, path=self.socket_path)
        self.servers.append(server)

    async def test_status_client_returns_only_bounded_service_and_gpu_fields(self):
        requests: list[bytes] = []

        async def handler(reader, writer):
            requests.append(await reader.readline())
            writer.write(json.dumps(valid_response()).encode("utf-8") + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        await self.serve(handler)

        snapshot = await GpuOverlordStatusClient(self.socket_path).read()

        self.assertEqual(requests, [b'{"op":"status"}\n'])
        self.assertEqual(snapshot.total_mb, 12_288)
        self.assertEqual(snapshot.free_mb, 8_192)
        self.assertEqual(snapshot.safety_reserve_mb, 512)
        self.assertEqual(len(snapshot.services), 1)
        whisper = snapshot.service("whisper")
        self.assertIsNotNone(whisper)
        self.assertEqual(
            vars(whisper),
            {
                "service": "whisper",
                "state": "ready",
                "active_jobs": 0,
                "worker_pid": 999,
                "profile": "large-v3",
                "budget_mb": 6_144,
                "observed_vram_mb": 3_456,
                "heartbeat_age_seconds": 0.1,
            },
        )
        self.assertTrue(whisper.worker_present)
        with self.assertRaises(FrozenInstanceError):
            snapshot.total_mb = 1
        with self.assertRaises(FrozenInstanceError):
            whisper.state = "idle"

    async def test_status_client_rejects_malformed_or_missing_response_fields(self):
        malformed = [
            None,
            {},
            {"ok": False, "error": "private failure"},
            {"ok": True},
            {"ok": True, "status": {"gpu": {}, "services": []}},
            {
                "ok": True,
                "status": {
                    "gpu": {
                        "total_mb": True,
                        "free_mb": 1,
                        "safety_reserve_mb": 1,
                    },
                    "services": [],
                },
            },
            {
                "ok": True,
                "status": {
                    "gpu": {
                        "total_mb": 1,
                        "free_mb": 1,
                        "safety_reserve_mb": 1,
                    },
                    "services": [
                        {
                            "service": "whisper",
                            "state": "unknown",
                            "active_jobs": 0,
                            "worker_pid": None,
                            "budget_mb": 1,
                            "observed_vram_mb": 0,
                            "profile": "large-v3",
                            "heartbeat_age_seconds": 0.1,
                        }
                    ],
                },
            },
            {
                "ok": True,
                "status": {
                    "gpu": {
                        "total_mb": 1,
                        "free_mb": 1,
                        "safety_reserve_mb": 1,
                    },
                    "services": [
                        {
                            "service": "whisper",
                            "state": "ready",
                            "active_jobs": 0,
                            "worker_pid": 1,
                            "profile": "large-v3",
                            "budget_mb": 1,
                            "observed_vram_mb": 1,
                            "heartbeat_age_seconds": float("nan"),
                        }
                    ],
                },
            },
        ]

        for response in malformed:
            with self.subTest(response=response):
                with self.assertRaisesRegex(
                    GpuStatusError, "^GPU_STATUS_UNAVAILABLE$"
                ) as caught:
                    parse_gpu_status(response)
                self.assertNotIn("private failure", str(caught.exception))

    async def test_status_client_rejects_oversized_and_truncated_lines(self):
        responses = [
            b"x" * 129 + b"\n",
            json.dumps(valid_response()).encode("utf-8"),
        ]

        for index, response in enumerate(responses):
            with self.subTest(index=index):
                socket_path = Path(self.temporary_directory.name) / f"overlord-{index}.sock"

                async def handler(_reader, writer, payload=response):
                    writer.write(payload)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_unix_server(handler, path=socket_path)
                self.servers.append(server)
                client = GpuOverlordStatusClient(
                    socket_path, timeout_seconds=0.2, max_response_bytes=128
                )
                with self.assertRaisesRegex(
                    GpuStatusError, "^GPU_STATUS_UNAVAILABLE$"
                ):
                    await client.read()

    async def test_status_client_maps_connect_and_timeout_failures(self):
        missing = Path(self.temporary_directory.name) / "missing.sock"
        with self.assertRaisesRegex(
            GpuStatusError, "^GPU_STATUS_UNAVAILABLE$"
        ):
            await GpuOverlordStatusClient(missing, timeout_seconds=0.02).read()

        async def handler(reader, writer):
            await reader.readline()
            await asyncio.sleep(1)
            writer.close()

        await self.serve(handler)
        with self.assertRaisesRegex(
            GpuStatusError, "^GPU_STATUS_UNAVAILABLE$"
        ):
            await GpuOverlordStatusClient(
                self.socket_path, timeout_seconds=0.02
            ).read()


if __name__ == "__main__":
    unittest.main()
