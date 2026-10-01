import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest
import requests

from sdilej_to_prehrajto import subtitle_backfill as worker_module
from sdilej_to_prehrajto.subtitle_backfill import Backfill, report_markdown, source_map
from sdilej_to_prehrajto.subtitle_media import (
    SubtitleUnavailable, choose_stream, convert_text_subtitle, normalize_srt, run_media,
)
from sdilej_to_prehrajto.subtitle_target import (
    Target, TargetUnavailable, SubtitleTarget, parse_listing, trusted_action,
)

SRT = "1\n00:00:01,000 --> 00:00:02,500\nPříliš žluťoučký kůň.\n".encode()


def listing(title="Film CZ Titulky", tracks="", form_id="42", count=0):
    return f'''<a href="?uploadedVideoListing-visualPaginator-page=160">160</a>
    <div id="snippet-uploadedVideoListing-video-42">
      <h3 id="snippet-uploadedVideoListing-videoName-42">{title}</h3>
      <form action="/profil/nahrana-videa?do=uploadedVideoListing-uploadSubtitles">
        <input name="video" value="{form_id}"><input name="files[]" type="file">
      </form>
      <span id="snippet-uploadedVideoListing-subtitlescount-42">({count})</span>
      <div id="snippet-uploadedVideoListing-subtitles-42">{tracks}</div>
    </div>'''


def track(name="cze", subtitle_id="7", selected=""):
    return f'''<div class="grid-x"><p class="text-medium">{name}</p>
    <a href="?uploadedVideoListing-videoId=42&amp;uploadedVideoListing-subtitleId={subtitle_id}&amp;do=uploadedVideoListing-removeSubtitle">Odebrat</a>
    <select class="select-language"><option {selected} data-link="/profil/nahrana-videa?uploadedVideoListing-videoId=42&amp;uploadedVideoListing-subtitleId={subtitle_id}&amp;do=uploadedVideoListing-changeSubtitleLanguage&amp;uploadedVideoListing-language=cz">CZ</option><option>US</option></select></div>'''.replace('href="?', 'href="/profil/nahrana-videa?')


def test_ready_target_requires_matching_form_and_visible_completion():
    targets, last = parse_listing(listing())
    assert targets[0].ready and not targets[0].tracks and last == 160
    target = parse_listing(listing(title="Film CZ Titulky (Zpracovává se)"))[0][0]
    assert not target.ready and target.title == "Film CZ Titulky"
    assert not parse_listing(listing(form_id="43"))[0][0].ready


@pytest.mark.parametrize("name,czech,unknown", [("cze", True, False), ("eng", False, False), ("unk1", False, True)])
def test_default_first_cz_option_is_not_language_evidence(name, czech, unknown):
    target = parse_listing(listing(tracks=track(name), count=1))[0][0]
    assert target.has_czech == czech
    assert target.unknown_tracks == unknown


def test_explicit_selected_language_and_unparsed_tracks():
    assert parse_listing(listing(tracks=track("unk1", selected="selected"), count=1))[0][0].has_czech
    assert parse_listing(listing(count=1))[0][0].unknown_tracks
    assert parse_listing(listing(tracks="Unknown subtitle layout"))[0][0].unknown_tracks


def test_unrecognized_listing_fails_closed():
    with pytest.raises(TargetUnavailable):
        parse_listing("<h1>Please log in</h1>")
    assert not trusted_action("https://other.example/profil/nahrana-videa?do=uploadedVideoListing-uploadSubtitles", "uploadSubtitles")


def test_listing_uses_the_same_origin_as_authenticated_login():
    from sdilej_to_prehrajto.prehrajto import BASE_URL
    session = Mock()
    session.get.return_value.text = listing()
    target = SubtitleTarget(session, interval=0)
    targets, _ = target.listing()
    assert session.get.call_args.args[0] == BASE_URL + "/profil/nahrana-videa"
    assert targets[0].action.startswith(BASE_URL + "/")


