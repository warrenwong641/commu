from __future__ import annotations

import os
import stat
from pathlib import Path


def _required_directory(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise RuntimeError(f"{name} is not a directory: {path}")
    return str(path)


def _required_password_hash(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    unresolved = Path(value).expanduser()
    if unresolved.is_symlink():
        raise RuntimeError(f"{name} must not be a symlink: {unresolved}")
    path = unresolved.resolve(strict=True)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{name} is not a regular file: {path}")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"{name} is not owned by the service user: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != 0o600:
            raise RuntimeError(
                f"{name} must have mode 0600, got {mode:04o}: {path}"
            )
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0].startswith("argon2:$argon2"):
        raise RuntimeError(f"{name} is not a single Argon2 password verifier")
    return lines[0]


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

# Persist only an Argon2 verifier. The plaintext password is never written to a
# file, environment variable, command line, config, log, or packet capture.
c.IdentityProvider.token = ""  # noqa: F821
c.PasswordIdentityProvider.hashed_password = _required_password_hash(  # noqa: F821
    "JUPYTER_PASSWORD_HASH_FILE"
)
