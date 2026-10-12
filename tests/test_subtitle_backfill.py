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
    extraction_timeout, extract_original,
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


def test_listing_recovers_from_temporary_http_error(monkeypatch, capsys):
    monkeypatch.setattr("sdilej_to_prehrajto.http_recovery.time.sleep", Mock())
    session = Mock()
    session.get.side_effect = [Mock(status_code=503, headers={}), Mock(status_code=200, text=listing())]
    targets, _ = SubtitleTarget(session, interval=0).listing()
    assert targets[0].video_id == "42"
    assert session.get.call_count == 2
    assert "phase=target_listing status=503" in capsys.readouterr().out


@pytest.mark.parametrize("timeout", [False, True])
def test_subtitle_post_is_never_retried(timeout, monkeypatch, capsys):
    monkeypatch.setattr("sdilej_to_prehrajto.http_recovery.time.sleep", Mock())
    session = Mock()
    if timeout:
        session.post.side_effect = requests.Timeout("PRIVATE_TOKEN")
    else:
        session.post.return_value = Mock(status_code=503, headers={})
    target = parse_listing(listing())[0][0]
    with pytest.raises((requests.Timeout, TargetUnavailable)):
        SubtitleTarget(session, interval=0).upload(target, "cs-test.srt", SRT)
    session.post.assert_called_once()
    output = capsys.readouterr().out
    assert "phase=target_subtitle_upload" in output
    assert "PRIVATE" not in output


def test_mutating_language_get_is_not_retried(monkeypatch, capsys):
    monkeypatch.setattr("sdilej_to_prehrajto.http_recovery.time.sleep", Mock())
    session = Mock()
    session.get.return_value = Mock(status_code=503, headers={})
    target = parse_listing(listing(tracks=track(), count=1))[0][0]
    with pytest.raises(TargetUnavailable, match="subtitle_language_unconfirmed"):
        SubtitleTarget(session, interval=0).set_own_czech_language(target.tracks[0], "42")
    session.get.assert_called_once()
    assert "phase=target_subtitle_language status=503" in capsys.readouterr().out


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


