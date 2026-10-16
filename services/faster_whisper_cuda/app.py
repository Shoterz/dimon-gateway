"""Loopback frontend for the GPU-managed Faster-Whisper worker."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from services.faster_whisper_cuda.install import RuntimeLayout, runtime_environment
from services.faster_whisper_cuda.managed import ManagedCudaWhisper


def create_frontend_app(
    manager: ManagedCudaWhisper,
    *,
    websocket_connect=None,
    shutdown_callback: Callable[[], object] | None = None,
) -> FastAPI:
    async def clear_active_job() -> None:
        if shutdown_callback is None:
            snapshot = manager.snapshot()
            if snapshot.get("active_jobs"):
                await asyncio.to_thread(manager.end_job)
            return
        result = shutdown_callback()
        if asyncio.iscoroutine(result):
            await result

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await clear_active_job()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return manager.snapshot()

    @app.post("/warm")
    async def warm() -> dict[str, object]:
        try:
            return await asyncio.to_thread(manager.ensure_loaded)
        except Exception:
            return manager.snapshot()

    @app.post("/probe")
    async def probe() -> dict[str, object]:
        try:
            return await asyncio.to_thread(manager.run_probe)
        except Exception:
            return manager.snapshot()

    @app.websocket("/ws")
    async def proxy(websocket: WebSocket) -> None:
        await websocket.accept()
        began = False
        connection = None
        try:
            await asyncio.to_thread(manager.begin_job)
            began = True
            connector = websocket_connect
            if connector is None:
                from websockets.asyncio.client import connect

                connector = connect
            connection = connector(
                "ws://127.0.0.1:8008/ws",
                open_timeout=3,
                close_timeout=0.2,
                ping_interval=None,
                max_size=1_048_576,
                proxy=None,
            )
            child = await connection.__aenter__()
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                chunk = message.get("bytes")
                if isinstance(chunk, bytes):
                    await child.send(chunk)
                    continue
                text = message.get("text")
                if text != "done":
                    await websocket.close(code=1008)
                    return
                await child.send("done")
                response = await asyncio.wait_for(child.recv(), timeout=90)
                if not isinstance(response, str) or len(response) > 1_048_576:
                    raise RuntimeError("WORKER_RESPONSE_INVALID")
                await websocket.send_text(response)
                return
        except WebSocketDisconnect:
            return
        except Exception:
            await websocket.close(code=1011)
        finally:
            if connection is not None:
                try:
                    await connection.__aexit__(None, None, None)
                except Exception:
                    pass
            if began:
                await asyncio.to_thread(manager.end_job)

    return app


def build_managed_service(environ: Mapping[str, str] | None = None):
    env = dict(os.environ if environ is None else environ)
    repository = Path(__file__).resolve().parents[2]
    layout = RuntimeLayout.for_repository(repository)
    # Keep the venv's bin/python path intact.  Resolving its intentional
    # symlink to /usr/bin/python would discard pyvenv.cfg and run outside the
    # isolated CUDA environment.
    worker_python = Path(env["DIMON_FASTER_WHISPER_PYTHON"])
    model_path = Path(env["DIMON_FASTER_WHISPER_MODEL_PATH"]).resolve()
    client_directory = Path(env["DIMON_GPU_OVERLORD_CLIENT_DIR"]).resolve()
    if str(client_directory) not in sys.path:
        sys.path.insert(0, str(client_directory))
    from gpu_overlord_client import ManagedServiceClient

    holder: dict[str, ManagedCudaWhisper] = {}
    socket_value = env.get("DIMON_GPU_OVERLORD_SOCKET", "").strip()
    broker = ManagedServiceClient(
        "faster_whisper",
        Path(socket_value) if socket_value else None,
        lambda: holder["manager"].evict_if_idle(),
    )
    worker_environment = runtime_environment(layout, env)
    worker_environment.update(
        {
            "DIMON_FASTER_WHISPER_MODEL_PATH": str(model_path),
            "DIMON_FASTER_WHISPER_MODEL": "medium",
            "DIMON_FASTER_WHISPER_DEVICE": "cuda",
            "DIMON_FASTER_WHISPER_COMPUTE_TYPE": "int8_float16",
            "DIMON_FASTER_WHISPER_WORKER_HOST": "127.0.0.1",
            "DIMON_FASTER_WHISPER_WORKER_PORT": "8008",
        }
    )
    manager = ManagedCudaWhisper(
        broker=broker,
        worker_command=(
            str(worker_python),
            "-m",
            "services.faster_whisper_cuda.worker",
        ),
        worker_environment=worker_environment,
    )
    holder["manager"] = manager
    return manager, broker


def main() -> None:
    host = os.environ.get("DIMON_FASTER_WHISPER_FRONTEND_HOST", "127.0.0.1")
    port = int(os.environ.get("DIMON_FASTER_WHISPER_FRONTEND_PORT", "8007"))
    if host != "127.0.0.1" or port != 8007:
        raise ValueError("frontend must use 127.0.0.1:8007")
    manager, broker = build_managed_service()
    broker.start()
    async def shutdown() -> None:
        await asyncio.to_thread(manager.end_job)
        await asyncio.to_thread(manager.close)
        broker.close()

    app = create_frontend_app(manager, shutdown_callback=shutdown)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