def test_vtt_conversion_preserves_czech_text_and_timing():
    result = normalize_srt("\ufeffWEBVTT\n\ncue-id\n00:01.000 --> 00:02.500 align:start\nPříliš žluťoučký kůň.\n".encode())
    assert result == normalize_srt(SRT)
    assert b"\n" not in result.replace(b"\r\n", b"")


@pytest.mark.parametrize("payload", [b"<html>login</html>", b"WEBVTT", b"1\n00:02.000 --> 00:01.000\nx", b"1\n00:62.000 --> 01:03.000\nx", b"1\n00:01.000 --> 00:02.000\n", b"\xff"])
def test_rejects_invalid_subtitles(payload):
    with pytest.raises(SubtitleUnavailable):
        normalize_srt(payload)


def test_stream_selection_never_falls_back_to_foreign_or_bitmap():
    english = {"index": 2, "codec_name": "subrip", "tags": {"language": "eng"}}
    bitmap = {"index": 3, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "cze"}}
    forced = {"index": 4, "codec_name": "subrip", "tags": {"language": "cze"}, "disposition": {"forced": 1}}
    czech = {"index": 5, "codec_name": "ass", "tags": {"language": "ces"}}
    assert choose_stream([english, bitmap, forced, czech]) is czech
    for streams in ([], [english], [bitmap], [forced]):
        with pytest.raises(SubtitleUnavailable):
            choose_stream(streams)


def test_media_timeout_never_exposes_authenticated_url(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(["ffprobe", "https://secret.example/token"], 1)))
    with pytest.raises(SubtitleUnavailable, match="^source_media_timeout$"):
        run_media(["ffprobe"], 1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_ass_to_srt_conversion():
    ass = '''[Script Info]
ScriptType: v4.00+
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Příliš žluťoučký kůň.
'''.encode()
    assert convert_text_subtitle(ass) == normalize_srt(SRT)


def prepare_root(tmp_path, selected_after=False):
    (tmp_path / "state").mkdir()
    (tmp_path / "plans").mkdir()
    (tmp_path / "manifests").mkdir()
    (tmp_path / "state/sync.json").write_text(json.dumps({"films": {"1": {"upload": {"target_video_id": "42", "uploaded_at": "2026-09-01T00:00:00+00:00"}}}}))
    source = {"cr_film_id": 1, "source_id": "123", "source_url": "https://sdilej.cz/123/original.mkv", "updated_at": "2026-09-02T00:00:00+00:00" if selected_after else "2026-08-31T00:00:00+00:00"}
    (tmp_path / "manifests/selected-sources.jsonl").write_text(json.dumps(source) + "\n")
    return tmp_path


def test_source_mapping_rejects_later_reselection_and_unrelated_queue_target(tmp_path):
    prepare_root(tmp_path, selected_after=True)
    assert source_map(tmp_path) == {}
    (tmp_path / "plans/subtitle-followup.jsonl").write_text(json.dumps({"cr_film_id": 1, "target_video_id": "43", "source_id": "123", "source_url": "https://sdilej.cz/123/original.mkv"}))
    assert source_map(tmp_path) == {}


def test_source_mapping_accepts_exact_historical_source(tmp_path):
    prepare_root(tmp_path)
    assert source_map(tmp_path)["42"]["source_id"] == "123"


def test_name_reconciliation_is_not_source_provenance(tmp_path):
    prepare_root(tmp_path)
    (tmp_path / "plans/subtitle-followup.jsonl").write_text(json.dumps({"cr_film_id": 1, "target_video_id": "42", "source_id": "123", "source_url": "https://sdilej.cz/123/original.mkv", "source_binding_verified": False}))
    assert source_map(tmp_path) == {}


def make_worker(tmp_path, monkeypatch):
    prepare_root(tmp_path)
    target = Mock()
    target.refresh.return_value = Target("42", "Film CZ Titulky", True, action="https://prehrajto.cz/profil/nahrana-videa?do=uploadedVideoListing-uploadSubtitles")
    worker = Backfill(target, Mock(), tmp_path, tmp_path / "backfill.json", tmp_path / "report.md")
    row = worker.inspect(target.refresh.return_value)
    monkeypatch.setattr(worker_module, "extract_original", Mock(return_value=(normalize_srt(SRT), {"source_id": "123", "method": "original_embedded_track"})))
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: None)
    return worker, row, target


