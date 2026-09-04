from pathlib import Path
from types import SimpleNamespace

import pytest

from sdilej_to_prehrajto.language import (
    LanguageDetectionError,
    WhisperLanguageDetector,
)


def test_remote_sample_has_bounded_ffmpeg_timeout(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        Path(command[-1]).write_bytes(b"wav")
        return SimpleNamespace(returncode=0)

    class FakeModel:
        def transcribe(self, _path, **_kwargs):
            return [], SimpleNamespace(language="cs", language_probability=0.9)

    monkeypatch.delenv("WHISPER_FFMPEG_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr("subprocess.run", fake_run)
    detector = WhisperLanguageDetector(seconds=75)
    detector._model = FakeModel()

    assert detector.detect("https://stream.example/video") == ("cs", 0.9)
    assert seen["timeout"] == 120
    command = seen["command"]
    assert command[command.index("-rw_timeout") + 1] == "120000000"
    assert "-nostdin" in command


def test_stuck_whisper_worker_is_killed_after_timeout(tmp_path) -> None:
    class FakeConnection:
        closed = False

        def send(self, _sample):
            return None

        def poll(self, _timeout):
            return False

        def close(self):
            self.closed = True

    class FakeProcess:
        killed = False
        joined = False

        def is_alive(self):
            return True

        def kill(self):
            self.killed = True

        def join(self, timeout):
            assert timeout == 5
            self.joined = True

    connection = FakeConnection()
    process = FakeProcess()
    detector = WhisperLanguageDetector(inference_timeout_seconds=1)
    detector._worker_connection = connection
    detector._worker_process = process
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"wav")

    with pytest.raises(LanguageDetectionError, match="inference timed out"):
        detector._transcribe(sample)

    assert connection.closed
    assert process.killed
    assert process.joined
