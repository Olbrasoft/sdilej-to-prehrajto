from __future__ import annotations

import multiprocessing
import os
import subprocess
import tempfile
import threading
from collections import defaultdict
from multiprocessing.connection import Connection
from pathlib import Path


class LanguageDetectionError(RuntimeError):
    pass


def _whisper_worker(connection: Connection, settings: dict[str, object]) -> None:
    """Keep Whisper isolated so a stuck native inference can be terminated."""
    try:
        from faster_whisper import WhisperModel  # type: ignore

        model = WhisperModel(**settings)
        connection.send(("ready", None))
        while True:
            sample = connection.recv()
            if sample is None:
                return
            try:
                _segments, info = model.transcribe(
                    sample, beam_size=1, vad_filter=True
                )
                connection.send(
                    (
                        "result",
                        (info.language or "").lower(),
                        float(info.language_probability or 0.0),
                    )
                )
            except Exception as error:
                connection.send(("error", type(error).__name__))
    except (EOFError, BrokenPipeError):
        return
    except Exception as error:
        try:
            connection.send(("error", type(error).__name__))
        except (EOFError, BrokenPipeError):
            pass
    finally:
        connection.close()


class WhisperLanguageDetector:
    def __init__(
        self, *, seconds: int = 75, inference_timeout_seconds: int | None = None
    ):
        self.seconds = seconds
        self.inference_timeout_seconds = (
            inference_timeout_seconds
            if inference_timeout_seconds is not None
            else int(os.environ.get("WHISPER_INFERENCE_TIMEOUT_SECONDS", "180"))
        )
        if self.inference_timeout_seconds <= 0:
            raise ValueError("Whisper inference timeout must be positive")
        self._model = None
        self._worker_process = None
        self._worker_connection: Connection | None = None
        self._worker_lock = threading.RLock()

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

    def _stop_worker(self) -> None:
        connection = self._worker_connection
        process = self._worker_process
        self._worker_connection = None
        self._worker_process = None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

    def _start_worker(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe()
        settings = {
            "model_size_or_path": os.environ.get("WHISPER_MODEL", "small"),
            "device": os.environ.get("WHISPER_DEVICE", "cpu"),
            "compute_type": os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
            "cpu_threads": int(os.environ.get("WHISPER_CPU_THREADS", "0")),
            "num_workers": int(os.environ.get("WHISPER_NUM_WORKERS", "1")),
        }
        process = context.Process(
            target=_whisper_worker,
            args=(child_connection, settings),
            daemon=True,
        )
        process.start()
        child_connection.close()
        self._worker_process = process
        self._worker_connection = parent_connection
        message = self._receive_worker_message("Whisper model startup")
        if message[0] != "ready":
            self._stop_worker()
            raise LanguageDetectionError(
                f"Whisper model startup failed ({message[1]})"
            )

    def _receive_worker_message(self, operation: str) -> tuple:
        assert self._worker_connection is not None
        try:
            if not self._worker_connection.poll(self.inference_timeout_seconds):
                self._stop_worker()
                raise LanguageDetectionError(f"{operation} timed out")
            message = self._worker_connection.recv()
        except (EOFError, OSError) as error:
            self._stop_worker()
            raise LanguageDetectionError(
                f"{operation} stopped unexpectedly"
            ) from error
        if not isinstance(message, tuple) or not message:
            self._stop_worker()
            raise LanguageDetectionError("Whisper worker returned invalid output")
        return message

    def _transcribe(self, sample: Path) -> tuple[str, float]:
        # Tests and callers may inject a lightweight model explicitly. Normal
        # production inference runs in a reusable child process with a hard
        # deadline because native Whisper code cannot be stopped by a thread.
        if self._model is not None:
            _segments, info = self._model.transcribe(
                str(sample), beam_size=1, vad_filter=True
            )
            return (info.language or "").lower(), float(
                info.language_probability or 0.0
            )
        with self._worker_lock:
            if self._worker_process is None or not self._worker_process.is_alive():
                self._stop_worker()
                self._start_worker()
            assert self._worker_connection is not None
            try:
                self._worker_connection.send(str(sample))
            except (BrokenPipeError, EOFError, OSError) as error:
                self._stop_worker()
                raise LanguageDetectionError(
                    "Whisper inference worker is unavailable"
                ) from error
            message = self._receive_worker_message("Whisper inference")
            if message[0] == "error":
                self._stop_worker()
                raise LanguageDetectionError(
                    f"Whisper inference failed ({message[1]})"
                )
            if message[0] != "result":
                self._stop_worker()
                raise LanguageDetectionError("Whisper worker returned invalid output")
            return str(message[1]), float(message[2])

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
            language, probability = self._transcribe(sample)
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
