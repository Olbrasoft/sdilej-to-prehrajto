from pathlib import Path
from types import SimpleNamespace

from sdilej_to_prehrajto.language import WhisperLanguageDetector


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
