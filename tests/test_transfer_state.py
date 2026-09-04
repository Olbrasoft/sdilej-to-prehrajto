import io
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from sdilej_to_prehrajto.models import Candidate, LanguageTier, MatchTier
from sdilej_to_prehrajto.prehrajto import (
    PrehrajtoError,
    RemoteReader,
    relay_upload,
    response_total_size,
    uploaded_video_confirmed,
    uploaded_video_id_by_name,
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
        text: str = "",
    ):
        self.raw = io.BytesIO(data)
        self.headers = headers or {}
        self.status_code = status_code
        self._payload = payload
        self.text = text

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


def test_uploaded_video_lookup_requires_exact_display_name() -> None:
    class ListingSession:
        def __init__(self):
            self.params = None

        def get(self, *_args, **kwargs):
            self.params = kwargs.get("params")
            return Response(
                text=(
                    '<div data-video-id="777"><h3>Film (2000) 4K CZ Dabing.mkv '
                    '(Zpracovává se)</h3></div>'
                    '<div data-video-id="778"><input value="Film (2000) 4K.mkv"></div>'
                )
            )

    session = ListingSession()
    assert uploaded_video_id_by_name(session, "Film (2000) 4K CZ Dabing") == "777"
    assert not uploaded_video_confirmed(
        session, "777", "Film (2000) 4K CZ Dabing"
    )
    assert session.params == {"searchPhrase": "Film (2000) 4K CZ Dabing"}
    assert uploaded_video_id_by_name(ListingSession(), "Film (2000)") is None


def test_uploaded_video_confirmation_requires_id_from_the_matching_row() -> None:
    class ListingSession:
        def get(self, *_args, **_kwargs):
            return Response(
                text=(
                    '<div data-video-id="777"><h3>Different Film.mkv</h3></div>'
                    '<div data-video-id="778"><h3>Film (2000) 4K</h3></div>'
                )
            )

    session = ListingSession()
    assert not uploaded_video_confirmed(session, "777", "Film (2000) 4K")
    assert uploaded_video_confirmed(session, "778", "Film (2000) 4K")


def test_candidate_round_trip_restores_enum_values() -> None:
    original = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "Film",
        language_tier=LanguageTier.CZECH_AUDIO,
        match_tier=MatchTier.STRONG,
    )
    restored = Candidate.from_dict(original.to_dict())
    assert restored == original


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
        self.uploaded_count = 0

    def get(self, url, *_args, **_kwargs) -> Response:
        if "statistiky" in url:
            return Response(text=f"Nahráno videí celkem {self.uploaded_count}")
        if "nahrana-videa" in url and self.renamed_to:
            return Response(
                text=(
                    f'<div data-video-id="777"><h3>{self.renamed_to}</h3>'
                    "videoId=777</div>"
                )
            )
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
            self.uploaded_count += 1
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
        upload_requester=target.post,
    )
    assert result.video_id == "777"
    assert result.source_bytes_read == len(source_payload)
    assert source_payload in target.uploaded_body
    assert prepared == [("777", len(source_payload))]
    assert target.renamed_to == "Film (2000) 4K"


def test_relay_upload_renames_when_listing_confirms_before_request_returns() -> None:
    source_payload = b"small-video-payload"
    target = TargetSession()
    upload_read = threading.Event()
    release_response = threading.Event()
    candidate = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "film",
        filename="film.mkv",
        mime_type="video/x-matroska",
        download_url="https://data.sdilej.cz/file",
    )

    original_get = target.get

    def get_with_unrenamed_listing(url, *_args, **_kwargs):
        if "nahrana-videa" in url and upload_read.is_set():
            name = target.renamed_to or "Film (2000) 4K.mkv"
            return Response(text=f'<div data-video-id="777"><h3>{name}</h3></div>')
        return original_get(url, *_args, **_kwargs)

    target.get = get_with_unrenamed_listing

    def delayed_response(*_args, **kwargs):
        encoder = kwargs["data"]
        while encoder.read(7):
            pass
        target.uploaded_count += 1
        upload_read.set()
        release_response.wait(1)
        return Response(status_code=201)

    try:
        result = relay_upload(
            target,
            SourceSession(source_payload),
            candidate,
            "Film (2000) 4K",
            upload_requester=delayed_response,
            monitor_interval_seconds=0.001,
        )
    finally:
        release_response.set()

    assert result.video_id == "777"
    assert target.renamed_to == "Film (2000) 4K"


