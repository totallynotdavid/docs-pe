from __future__ import annotations

import os
import subprocess
import sys

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local-servers.sh"


def test_a_checkout_owns_its_server_under_a_narrow_terminal(tmp_path: Path) -> None:
    minio_dir = tmp_path / ".data" / "minio"
    minio_dir.mkdir(parents=True)
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "server",
            str(minio_dir),
            "--address",
            "127.0.0.1:25999",
            "--console-address",
            "127.0.0.1:26999",
        ]
    )
    try:
        (tmp_path / ".data" / "minio.pid").write_text(
            f"{server.pid}\n", encoding="utf-8"
        )
        owned = subprocess.run(
            ["bash", "-c", f". {SCRIPT}; minio_owned"],
            cwd=tmp_path,
            env={**os.environ, "COLUMNS": "40", "PORTAL_DEV_MINIO_PORT": "25999"},
            check=False,
        )
    finally:
        server.kill()
        server.wait()

    assert owned.returncode == 0
