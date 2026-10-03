"""
Desktop window launcher.

Starts the local API server and opens a native window via pywebview.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path


def _free_port(preferred: int = 8787) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def _wait_ready(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def run_desktop(
    *,
    config_path: str | Path = "config.yaml",
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
    """Launch the control panel inside a native desktop window."""
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: uvicorn. Install with: pip install -r requirements.txt"
        ) from exc

    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: pywebview. Install with: pip install pywebview"
        ) from exc

    from gui.app import create_app

    cfg = str(Path(config_path).resolve())
    bind_port = port or _free_port(8787)
    app = create_app(config_path=cfg)

    def _serve() -> None:
        uvicorn.run(
            app,
            host=host,
            port=bind_port,
            log_level="warning",
            access_log=False,
        )

    thread = threading.Thread(target=_serve, name="moodle-gui-server", daemon=True)
    thread.start()

    if not _wait_ready(host, bind_port):
        raise SystemExit(f"GUI server failed to start on {host}:{bind_port}")

    url = f"http://{host}:{bind_port}/"
    window = webview.create_window(
        title="Moodle Solver",
        url=url,
        width=1360,
        height=900,
        min_size=(980, 680),
        background_color="#0B0D10",
        text_select=True,
    )
    webview.start(debug=False)
    _ = window
