import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from sdilej_to_prehrajto.models import Candidate
from sdilej_to_prehrajto.prehrajto import (
    PrehrajtoError,
    RemoteReader,
    relay_upload,
    response_total_size,
)
from sdilej_to_prehrajto.state import StateStore


class Response:
    def __init__(
        self,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
        *,
        status_code: int = 200,
        payload: dict | None = None,
    ):
        self.raw = io.BytesIO(data)
        self.headers = headers or {}
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        assert self._payload is not None
        return self._payload

    def close(self) -> None:
        pass


def test_remote_reader_reports_remaining_length() -> None:
    reader = RemoteReader(Response(b"abcdef"), 6)
    assert reader.len == 6
    assert reader.read(2) == b"ab"
    assert reader.len == 4
    assert reader.read(10) == b"cdef"
    assert reader.len == 0


def test_content_range_has_priority_over_chunk_length() -> None:
    response = Response(b"", {"Content-Range": "bytes 0-99/1000", "Content-Length": "100"})
    assert response_total_size(response) == 1000


def test_remote_reader_rejects_truncated_source() -> None:
    reader = RemoteReader(Response(b"abc"), 6)
    assert reader.read(3) == b"abc"
    with pytest.raises(PrehrajtoError, match="Source ended"):
        reader.read(3)


class SourceSession:
    def __init__(self, payload: bytes):
        self.payload = payload

    def get(self, *_args, **_kwargs) -> Response:
        return Response(self.payload, {"Content-Length": str(len(self.payload))})


class TargetSession:
    def __init__(self):
        self.uploaded_body = b""
        self.renamed_to = None

    def get(self, *_args, **_kwargs) -> Response:
        return Response()

    def post(self, url, **kwargs) -> Response:
        if "prepareVideo" in url:
            return Response(
                payload={
                    "params": json.dumps({"video_id": 777}),
                    "response": "response",
                    "project": "project",
                    "nonce": "nonce",
                    "signature": "signature",
                }
            )
        if "api.premiumcdn.net" in url:
            encoder = kwargs["data"]
            chunks = []
            while True:
                chunk = encoder.read(7)
                if not chunk:
                    break
                chunks.append(chunk)
            self.uploaded_body = b"".join(chunks)
            return Response(status_code=201)
        self.renamed_to = kwargs["data"]["uploadedVideoListing-name"]
        return Response()


def test_relay_upload_streams_payload_and_renames() -> None:
    source_payload = b"small-video-payload"
    target = TargetSession()
    prepared = []
    candidate = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "film",
        filename="film.mkv",
        mime_type="video/x-matroska",
        download_url="https://data.sdilej.cz/file",
    )
    result = relay_upload(
        target,
        SourceSession(source_payload),
        candidate,
        "Film (2000) 4K",
        on_prepared=lambda video_id, size: prepared.append((video_id, size)),
    )
    assert result.video_id == "777"
    assert result.source_bytes_read == len(source_payload)
    assert source_payload in target.uploaded_body
    assert prepared == [("777", len(source_payload))]
    assert target.renamed_to == "Film (2000) 4K"


def test_prepared_state_blocks_automatic_duplicate_retry(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_prepared(1, "777", 123)
    assert state.pending_prepared(1)
    assert not state.uploaded(1)
    loaded = StateStore(tmp_path / "state.json")
    assert loaded.pending_prepared(1)


def test_success_replaces_prepared_state(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_prepared(1, "777", 123)
    state.record_success(1, {"target_video_id": "777"})
    assert state.uploaded(1)
    assert not state.pending_prepared(1)


def test_no_source_attempt_is_deferred_for_thirty_days(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(1, {"status": "no_acceptable_source", "permanent": False})
    attempted = datetime.fromisoformat(state.film(1)["attempts"][-1]["attempted_at"])
    assert state.deferred(1, at=attempted + timedelta(days=29))
    assert not state.deferred(1, at=attempted + timedelta(days=31))
