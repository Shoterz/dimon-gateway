"""Bind the compatible Kokoro Gradio application to loopback."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--host", choices=("127.0.0.1",), required=True)
    parser.add_argument("--port", type=int, choices=(3001,), required=True)
    args = parser.parse_args()
    app_dir = Path(args.app_dir).resolve()
    os.chdir(app_dir)
    spec = importlib.util.spec_from_file_location("dimon_kokoro_compat", app_dir / "app.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Kokoro compatibility application could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    demo = module.gr.TabbedInterface(
        [module.ui(), module.tutorial()],
        ["Multilingual TTS", "VoicePack Explanation"],
        title="Kokoro TTS",
    )
    demo.queue().launch(
        debug=False,
        share=False,
        server_name=args.host,
        server_port=args.port,
    )


if __name__ == "__main__":
    main()
