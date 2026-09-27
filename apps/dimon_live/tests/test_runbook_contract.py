import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class RunbookContractTests(unittest.TestCase):
    def test_launcher_is_loopback_only_and_does_not_start_models(self):
        script = (ROOT / "apps/dimon_live/run_public_mvp.sh").read_text()
        self.assertIn("127.0.0.1", script)
        self.assertIn("8890", script)
        self.assertNotIn("0.0.0.0", script)
        self.assertNotIn("run-gpu-overlord", script)
        self.assertNotIn("llama-server", script)

    def test_env_example_contains_isolation_and_ingress_controls(self):
        env = (ROOT / "apps/dimon_live/.env.example").read_text()
        for key in (
            "DIMON_DB_OWNER_PATH",
            "DIMON_VISITOR_DB_DIR",
            "DIMON_ALLOWED_ORIGINS",
            "DIMON_OWNER_ACCESS_AUDIENCE",
            "DIMON_NEMOTRON_URL",
            "DIMON_KOKORO_URL",
            "DIMON_MAX_AUDIO_BYTES",
        ):
            self.assertIn(key, env)


if __name__ == "__main__":
    unittest.main()