def test_inventory_recovers_links_without_search_or_overwriting_processing(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    row["status"] = "target_processing"
    pending = Target("42", "Film CZ Titulky", False, detail_url="https://prehraj.to/film/1234567890abcdef")
    target.listing.return_value = ([pending], 2)
    worker.inventory(2)
    target.listing.assert_called_once_with(1)
    assert worker.state["next_page"] == 0 and row["status"] == "target_processing"
    assert row["detail_url"] == pending.detail_url
    assert worker_module.ready_for_source_check(row, worker_module.now().isoformat())
    target.listing.reset_mock()
    worker.inventory(2)
    target.listing.assert_not_called()


def test_report_has_actionable_missing_list_and_separate_temporary_errors():
    state = {"videos": {"42": {"target_video_id": "42", "title": "Film", "status": "source_czech_text_missing", "source": {"source_id": "123", "source_url": "https://sdilej.cz/123/film.mkv"}}}}
    report = report_markdown(state)
    assert "K ručnímu doplnění" in report
    assert "Původní soubor nemá samostatnou českou titulkovou stopu" in report
    assert "https://sdilej.cz/123/film.mkv" in report
    assert "Dočasné chyby" in report


def test_batch_checks_120_sources_serially_and_continues(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.state["videos"] = {str(i): {"target_video_id": str(i), "status": "pending"} for i in range(1, 123)}
    monkeypatch.setattr(worker, "inventory", lambda _: None)
    sleeps = []
    monkeypatch.setattr(worker_module.time, "sleep", sleeps.append)
    calls = []

    def process(record):
        calls.append(record["target_video_id"])
        raise SubtitleUnavailable("source_czech_text_missing")

    monkeypatch.setattr(worker, "process", process)
    result = worker.run(20, 120, 30)
    assert calls == [str(i) for i in range(1, 121)]
    assert sleeps == [2] * 119
    assert result["inspected_sources"] == 120
    assert result["remaining_due"] == 2
    assert result["stop_reason"] == "source_limit"
    assert result["continue_backfill"]
    assert json.loads(worker.state_path.read_text())["last_batch"] == result
    assert "Kontrol: 120" in worker.report_path.read_text()


@pytest.mark.parametrize("pages,dry_run,continuation", [(0, False, False), (3, False, False), (3, True, False)])
def test_continuation_excludes_deferred_and_inventory_only_rows(tmp_path, monkeypatch, pages, dry_run, continuation):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.dry_run = dry_run
    statuses = list(worker_module.INVENTORY_ONLY_STATUSES) + ["source_czech_text_missing", "source_media_timeout", "upload_unconfirmed"]
    worker.state["videos"] = {str(i): {"target_video_id": str(i), "status": status,
                                      "retry_after": "2999-01-01T00:00:00+00:00" if status not in worker_module.INVENTORY_ONLY_STATUSES else ""}
                              for i, status in enumerate(statuses)}
    worker.state["next_page"] = pages
    monkeypatch.setattr(worker, "inventory", lambda _: None)
    process = Mock()
    monkeypatch.setattr(worker, "process", process)
    result = worker.run(20, 120, 30)
    process.assert_not_called()
    assert result["remaining_due"] == 0
    assert result["continue_backfill"] == continuation


def test_batch_deadline_preserves_pending_source_and_schedules_successor(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "inventory", lambda _: None)
    monkeypatch.setattr(worker, "process", Mock(side_effect=SubtitleUnavailable("batch_deadline_reached")))
    result = worker.run(20, 120, 30)
    assert row["status"] == "pending"
    assert result["stop_reason"] == "time_budget"
    assert result["remaining_due"] == 1
    assert result["continue_backfill"]
    target.upload.assert_not_called()


def test_inventory_time_is_part_of_batch_budget(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    clock = [0]
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "inventory", lambda _: clock.__setitem__(0, 2100))
    process = Mock()
    monkeypatch.setattr(worker, "process", process)
    result = worker.run(20, 120, 30)
    process.assert_not_called()
    assert result["stop_reason"] == "time_budget"


def test_extraction_uses_remaining_budget_without_misclassifying_source(monkeypatch):
    from sdilej_to_prehrajto import subtitle_media as media
    clock = [10]
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    assert extraction_timeout(900, 20) == 10
    assert extraction_timeout(900, None) == 900
    with pytest.raises(SubtitleUnavailable, match="batch_deadline_reached"):
        extraction_timeout(900, 10)

    def timeout(*args, **kwargs):
        clock[0] = 20
        raise SubtitleUnavailable("source_media_timeout")

    monkeypatch.setattr(media, "_extract_original", timeout)
    with pytest.raises(SubtitleUnavailable, match="batch_deadline_reached"):
        extract_original(Mock(), {}, deadline=20)


def test_30_attachment_limit_includes_ambiguous_posts(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.state["videos"] = {str(i): {"target_video_id": str(i), "status": "pending"} for i in range(1, 33)}
    monkeypatch.setattr(worker, "inventory", lambda _: None)

    def process(record):
        record["intent"] = {"filename": "cs-unique.srt"}
        raise requests.Timeout()

    monkeypatch.setattr(worker, "process", process)
    result = worker.run(20, 120, 30)
    assert result["inspected_sources"] == result["attachments_submitted"] == 30
    assert result["attached_verified"] == 0
    assert result["remaining_due"] == 2
    assert result["stop_reason"] == "attachment_limit"


def test_local_queue_seeds_details_without_remote_inventory(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    url = "https://prehraj.to/film/1234567890abcdef"
    (tmp_path / "plans/subtitle-followup.jsonl").write_text(json.dumps({"cr_film_id": 1, "target_video_id": "42", "display_name": "Film CZ Titulky", "detail_url": url}))
    worker.inventory(2)
    target.listing.assert_not_called()
    assert row["detail_url"] == url
    assert row["source"]["source_id"] == "123"
    assert worker.state["inventory_mode"] == "local-detail-v1"


def test_missing_link_index_advances_and_failure_does_not_abort_queue(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    worker.state["detail_index_next_page"] = 5
    target.listing.return_value = ([], 100)
    worker.inventory(2)
    assert [call.args for call in target.listing.call_args_list] == [(1,), (5,)]
    assert worker.state["detail_index_next_page"] == 6
    target.listing.side_effect = requests.HTTPError("secret")
    worker.inventory(2)
    assert row["status"] == "pending" and row["source"]


def test_direct_reconciliation_requires_own_new_id_content_and_czech(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    row["intent"] = {"filename": "cs-42-hash.srt", "before_ids": ["7"], "sha256": "expected"}
    target.refresh.return_value.detail_verified = True
    target.refresh.return_value.tracks = [{"id": "8", "czech": True, "url": "PRIVATE"}]
    target.verify_content.return_value = False
    assert not worker.reconcile(row)
    target.verify_content.return_value = True
    target.refresh.return_value.tracks[0]["czech"] = False
    assert not worker.reconcile(row)
    target.refresh.return_value.tracks[0]["czech"] = True
    assert worker.reconcile(row)
    worker.save()
    assert row["intent"]["subtitle_id"] == "8"
    assert "PRIVATE" not in worker.state_path.read_text()
    target.upload.assert_not_called()
    target.set_own_czech_language.assert_not_called()


def test_direct_reconciliation_rejects_preexisting_track(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    row["intent"] = {"filename": "cs-42-hash.srt", "before_ids": ["7"], "sha256": "expected"}
    target.refresh.return_value.detail_verified = True
    target.refresh.return_value.tracks = [{"id": "7", "czech": True}]
    assert not worker.reconcile(row)
    target.verify_content.assert_not_called()


def test_seed_preserves_existing_czech_evidence_without_source(tmp_path, monkeypatch):
    worker, row, target = make_worker(tmp_path, monkeypatch)
    row.update(status="already_has_czech", checked_at="2026-10-01T00:00:00+00:00")
    row.pop("source")
    worker.sources = {}
    worker.inventory(0)
    assert row["status"] == "already_has_czech"
    assert row["checked_at"] == "2026-10-01T00:00:00+00:00"
