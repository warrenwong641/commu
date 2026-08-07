from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CaptureResult:
    path: Path | None
    return_code: int | None
    stderr: str


class DumpcapCapture:
    """Start dumpcap, stage the live capture in a secure temporary directory,
    then atomically move the completed file to *output_path*.

    dumpcap is a file-capability binary (cap_net_raw,cap_net_admin=ep).
    Inside a network namespace (``ip netns exec``) the kernel strips
    CAP_DAC_OVERRIDE, so dumpcap cannot write to root-owned directories
    in the experiment tree.  Writing to a private temp directory under
    */tmp* (world-writable) avoids that interaction entirely.

    The final path and its parent directories are created as usual so the
    runner can validate their existence before the request begins.
    """

    def __init__(
        self,
        output_path: Path,
        interface: str,
        capture_filter: str,
        duration_seconds: int,
        startup_delay_seconds: float = 0.5,
        stop_on_finish: bool = False,
        executable: str = "dumpcap",
        tmp_dir: str | None = None,
    ) -> None:
        if not interface:
            raise ValueError("capture interface is required unless --no-capture is used")
        if duration_seconds <= 0:
            raise ValueError("capture duration must be positive")
        if shutil.which(executable) is None:
            raise FileNotFoundError(f"{executable} was not found in PATH")
        self.output_path = output_path
        self.interface = interface
        self.capture_filter = capture_filter
        self.duration_seconds = duration_seconds
        self.startup_delay_seconds = startup_delay_seconds
        self.stop_on_finish = stop_on_finish
        self.executable = executable
        self.process: subprocess.Popen[str] | None = None

        # Staging directory inside a world-writable filesystem so dumpcap
        # can create files regardless of capability/namespace interactions.
        base = Path(os.environ.get("CAPTURE_TMP_DIR", tmp_dir or tempfile.gettempdir()))
        base.mkdir(parents=True, exist_ok=True)
        self._staging_dir = Path(
            tempfile.mkdtemp(prefix="commu_capture_", dir=str(base))
        )
        self._staging_dir.chmod(0o700)
        self._staging_path = self._staging_dir / output_path.name

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Create final parent directories, then start dumpcap writing to
        the staging path."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.executable,
            "-q",
            "-i",
            self.interface,
            "-a",
            f"duration:{self.duration_seconds}",
            "-w",
            str(self._staging_path),
        ]
        if self.capture_filter:
            command.extend(["-f", self.capture_filter])
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(self.startup_delay_seconds)
        if self.process.poll() is not None:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            self._cleanup_staging()
            raise RuntimeError(f"dumpcap exited before the request: {stderr.strip()}")

    # ------------------------------------------------------------------
    def finish(self) -> CaptureResult:
        """Wait for dumpcap, atomically move the staging file to the
        final output path, then remove the staging directory."""
        if self.process is None:
            raise RuntimeError("capture was not started")
        if self.stop_on_finish and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        try:
            _, stderr = self.process.communicate(timeout=self.duration_seconds + 10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                _, stderr = self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, stderr = self.process.communicate()

        final_path: Path | None = None
        staging = getattr(self, "_staging_path", None)
        if staging is not None and staging.exists() and staging.stat().st_size > 0:
            shutil.move(str(staging), str(self.output_path))
            final_path = self.output_path
        elif (staging is None or not staging.exists()) and self.output_path.exists():
            # No staging path (legacy mock) or staging was never populated —
            # use the output path directly if it already exists.
            final_path = self.output_path

        self._cleanup_staging()
        return CaptureResult(
            path=final_path,
            return_code=self.process.returncode,
            stderr=stderr.strip() if stderr else "",
        )

    # ------------------------------------------------------------------
    def _cleanup_staging(self) -> None:
        """Remove the staging directory and any leftover files.  Never
        raises — cleanup is best-effort."""
        staging = getattr(self, "_staging_path", None)
        if staging is not None and staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
        sdir = getattr(self, "_staging_dir", None)
        if sdir is not None and sdir.exists():
            try:
                sdir.rmdir()
            except OSError:
                pass

    def __del__(self) -> None:
        self._cleanup_staging()
