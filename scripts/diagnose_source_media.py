"""Read-only source diagnostics without logging authenticated media addresses."""
import json
import os
import importlib.metadata
import traceback

import requests

from sdilej_to_prehrajto.language import LanguageDetectionError, WhisperLanguageDetector
from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto.sdilej import SdilejError, login, parse_detail_html, probe_media


def report(stage, **fields):
    print(json.dumps({"stage": stage, **fields}), flush=True)


def main():
    report("versions", packages={name: importlib.metadata.version(name) for name in (
        "faster-whisper", "ctranslate2", "onnxruntime", "numpy", "tokenizers", "av")})
    session = login(os.environ["SDILEJ_EMAIL"], os.environ["SDILEJ_PASSWORD"])
    candidate = Candidate("22949062", "https://sdilej.cz/22949062/hlubina-1977-cz-1080p.mkv", "Hlubina (1977)")
    response = session.get(candidate.url, timeout=45)
    response.raise_for_status()
    detail = parse_detail_html(response.text, candidate)
    report("detail", source_id=detail.source_id, separate_player=detail.sample_url != detail.download_url)
    detector = WhisperLanguageDetector()
    # Diagnostic only: keep inference in this bounded workflow process so its
    # stack can be inspected. Production retains its isolated hard watchdog.
    detector._load_model()
    try:
        for kind, url in (("player", detail.sample_url), ("original", detail.download_url)):
            try:
                with session.get(url, headers={"Range": "bytes=0-0", "Referer": candidate.url}, stream=True, timeout=(20, 30)) as response:
                    report(kind + "_transport", status=response.status_code,
                           content_type=response.headers.get("Content-Type", ""),
                           redirects=len(response.history))
                    response.raise_for_status()
                    resolved = response.url
                report(kind + "_metadata", metadata=probe_media(resolved))
                language, probability = detector.detect(resolved)
                report(kind + "_language", language=language, probability=probability)
            except LanguageDetectionError as error:
                # This exception contains only fixed internal messages, never URLs.
                report(kind + "_error", error_type=type(error).__name__, reason=str(error))
            except (SdilejError, requests.RequestException) as error:
                report(kind + "_error", error_type=type(error).__name__)
            except TypeError as error:
                report(kind + "_error", error_type=type(error).__name__, reason=str(error)[:600],
                       frames=[{"file": os.path.basename(frame.filename), "function": frame.name,
                                "line": frame.lineno} for frame in traceback.extract_tb(error.__traceback__)])
    finally:
        detector._stop_worker()
        session.close()


if __name__ == "__main__":
    main()
