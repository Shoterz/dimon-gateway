"""Render portable user service templates without activating them."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "deploy" / "systemd"
RENDER_TARGETS = (
    ("dimon-gpu-host.service", Path("dimon-gpu-host.service")),
    ("dimon-inference-gateway.service", Path("dimon-inference-gateway.service")),
    ("dimon-faster-whisper-medium.service", Path("dimon-faster-whisper-medium.service")),
    (
        "dimon-inference-gateway-host-upstreams.conf",
        Path("dimon-inference-gateway.service.d/host-upstreams.conf"),
    ),
    (
        "gpu-overlord-stack-dimon.conf",
        Path("gpu-overlord-stack.service.d/dimon.conf"),
    ),
)


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
    for template_name, destination_name in RENDER_TARGETS:
        rendered = (TEMPLATES / template_name).read_text(encoding="utf-8")
        rendered = rendered.replace("@REPO_ROOT@", str(ROOT)).replace("@ENV_FILE@", str(env_file))
        destination = args.destination / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(f"rendered {len(RENDER_TARGETS)} systemd files; activation remains manual")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
