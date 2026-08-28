from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


class LanguageDetectionError(RuntimeError):
    pass


class WhisperLanguageDetector:
    def __init__(self, *, seconds: int = 75):
        self.seconds = seconds
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as error:
            raise LanguageDetectionError("faster-whisper is not installed") from error
        self._model = WhisperModel(
            os.environ.get("WHISPER_MODEL", "small"),
            device=os.environ.get("WHISPER_DEVICE", "cpu"),
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
        )
        return self._model

    def detect(self, media_url: str) -> tuple[str, float]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sample = Path(temporary_directory) / "sample.wav"
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                os.environ.get("WHISPER_SAMPLE_OFFSET", "180"),
                "-t",
                str(self.seconds),
                "-i",
                media_url,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(sample),
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(self.seconds * 4, 300),
                check=False,
            )
            if result.returncode != 0 or not sample.exists() or sample.stat().st_size == 0:
                raise LanguageDetectionError("ffmpeg could not create an audio sample")
            model = self._load_model()
            _segments, info = model.transcribe(
                str(sample), beam_size=1, vad_filter=True
            )
            language = (info.language or "").lower()
            probability = float(info.language_probability or 0.0)
            if not language:
                raise LanguageDetectionError("Whisper returned no language")
            return language, probability
