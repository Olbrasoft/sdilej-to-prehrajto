import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from sdilej_to_prehrajto.prehrajto import remember_detail_urls
from sdilej_to_prehrajto.subtitle_target import (
    SubtitleTarget, TargetUnavailable, parse_detail, trusted_detail,
)
from sdilej_to_prehrajto.subtitle_media import normalize_srt


URL = "https://prehraj.to/film/1234567890abcdef"
TRACK_URL = "https://pf-storage2.premiumcdn.net/123/example.vtt?token=PRIVATE"
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nAhoj.\n"
DIGEST = hashlib.sha256(normalize_srt(SRT)).hexdigest()


def detail(video_id="42", tracks="", ready=True):
    videos = "videos.push({src: 'https://cdn.example/video.mp4'});" if ready else ""
    return f"<script>var config = {{'videoId': {video_id}}}; {videos} var tracks = [{tracks}];</script>"


def track(language="cz", track_id="7"):
    return f'{{src: "{TRACK_URL}", srclang: "{language}", label: "CZ - {track_id} - cs-42…", kind: "captions"}}'


def test_detail_checks_exact_id_and_explicit_track_language():
    target = parse_detail(detail(tracks=track()), "42", "Film", URL)
    assert target.detail_verified and target.ready and target.has_czech
    assert target.tracks[0]["id"] == "7"
    assert not parse_detail(detail(tracks=track("en")), "42", "Film", URL).has_czech
    assert not parse_detail(detail(ready=False), "42", "Film", URL).ready
    with pytest.raises(TargetUnavailable, match="identity_unverified"):
        parse_detail(detail("43"), "42", "Film", URL)


@pytest.mark.parametrize("html", [
    "<h1>Please log in</h1>",
    "<script>var config = {'videoId': 42};</script>",
    detail(tracks='{src: "https://evil.example/file.vtt", srclang: "cz", label: "CZ - 7 - file"}'),
    detail(tracks=track().replace('srclang: "cz",', '')),
    detail(tracks=track() + ',' + track()),
    detail() + '<script>var tracks = [{file:"https://cdn.example/x", label:"CZ - 7 - file"}];</script>',
])
def test_ambiguous_detail_fails_closed(html):
    with pytest.raises(TargetUnavailable):
        parse_detail(html, "42", "Film", URL)


@pytest.mark.parametrize("url", [
    "https://evil.example/film/1234567890abcdef",
    URL + "?do=delete", URL + "#fragment",
    "https://user:password@prehraj.to/film/1234567890abcdef",
])
def test_untrusted_details_are_never_requested(url):
    assert not trusted_detail(url)
    session = Mock()
    with pytest.raises(TargetUnavailable):
        SubtitleTarget(session, 0).refresh("42", "Film", url)
    session.get.assert_not_called()


def test_direct_refresh_never_searches_listing():
    session = Mock()
    session.get.return_value = Mock(status_code=200, text=detail())
    target = SubtitleTarget(session, 0).refresh("42", "Film", URL)
    assert target.ready and not target.tracks
    session.get.assert_called_once_with(URL, timeout=45, allow_redirects=False)


def test_content_verification_follows_only_trusted_cdn_redirects():
    session = Mock()
    first = Mock(status_code=302, headers={"Location": "https://ovh-s9-storage2.premiumcdn.net/a.vtt"})
    last = Mock(status_code=200)
    last.iter_content.return_value = [b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAhoj.\n"]
    session.get.side_effect = [first, last]
    assert SubtitleTarget(session, 0).verify_content({"url": TRACK_URL}, DIGEST)
    first.close.assert_called_once()
    last.close.assert_called_once()


def test_content_verification_rejects_foreign_redirect_and_wrong_content():
    session = Mock()
    session.get.return_value = Mock(status_code=302, headers={"Location": "http://localhost/secret.vtt"})
    with pytest.raises(TargetUnavailable, match="url_untrusted"):
        SubtitleTarget(session, 0).verify_content({"url": TRACK_URL}, DIGEST)
    session.get.assert_called_once()
    last = Mock(status_code=200)
    last.iter_content.return_value = [SRT.replace(b"Ahoj.", b"Other.")]
    session.get.side_effect = [last]
    assert not SubtitleTarget(session, 0).verify_content({"url": TRACK_URL}, DIGEST)


def test_uploader_caches_links_bound_to_their_own_ids():
    session = requests.Session()
    remember_detail_urls(session, f'<div id="snippet-uploadedVideoListing-video-42"><a href="{URL}">Detail</a></div>'
                         '<div id="snippet-uploadedVideoListing-video-43"><a href="https://evil.example/film/1234567890abcdef">Detail</a></div>')
    assert session._prehrajto_detail_urls == {"42": URL}


def test_upload_completion_persists_detail_in_ledger_and_followup():
    from sdilej_to_prehrajto.pipeline import SyncPipeline
    pipeline = object.__new__(SyncPipeline)
    pipeline._completion_lock = threading.Lock()
    pipeline.state = Mock()
    pipeline.subtitle_queue = Mock()
    session = SimpleNamespace(_prehrajto_detail_urls={"42": URL})
    film = SimpleNamespace(cr_film_id=1)
    source = SimpleNamespace(source_id="123", url="https://sdilej.cz/123/film.mkv", filename="film.mkv")
    upload = {"target_video_id": "42", "completion_evidence": "relay_completed"}
    pipeline._finish_success(film, source, {"needs_czech_subtitles": True, "display_name": "Film"}, upload, session)
    assert pipeline.subtitle_queue.enqueue.call_args.args[0]["detail_url"] == URL
    assert pipeline.state.record_success.call_args.args[1]["detail_url"] == URL


def test_unknown_track_language_is_not_assumed_absent():
    assert parse_detail(detail(tracks=track("unk")), "42", "Film", URL).unknown_tracks
