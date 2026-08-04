from __future__ import annotations

import signal

from traffic_experiment.traffic_measure.capture import DumpcapCapture


def test_capture_stop_on_finish_flushes_with_sigint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "traffic_experiment.traffic_measure.capture.shutil.which",
        lambda _: "/usr/bin/dumpcap",
    )
    output = tmp_path / "capture.pcapng"
    output.write_bytes(b"pcap")
    capture = DumpcapCapture(
        output_path=output,
        interface="llmhost0",
        capture_filter="tcp port 8443",
        duration_seconds=900,
        stop_on_finish=True,
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
