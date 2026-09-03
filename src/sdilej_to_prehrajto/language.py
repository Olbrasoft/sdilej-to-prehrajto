from __future__ import annotations

import os
import subprocess
import tempfile
from collections import defaultdict
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
            cpu_threads=int(os.environ.get("WHISPER_CPU_THREADS", "0")),
            num_workers=int(os.environ.get("WHISPER_NUM_WORKERS", "1")),
        )
        return self._model

    def _detect_at(self, media_url: str, offset: int) -> tuple[str, float]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sample = Path(temporary_directory) / "sample.wav"
            ffmpeg_timeout = max(
                self.seconds + 30,
                int(os.environ.get("WHISPER_FFMPEG_TIMEOUT_SECONDS", "120")),
            )
            command = [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-rw_timeout",
                str(ffmpeg_timeout * 1_000_000),
                "-ss",
                str(offset),
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
                timeout=ffmpeg_timeout,
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

    def detect(self, media_url: str) -> tuple[str, float]:
        return self._detect_at(
            media_url, int(os.environ.get("WHISPER_SAMPLE_OFFSET", "180"))
        )

    def detect_consensus(
        self,
        media_url: str,
        duration_sec: int | None,
        *,
        initial: tuple[str, float] | None = None,
        preferred_language: str | None = None,
    ) -> tuple[str, float]:
        """Resolve a filename/Whisper conflict from dispersed movie samples."""
        duration = max(int(duration_sec or 0), 900)
        offsets = [int(duration * fraction) for fraction in (0.25, 0.5, 0.75)]
        samples = ([initial] if initial else []) + [
            self._detect_at(media_url, offset) for offset in offsets
        ]
        grouped: dict[str, list[float]] = defaultdict(list)
        for language, probability in samples:
            grouped[language.lower()].append(float(probability))
        preferred = (preferred_language or "").lower()
        if preferred in grouped and max(grouped[preferred]) >= 0.55:
            probabilities = grouped[preferred]
            return preferred, max(probabilities)
        winner, probabilities = max(
            grouped.items(),
            key=lambda item: (len(item[1]), sum(item[1]), item[0]),
        )
        return winner, sum(probabilities) / len(probabilities)
