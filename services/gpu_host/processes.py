"""Project-owned process lifecycle with PID identity checks."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence


class ProcessStateError(RuntimeError):
    pass


class ProcessAdapter(Protocol):
    def spawn(self, command: Sequence[str], log_path: Path) -> int: ...
    def identity(self, pid: int) -> str | None: ...
    def is_alive(self, pid: int) -> bool: ...
    def stop_group(self, pid: int, timeout: float) -> None: ...


class SubprocessAdapter:
    def spawn(self, command: Sequence[str], log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
            log_path.replace(log_path.with_suffix(".log.1"))
        log = log_path.open("ab", buffering=0)
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        log.close()
        return process.pid

    def identity(self, pid: int) -> str | None:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            return None
        start_ticks = stat.rsplit(")", 1)[1].split()[19]
        return hashlib.sha256(f"{boot_id}:{start_ticks}".encode()).hexdigest()

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def stop_group(self, pid: int, timeout: float) -> None:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while self.is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.is_alive(pid):
            os.killpg(pid, signal.SIGKILL)


class ManagedProcess:
    def __init__(
        self,
        *,
        name: str,
        command: Sequence[str],
        state_dir: Path,
        readiness: Callable[[], bool],
        adapter: ProcessAdapter | None = None,
        startup_timeout_seconds: float = 30,
        stop_timeout_seconds: float = 5,
        poll_seconds: float = 0.1,
        backoff_seconds: float = 2,
    ):
        if not name or not command:
            raise ValueError("managed process requires a name and command")
        expected_suffix = f"services/{name}/run.sh"
        if len(command) != 1 or not str(command[0]).endswith(expected_suffix):
            raise ValueError("managed command is outside the launcher allow-list")
        self.name = name
        self.command = tuple(str(part) for part in command)
        self.state_dir = Path(state_dir)
        self.readiness = readiness
        self.adapter = adapter or SubprocessAdapter()
        self.startup_timeout_seconds = startup_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.poll_seconds = poll_seconds
        self.backoff_seconds = backoff_seconds
        self._retry_after = 0.0

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"{self.name}.json"

    @property
    def command_hash(self) -> str:
        return hashlib.sha256(b"\0".join(part.encode() for part in self.command)).hexdigest()

    def _read_state(self) -> dict | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _owned_pid(self) -> int | None:
        state = self._read_state()
        if state is None:
            return None
        pid = state.get("pid")
        if (
            not isinstance(pid, int)
            or state.get("command_hash") != self.command_hash
            or self.adapter.identity(pid) != state.get("identity")
        ):
            self.state_path.unlink(missing_ok=True)
            return None
        return pid

    def start(self) -> int:
        existing = self._owned_pid()
        if existing is not None and self.adapter.is_alive(existing):
            return existing
        now = time.monotonic()
        if now < self._retry_after:
            raise ProcessStateError("PROCESS_BACKOFF")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        pid = self.adapter.spawn(self.command, self.state_dir / f"{self.name}.log")
        identity = self.adapter.identity(pid)
        if identity is None:
            raise ProcessStateError("PROCESS_IDENTITY_UNAVAILABLE")
        payload = {"pid": pid, "identity": identity, "command_hash": self.command_hash}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.state_path)
        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            if not self.adapter.is_alive(pid):
                break
            if self.readiness():
                return pid
            if time.monotonic() >= deadline:
                break
            time.sleep(self.poll_seconds)
        self.stop()
        self._retry_after = time.monotonic() + self.backoff_seconds
        raise ProcessStateError("READINESS_TIMEOUT")

    def ready(self) -> bool:
        pid = self._owned_pid()
        return bool(pid is not None and self.adapter.is_alive(pid) and self.readiness())

    def stop(self) -> None:
        pid = self._owned_pid()
        if pid is None:
            self.state_path.unlink(missing_ok=True)
            return
        if self.adapter.is_alive(pid):
            self.adapter.stop_group(pid, self.stop_timeout_seconds)
        self.state_path.unlink(missing_ok=True)
