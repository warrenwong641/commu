from __future__ import annotations

import os
from pathlib import Path


def _required_directory(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise RuntimeError(f"{name} is not a directory: {path}")
    return str(path)


# Jupyter is deliberately reachable only from the local reverse proxy/tunnel.
c.ServerApp.ip = "127.0.0.1"  # noqa: F821
c.ServerApp.port = int(os.environ.get("JUPYTER_PORT", "8888"))  # noqa: F821
c.ServerApp.port_retries = 0  # noqa: F821
c.ServerApp.open_browser = False  # noqa: F821
c.ServerApp.root_dir = _required_directory("JUPYTER_ROOT_DIR")  # noqa: F821
c.ServerApp.allow_remote_access = True  # noqa: F821
c.ServerApp.trust_xheaders = True  # noqa: F821
c.ServerApp.disable_check_xsrf = False  # noqa: F821
c.ServerApp.allow_root = False  # noqa: F821
c.ServerApp.terminals_enabled = True  # noqa: F821
c.ServerApp.autoreload = False  # noqa: F821
c.ServerApp.quit_button = False  # noqa: F821

# Authentication remains enabled. JUPYTER_TOKEN_FILE is supplied by the user
# service and contains a random 256-bit token readable only by the service user.
