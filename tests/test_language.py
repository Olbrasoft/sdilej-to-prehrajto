from pathlib import Path
from types import SimpleNamespace
import subprocess
import traceback

import pytest

from sdilej_to_prehrajto.language import (
    AudioSampleError,
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


def test_ffmpeg_timeout_is_recoverable_and_does_not_expose_session(monkeypatch):
    sample_paths = []

    def timeout(command, **kwargs):
        sample_paths.append(Path(command[-1]))
        sample_paths[-1].write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", timeout)
    monkeypatch.delenv("WHISPER_FFMPEG_TIMEOUT_SECONDS", raising=False)
    detector = WhisperLanguageDetector(seconds=75)
    media_url = "https://source.example/video?session=private-token"
    with pytest.raises(AudioSampleError, match="timed out after 120 seconds") as caught:
        detector.detect(media_url)

    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "private-token" not in diagnostic
    assert "source.example" not in diagnostic
    assert not sample_paths[0].parent.exists()


@pytest.mark.parametrize('probabilities', [
    [.473, .730, .456, .501],
    [.334, .334, .334, .334],
    [.50, .64, .60, .55],
])
def test_unanimous_sections_preserve_the_strongest_observed_confidence(monkeypatch, probabilities):
    detector = WhisperLanguageDetector()
    remaining = iter(probabilities[1:])
    offsets = []

    def detect_at(_url, offset):
        offsets.append(offset)
        return 'es', next(remaining)

    monkeypatch.setattr(detector, '_detect_at', detect_at)
    language, confidence = detector.detect_consensus('media', 6000, initial=('es', probabilities[0]))
    assert language == 'es'
    assert confidence == max(probabilities)
    assert offsets == [1500, 3000, 4500]
    # Unanimous weak guesses cannot manufacture confidence above the floor.
    assert (confidence >= .65) == any(value >= .65 for value in probabilities)


def test_conflicting_sections_do_not_use_the_single_strongest_sample(monkeypatch):
    detector = WhisperLanguageDetector()
    samples = iter([('es', .97), ('en', .45), ('en', .49)])
    monkeypatch.setattr(detector, '_detect_at', lambda *_args: next(samples))
    language, confidence = detector.detect_consensus('media', 6000, initial=('es', .3))
    assert language == 'es'
    assert confidence == pytest.approx((.3 + .97) / 2)
    assert confidence < .65


def test_unanimous_speech_overrides_an_unconfirmed_filename_hint(monkeypatch):
    detector = WhisperLanguageDetector()
    samples = iter([('es', .73), ('es', .46), ('es', .50)])
    monkeypatch.setattr(detector, '_detect_at', lambda *_args: next(samples))
    assert detector.detect_consensus('media', 6000, initial=('es', .47),
                                     preferred_language='cs') == ('es', .73)
