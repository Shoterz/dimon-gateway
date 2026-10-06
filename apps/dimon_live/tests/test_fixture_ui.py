from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from apps.dimon_live.cli import dev_main
from apps.dimon_live.provider_factory import build_inference_providers


class FixtureDevelopmentCommandTests(unittest.TestCase):
    def test_dimon_dev_defaults_to_fixture_for_the_server_runner(self) -> None:
        observed: list[str | None] = []

        def runner(*_args, **_kwargs) -> None:
            observed.append(os.environ.get("DIMON_INFERENCE_MODE"))

        with patch.dict(os.environ, {}, clear=True):
            dev_main(environ={}, runner=runner)
            self.assertNotIn("DIMON_INFERENCE_MODE", os.environ)

        self.assertEqual(observed, ["fixture"])

    def test_dimon_dev_preserves_an_explicit_mode(self) -> None:
        observed: list[str | None] = []

        def runner(*_args, **_kwargs) -> None:
            observed.append(os.environ.get("DIMON_INFERENCE_MODE"))

        with patch.dict(os.environ, {}, clear=True):
            dev_main(
                environ={"DIMON_INFERENCE_MODE": "local"},
                runner=runner,
            )

        self.assertEqual(observed, ["local"])


class FixtureSessionUiTests(unittest.TestCase):
    def setUp(self) -> None:
        from apps.dimon_live.app import create_app

        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            Path(self.temp_dir.name) / "demo.sqlite3",
            providers=build_inference_providers("fixture", {}),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(self, path: str) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                return await client.get(path)

        return asyncio.run(send())

    def test_session_exposes_only_the_public_inference_mode(self) -> None:
        body = self.request("/api/session").json()

        self.assertEqual(body["inference_mode"], "fixture")
        self.assertNotIn("url", body)
        self.assertNotIn("environment", body)

    def test_app_shell_contains_a_fixture_badge_controlled_by_session_mode(self) -> None:
        html = self.request("/").text
        script = self.request("/static/app.js").text

        self.assertIn('id="inference-mode-badge"', html)
        self.assertIn("Fixture inference", html)
        self.assertIn('fetch("/api/session")', script)
        self.assertIn('session.inference_mode === "fixture"', script)

    def test_configured_localhost_port_accepts_navigation_without_origin(self) -> None:
        from apps.dimon_live.app import create_app

        root = Path(self.temp_dir.name) / "port-boundary"
        settings = {
            "DIMON_ALLOWED_ORIGINS": "http://127.0.0.1:18990",
            "DIMON_DB_OWNER_PATH": str(root / "owner.sqlite3"),
            "DIMON_VISITOR_DB_DIR": str(root / "visitors"),
        }
        with patch.dict(os.environ, settings):
            app = create_app(providers=build_inference_providers("fixture", {}))

        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:18990",
            ) as client:
                return await client.get("/api/session")

        self.assertEqual(asyncio.run(send()).status_code, 200)
        app.state.session_manager.close()

    def test_configured_localhost_port_serves_the_page_without_origin(self) -> None:
        from apps.dimon_live.app import create_app

        root = Path(self.temp_dir.name) / "page-boundary"
        settings = {
            "DIMON_ALLOWED_ORIGINS": "http://127.0.0.1:18990",
            "DIMON_DB_OWNER_PATH": str(root / "owner.sqlite3"),
            "DIMON_VISITOR_DB_DIR": str(root / "visitors"),
        }
        with patch.dict(os.environ, settings):
            app = create_app(providers=build_inference_providers("fixture", {}))

        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:18990",
            ) as client:
                return await client.get("/")

        self.assertEqual(asyncio.run(send()).status_code, 200)
        app.state.session_manager.close()


if __name__ == "__main__":
    unittest.main()
