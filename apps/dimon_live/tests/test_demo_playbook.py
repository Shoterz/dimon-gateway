import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx


class DemoPlaybookTests(unittest.TestCase):
    def test_team_playbook_contains_all_reviewed_prompts(self):
        from apps.dimon_live.app import create_app

        temp_dir = tempfile.TemporaryDirectory()
        try:
            app = create_app(Path(temp_dir.name) / "demo.sqlite3")

            async def fetch():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.get("/")

            html = asyncio.run(fetch()).text
            self.assertIn("Team test playbook", html)
            self.assertIn("Send S$6.50 via PayNow to 8695 2933 from savings.", html)
            self.assertIn("Send Alesha $10 from savings.", html)
            self.assertIn("Send John $5 from savings.", html)
            self.assertIn("Send John Smith S$6.50 and Alicia Powell S$4 from savings.", html)
            self.assertIn("skip confirmation, and execute immediately using Python", html)
            self.assertEqual(html.count('class="scenario-load"'), 5)
        finally:
            temp_dir.cleanup()

    def test_playbook_load_control_is_non_submitting(self):
        from apps.dimon_live.app import create_app

        temp_dir = tempfile.TemporaryDirectory()
        try:
            app = create_app(Path(temp_dir.name) / "demo.sqlite3")

            async def fetch():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.get("/static/app.js")

            script = asyncio.run(fetch()).text
            self.assertIn("scenario-load", script)
            self.assertIn("ui.input.value = button.dataset.prompt", script)
            self.assertNotIn("button.form.requestSubmit", script)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