def test_timeout_after_post_is_durable_and_never_posts_twice(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    target.upload.side_effect = requests.Timeout("sensitive address")
    with pytest.raises(requests.Timeout):
        worker.process(row)
    durable = json.loads(worker.state_path.read_text())
    assert durable["videos"]["42"]["intent"]["sha256"]
    restarted = Backfill(target, Mock(), tmp_path, worker.state_path, worker.report_path)
    assert not restarted.process(restarted.state["videos"]["42"])
    assert target.upload.call_count == 1


def test_failed_durable_checkpoint_prevents_post(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.persist = Mock(side_effect=RuntimeError("git unavailable"))
    with pytest.raises(RuntimeError):
        worker.process(row)
    target.upload.assert_not_called()


def test_fresh_existing_subtitle_before_post_stops_upload(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    target.refresh.side_effect = [target.refresh.return_value, Target("42", "Film", True, tracks=[{"id": "7", "czech": True}])]
    assert not worker.process(row)
    assert row["status"] == "already_has_czech"
    target.upload.assert_not_called()


def test_reconciliation_requires_matching_new_track_not_any_track(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    row["intent"] = {"filename": "cs-42-hash.srt", "before_ids": ["7"]}
    target.refresh.return_value.tracks = [{"id": "8", "name": "unrelated", "czech": True}]
    assert not worker.reconcile(row)
    assert row["status"] == "upload_unconfirmed"
    target.set_own_czech_language.assert_not_called()
    target.refresh.return_value.tracks = [{"id": "9", "name": "cs-42-hash", "czech": True, "processing": False}]
    assert worker.reconcile(row)
    assert row["status"] == "attached_verified"
    target.upload.assert_not_called()
    target.set_own_czech_language.assert_called_once()


def test_dry_run_never_changes_subtitle_language(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.dry_run = True
    row["intent"] = {"filename": "cs-42-hash.srt", "before_ids": []}
    target.refresh.return_value.tracks = [{"id": "9", "name": "cs-42-hash", "czech": True, "processing": False}]
    assert not worker.reconcile(row)
    target.set_own_czech_language.assert_not_called()
    target.upload.assert_not_called()


def test_attachment_limit_counts_unconfirmed_posts(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.state["videos"]["43"] = {"target_video_id": "43", "status": "pending"}
    monkeypatch.setattr(worker, "inventory", lambda _: None)
    attempts = []
    def process(record):
        attempts.append(record["target_video_id"])
        record["intent"] = {"filename": "cs-test.srt"}
        raise requests.Timeout()
    monkeypatch.setattr(worker, "process", process)
    worker.run(max_pages=1, max_sources=12, limit=1)
    assert attempts == ["42"]


def test_inventory_cursor_advances_and_revisits_processing(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    pending = Target("42", "Film CZ Titulky", False)
    ready = Target("42", "Film CZ Titulky", True)
    target.listing.side_effect = [([pending], 2), ([pending], 2)]
    worker.inventory(1)
    assert worker.state["next_page"] == 1 and row["status"] == "target_processing"
    target.listing.side_effect = [([ready], 2), ([ready], 2)]
    worker.inventory(1)
    assert worker.state["next_page"] == 0 and row["status"] == "pending"


def test_report_has_actionable_missing_list_and_separate_temporary_errors():
    state = {"videos": {"42": {"target_video_id": "42", "title": "Film", "status": "source_czech_text_missing", "source": {"source_id": "123", "source_url": "https://sdilej.cz/123/film.mkv"}}}}
    report = report_markdown(state)
    assert "K ručnímu doplnění" in report
    assert "Původní soubor nemá samostatnou českou titulkovou stopu" in report
    assert "https://sdilej.cz/123/film.mkv" in report
    assert "Dočasné chyby" in report
