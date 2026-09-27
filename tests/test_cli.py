from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from apps.dimon_live.cli import main


class DimonServeCliTests(unittest.TestCase):
    def test_defaults_to_loopback_port_8890(self) -> None:
        runner = Mock()

        main(environ={}, runner=runner)

        runner.assert_called_once_with(
            "apps.dimon_live.app:app",
            host="127.0.0.1",
            port=8890,
        )

    def test_accepts_explicit_host_and_port(self) -> None:
        runner = Mock()

        main(
            environ={"DIMON_HOST": "127.0.0.2", "DIMON_PORT": "8892"},
            runner=runner,
        )

        runner.assert_called_once_with(
            "apps.dimon_live.app:app",
            host="127.0.0.2",
            port=8892,
        )

    def test_does_not_start_child_model_processes(self) -> None:
        runner = Mock()

        with patch("subprocess.Popen") as popen:
            main(environ={}, runner=runner)

        popen.assert_not_called()

    def test_rejects_an_invalid_port_before_starting(self) -> None:
        runner = Mock()

        with self.assertRaisesRegex(ValueError, "DIMON_PORT"):
            main(environ={"DIMON_PORT": "70000"}, runner=runner)

        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
