"""Render portable user service templates without activating them."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "deploy" / "systemd"
UNIT_NAMES = ("dimon-gpu-host.service", "dimon-inference-gateway.service")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.host")
    args = parser.parse_args()
    env_file = args.env_file.resolve()
    if not env_file.is_file() or env_file.stat().st_mode & 0o077:
        parser.error("environment file must exist and have mode 600")
    args.destination.mkdir(parents=True, exist_ok=True)
    for name in UNIT_NAMES:
        rendered = (TEMPLATES / name).read_text(encoding="utf-8")
        rendered = rendered.replace("@REPO_ROOT@", str(ROOT)).replace("@ENV_FILE@", str(env_file))
        (args.destination / name).write_text(rendered, encoding="utf-8")
    print(f"rendered {len(UNIT_NAMES)} unit files; activation remains manual")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
