"""Validated standalone host configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .capacity import CapacityProfile


@dataclass(frozen=True)
class HostConfig:
    safety_reserve_mb: int
    profiles: dict[str, CapacityProfile]

    @classmethod
    def load(cls, path: str | Path) -> "HostConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("config_version") != 1:
            raise ValueError("invalid GPU host configuration")
        reserve = raw.get("safety_reserve_mb")
        profiles_raw = raw.get("profiles")
        if not isinstance(reserve, int) or reserve < 0 or not isinstance(profiles_raw, dict):
            raise ValueError("invalid GPU capacity configuration")
        if set(profiles_raw) != {"qwen", "nemotron", "kokoro"}:
            raise ValueError("GPU profiles must define qwen, nemotron, and kokoro")
        profiles = {}
        for name, item in profiles_raw.items():
            if not isinstance(item, dict):
                raise ValueError("invalid GPU profile")
            profiles[name] = CapacityProfile(name=name, **item)
        return cls(reserve, profiles)