def test_relay_upload_reports_target_processing_as_pending() -> None:
    source_payload = b"small-video-payload"
    target = TargetSession()
    candidate = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "film",
        filename="film.mkv",
        mime_type="video/x-matroska",
        download_url="https://data.sdilej.cz/file",
    )
    original_get = target.get

    def processing_listing(url, *_args, **_kwargs):
        if "nahrana-videa" in url and target.renamed_to:
            return Response(
                text=(
                    '<div data-video-id="777"><h3>'
                    f"{target.renamed_to}.mkv (Zpracovává se)"
                    "</h3></div>"
                )
            )
        return original_get(url, *_args, **_kwargs)

    target.get = processing_listing
    result = relay_upload(
        target,
        SourceSession(source_payload),
        candidate,
        "Film (2000) 4K",
        upload_requester=target.post,
    )

    assert result.video_id == "777"
    assert not result.completed


def test_relay_upload_rejects_a_stalled_target_request() -> None:
    source_payload = b"small-video-payload"
    target = TargetSession()
    release = threading.Event()
    candidate = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "film",
        filename="film.mkv",
        mime_type="video/x-matroska",
        download_url="https://data.sdilej.cz/file",
    )

    def stalled_request(*_args, **_kwargs):
        release.wait(1)
        return Response(status_code=201)

    try:
        with pytest.raises(PrehrajtoError, match="made no progress"):
            relay_upload(
                target,
                SourceSession(source_payload),
                candidate,
                "Film (2000) 4K",
                upload_requester=stalled_request,
                stall_timeout_seconds=0.01,
                monitor_interval_seconds=0.01,
            )
    finally:
        release.set()


def test_relay_upload_requires_target_statistics_confirmation() -> None:
    source_payload = b"small-video-payload"
    target = TargetSession()
    candidate = Candidate(
        "1",
        "https://sdilej.cz/1/film.mkv",
        "film",
        filename="film.mkv",
        mime_type="video/x-matroska",
        download_url="https://data.sdilej.cz/file",
    )

    def upload_without_statistics(*args, **kwargs):
        response = target.post(*args, **kwargs)
        target.uploaded_count = 0
        return response

    with pytest.raises(PrehrajtoError, match="statistics did not confirm"):
        relay_upload(
            target,
            SourceSession(source_payload),
            candidate,
            "Film (2000) 4K",
            upload_requester=upload_without_statistics,
            confirmation_timeout_seconds=0.01,
            confirmation_interval_seconds=0.001,
        )


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


