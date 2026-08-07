from __future__ import annotations

import io
import signal
import tempfile
from pathlib import Path

from traffic_experiment.traffic_measure.capture import DumpcapCapture

# dumpcap is at .tools/usr/bin/dumpcap — shutil.which won't find it
# unless PATH includes that directory.  Use the absolute path.
_DUMPCAP = "fake-dumpcap"


class _RunningProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.received_signal = None
        self.stderr = io.StringIO("")

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.received_signal = value
        self.returncode = 0

    def communicate(self, timeout=None):
        self.returncode = 0
        return "", ""


def _install_fake_dumpcap(monkeypatch, *, exits_immediately: bool = False) -> None:
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.capture.shutil.which",
        lambda _: _DUMPCAP,
    )
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.capture.time.sleep",
        lambda _: None,
    )

    def fake_popen(command, **_kwargs):
        if exits_immediately:
            process = _RunningProcess()
            process.returncode = 2
            process.stderr = io.StringIO("invalid interface")
            return process
        output = Path(command[command.index("-w") + 1])
        output.write_bytes(b"pcap")
        return _RunningProcess()

    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.capture.subprocess.Popen",
        fake_popen,
    )


def test_capture_stop_on_finish_flushes_with_sigint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.capture.shutil.which",
        lambda _: _DUMPCAP,
    )
    output = tmp_path / "capture.pcapng"
    output.write_bytes(b"pcap")
    capture = DumpcapCapture(
        output_path=output,
        interface="llmhost0",
        capture_filter="tcp port 8443",
        duration_seconds=900,
        stop_on_finish=True,
        executable=_DUMPCAP,
    )

    class Process:
        returncode = 0
        received_signal = None

        def poll(self):
            return None

        def send_signal(self, value):
            self.received_signal = value

        def communicate(self, timeout):
            assert timeout == 910
            return "", ""

    process = Process()
    capture.process = process
    result = capture.finish()

    assert process.received_signal == signal.SIGINT
    assert result.path == output
    assert result.return_code == 0


def test_staging_creates_temp_dir_and_cleans_up(tmp_path, monkeypatch):
    """Staging PCAP is written to /tmp, moved to final, staging cleaned."""
    _install_fake_dumpcap(monkeypatch)
    final = tmp_path / "final.pcapng"
    cap = DumpcapCapture(
        output_path=final,
        interface="lo",
        capture_filter="tcp port 9999",
        duration_seconds=3,
        startup_delay_seconds=0.2,
        executable=_DUMPCAP,
    )
    assert cap._staging_dir.exists()
    assert cap._staging_dir.parent == Path(tempfile.gettempdir())
    assert cap._staging_path.name == final.name
    assert cap._staging_path != final

    cap.start()
    cap.process.send_signal(signal.SIGINT)
    result = cap.finish()

    assert not cap._staging_dir.exists()
    assert not cap._staging_path.exists()
    if result.path is not None:
        assert result.path == final
        assert final.exists()


def test_staging_atomic_move_to_final(tmp_path, monkeypatch):
    """After successful finish, PCAP is moved from staging to final path."""
    _install_fake_dumpcap(monkeypatch)
    final = tmp_path / "final.pcapng"
    cap = DumpcapCapture(
        output_path=final,
        interface="lo",
        capture_filter="tcp port 65535",
        duration_seconds=2,
        startup_delay_seconds=0.5,
        executable=_DUMPCAP,
    )
    assert cap._staging_dir.exists()
    cap.start()
    cap.process.send_signal(signal.SIGINT)
    result = cap.finish()
    assert not cap._staging_dir.exists()
    assert result.path == final
    assert final.exists()


def test_staging_cleanup_on_error(tmp_path, monkeypatch):
    """Staging directory is cleaned even when an exception occurs during start."""
    _install_fake_dumpcap(monkeypatch, exits_immediately=True)
    final = tmp_path / "final.pcapng"
    cap = DumpcapCapture(
        output_path=final,
        interface="nonexistent_interface_xyz",
        capture_filter="tcp port 65535",
        duration_seconds=1,
        startup_delay_seconds=0.1,
        executable=_DUMPCAP,
    )
    staging = cap._staging_dir
    assert staging.exists()
    try:
        cap.start()
    except RuntimeError:
        pass
    # dumpcap exits immediately with bad interface; staging must be cleaned
    assert not staging.exists()
