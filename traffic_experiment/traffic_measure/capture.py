from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CaptureResult:
    path: Path | None
    return_code: int | None
    stderr: str


class DumpcapCapture:
    def __init__(
        self,
        output_path: Path,
        interface: str,
        capture_filter: str,
        duration_seconds: int,
        startup_delay_seconds: float = 0.5,
        executable: str = "dumpcap",
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
        self.executable = executable
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.executable,
            "-q",
            "-i",
            self.interface,
            "-a",
            f"duration:{self.duration_seconds}",
            "-w",
            str(self.output_path),
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
            raise RuntimeError(f"dumpcap exited before the request: {stderr.strip()}")

    def finish(self) -> CaptureResult:
        if self.process is None:
            raise RuntimeError("capture was not started")
        try:
            _, stderr = self.process.communicate(timeout=self.duration_seconds + 10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                _, stderr = self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, stderr = self.process.communicate()
        return CaptureResult(
            path=self.output_path if self.output_path.exists() else None,
            return_code=self.process.returncode,
            stderr=stderr.strip(),
        )