def test_target_processing_keeps_prepared_state_and_releases_claim(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    assert state.claim_upload(1, "shard-0")
    state.record_target_processing(
        1,
        video_id="777",
        size=123,
        source_id="source-1",
    )

    row = state.snapshot(1)
    assert row["prepared"]["target_video_id"] == "777"
    assert row["attempts"][-1]["status"] == "target_processing"
    assert "claim" not in row
    assert not state.uploaded(1)


def test_upload_claim_blocks_other_worker_until_lease_expires(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    claimed_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert state.claim_upload(1, "shard-0", at=claimed_at)
    assert not state.claim_upload(1, "shard-0", at=claimed_at + timedelta(hours=1))
    assert not state.claim_upload(1, "shard-1", at=claimed_at + timedelta(hours=5))
    assert state.claim_upload(1, "shard-1", at=claimed_at + timedelta(hours=7))
    assert state.snapshot(1)["claim"]["worker_id"] == "shard-1"


def test_active_claim_reports_only_unexpired_lease(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    claimed_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert state.claim_upload(1, "shard-0", at=claimed_at)

    assert state.actively_claimed(1, at=claimed_at + timedelta(hours=5))
    assert not state.actively_claimed(1, at=claimed_at + timedelta(hours=7))


def test_new_actions_run_releases_only_orphaned_claims(tmp_path, monkeypatch) -> None:
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setenv("GITHUB_RUN_ID", "old-run")
    assert state.claim_upload(1, "old-worker")
    monkeypatch.setenv("GITHUB_RUN_ID", "current-run")
    assert state.claim_upload(2, "current-worker")

    released = state.release_claims_from_other_run("current-run")

    assert released == 1
    assert "claim" not in state.snapshot(1)
    assert state.snapshot(2)["claim"]["run_id"] == "current-run"


def test_upload_failure_releases_claim_and_prepared_checkpoint(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    assert state.claim_upload(1, "shard-0")
    state.record_prepared(1, "777", 123)
    state.record_upload_failure(1, {"status": "upload_failed"})
    row = state.snapshot(1)
    assert "claim" not in row
    assert "prepared" not in row
    assert row["attempts"][-1]["status"] == "upload_failed"


def test_completed_no_source_attempt_is_deferred_for_thirty_days(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(
        1,
        {
            "status": "no_acceptable_source",
            "permanent": False,
            "discovery_complete": True,
        },
    )
    attempted = datetime.fromisoformat(state.film(1)["attempts"][-1]["attempted_at"])
    assert state.deferred(1, at=attempted + timedelta(days=29))
    assert not state.deferred(1, at=attempted + timedelta(days=31))


def test_ambiguous_legacy_no_source_attempt_is_retried(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(1, {"status": "no_acceptable_source", "permanent": False})

    assert not state.deferred(1)


def test_policy_specific_defer_ignores_attempt_from_old_policy(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(
        1,
        {
            "status": "no_acceptable_source",
            "permanent": False,
            "discovery_complete": True,
            "selection_policy": "old-policy",
        },
    )

    assert state.deferred(1, selection_policy="old-policy")
    assert not state.deferred(1, selection_policy="new-policy")


def test_failure_cooldown_does_not_block_replacement_source(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_upload_failure(
        1,
        {
            "status": "source_refresh_failed",
            "source_id": "oversized-old",
            "permanent": False,
        },
    )

    assert state.deferred(1, source_id="oversized-old")
    assert not state.deferred(1, source_id="compact-new")


def test_transient_transfer_failure_is_retried_after_thirty_minutes(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_upload_failure(
        1,
        {
            "status": "source_refresh_failed",
            "source_id": "verified-source",
            "permanent": False,
        },
    )
    attempted = datetime.fromisoformat(state.film(1)["attempts"][-1]["attempted_at"])

    assert state.deferred(
        1,
        source_id="verified-source",
        at=attempted + timedelta(minutes=29),
    )
    assert not state.deferred(
        1,
        source_id="verified-source",
        at=attempted + timedelta(minutes=31),
    )


def test_transient_source_discovery_failure_is_retried_after_thirty_minutes(
    tmp_path,
) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(
        1,
        {
            "status": "source_discovery_failed",
            "selection_policy": "current-policy",
            "permanent": False,
        },
    )
    attempted = datetime.fromisoformat(state.film(1)["attempts"][-1]["attempted_at"])

    assert state.deferred(
        1,
        selection_policy="current-policy",
        at=attempted + timedelta(minutes=29),
    )
    assert not state.deferred(
        1,
        selection_policy="current-policy",
        at=attempted + timedelta(minutes=31),
    )


def test_deep_scan_queue_is_policy_specific_and_retries_failures(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(
        1,
        {
            "status": "source_deep_scan_needed",
            "selection_policy": "current-policy",
            "permanent": True,
        },
    )

    assert state.deferred(1, selection_policy="current-policy")
    assert not state.deferred(1, selection_policy="new-policy")
    assert state.deep_scan_ready(1, selection_policy="current-policy")
    assert not state.deep_scan_ready(1, selection_policy="new-policy")

    state.record_attempt(
        1,
        {
            "status": "source_deep_scan_failed",
            "selection_policy": "current-policy",
            "permanent": False,
        },
    )
    attempted = datetime.fromisoformat(state.film(1)["attempts"][-1]["attempted_at"])
    assert not state.deep_scan_ready(
        1,
        selection_policy="current-policy",
        at=attempted + timedelta(minutes=29),
    )
    assert state.deep_scan_ready(
        1,
        selection_policy="current-policy",
        at=attempted + timedelta(minutes=31),
    )


def test_target_review_blocks_replacement_source_and_new_policy(tmp_path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.record_attempt(
        1,
        {
            "status": "target_requires_review",
            "source_id": "wrong-film",
            "selection_policy": "old-policy",
            "permanent": True,
        },
    )

    assert state.deferred(
        1,
        source_id="replacement-film",
        selection_policy="new-policy",
    )
