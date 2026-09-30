"""Exercise real WAV decoding, VAD and isolated Whisper inference, without secrets."""
import math
import struct
import tempfile
import wave
from pathlib import Path

from sdilej_to_prehrajto.language import WhisperLanguageDetector


def main():
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "runtime-check.wav"
        with wave.open(str(sample), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"".join(
                struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * index / 16000)))
                for index in range(16000 * 3)
            ))
        detector = WhisperLanguageDetector()
        try:
            language, probability = detector._transcribe(sample)
            if not language or not 0 <= probability <= 1:
                raise RuntimeError("Whisper runtime returned invalid language output")
            print("whisper_runtime_check=passed", flush=True)
        finally:
            detector._stop_worker()


if __name__ == "__main__":
    main()
