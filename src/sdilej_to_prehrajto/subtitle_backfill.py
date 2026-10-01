"""Bounded, resumable, originals-only subtitle backfill (one worker)."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote, urlsplit

import requests

from .git_state import GitStatePersister
from .prehrajto import login as target_login
from .sdilej import login as source_login, SdilejError
from .subtitle_media import extract_original, SubtitleUnavailable
from .subtitle_target import SubtitleTarget, TargetUnavailable


def now():
    return datetime.now(timezone.utc)


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def jsonl(path: Path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def source_map(root: Path) -> dict[str, dict]:
    """Never infer a source by title or use a source reselected after upload."""
    manifests = {str(row["cr_film_id"]): row for row in jsonl(root / "manifests/selected-sources.jsonl")}
    queue = {str(row["target_video_id"]): row for row in jsonl(root / "plans/subtitle-followup.jsonl")}
    films = json.loads((root / "state/sync.json").read_text())["films"]
    mapped = {}
    for film_id, row in films.items():
        upload = row.get("upload") or {}
        target_id = str(upload.get("target_video_id", ""))
        if not target_id:
            continue
        if upload.get("completion_evidence") == "reconciled_existing_film":
            continue
        if queue.get(target_id, {}).get("source_binding_verified") is False:
            continue
        source = upload
        evidence = "upload_snapshot"
        if not source.get("source_url"):
            queued = queue.get(target_id, {})
            source = queued if str(queued.get("cr_film_id")) == film_id else {}
            evidence = "subtitle_queue_snapshot"
        if not source.get("source_url"):
            source = manifests.get(film_id, {})
            evidence = "pre_upload_selected_source"
            try:
                selected_at = datetime.fromisoformat(source["updated_at"])
                uploaded_at = datetime.fromisoformat(upload["uploaded_at"])
                if selected_at > uploaded_at:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
        url = source.get("source_url", "")
        source_id = str(source.get("source_id", ""))
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "sdilej.cz" or parsed.query or not parsed.path.startswith(f"/{source_id}/") or not source_id.isdigit():
            continue
        mapped[target_id] = {"cr_film_id": int(film_id), "source_id": source_id,
                             "source_url": url, "source_filename": source.get("source_filename", ""),
                             "provenance": evidence}
    return mapped


MANUAL_REASONS = {
    "source_czech_text_missing": "Původní soubor nemá samostatnou českou titulkovou stopu.",
    "source_czech_full_text_unsupported": "Česká stopa je obrazová, pouze vynucená nebo v nepodporovaném formátu.",
    "source_provenance_missing": "Chybí spolehlivé spojení tohoto uploadu s původním zdrojem.",
    "source_provenance_invalid": "Nelze bezpečně ověřit identitu původního zdroje.",
    "existing_tracks_uncertain": "Titulky už existují, ale jejich jazyk není spolehlivě určen; bez automatického zásahu.",
    "subtitle_encoding_unsupported": "Kódování zdrojových titulků není UTF-8; je nutné ověřit převod.",
    "subtitle_invalid": "Zdrojové titulky mají neplatný nebo nepodporovaný textový formát.",
    "subtitle_invalid_timing": "Zdrojové titulky mají neplatné časování.",
    "subtitle_empty": "Zdrojová titulková stopa je prázdná.",
    "subtitle_empty_cue": "Zdrojové titulky obsahují prázdný úsek.",
    "subtitle_too_large": "Titulky přesahují bezpečný limit velikosti.",
}


def report_markdown(state: dict) -> str:
    rows = list(state.get("videos", {}).values())
    counts = Counter(row.get("status", "pending") for row in rows)
    lines = ["# Doplňování českých titulků", "",
             f"Poslední aktualizace (UTC): {state.get('updated_at', '')}", "",
             f"Zkontrolováno videí: {len(rows)}. Další stránka kontroly: {state.get('next_page', 0)}.", "",
             "Pouze titulky z původního zdroje. Bez překladu, generování, OCR, jiných vydání filmu a mazání existujících titulků.", "",
             "Stavy: " + ", ".join(f"`{key}`: {value}" for key, value in sorted(counts.items())), ""]

    def table(selected):
        result = ["| Film | Původní zdroj | Důvod | Poslední ověření (UTC) |", "| --- | --- | --- | --- |"]
        for row in sorted(selected, key=lambda value: value.get("title", "")):
            title = row.get("title", row["target_video_id"]).replace("|", "\\|").replace("\n", " ").replace("[", "(").replace("]", ")")
            target = row.get("detail_url") or "https://prehrajto.cz/profil/nahrana-videa?searchPhrase=" + quote(row.get("title", ""))
            source = row.get("source", {})
            source_link = f"[Sdílej {source['source_id']}]({source['source_url']})" if source.get("source_url") else "Nedoložen"
            reason = MANUAL_REASONS.get(row.get("status"), row.get("status", "pending"))
            result.append(f"| [{title}]({target}) (ID {row['target_video_id']}) | {source_link} | {reason} | {row.get('checked_at', '')} |")
        return result

    manual = [row for row in rows if row.get("status") in MANUAL_REASONS]
    retry = [row for row in rows if row.get("status") in {"source_media_failed", "source_media_timeout", "source_unavailable", "source_detail_changed", "target_unavailable", "upload_unconfirmed", "attached_processing"}]
    lines += ["## K ručnímu doplnění nebo ověření", "", *table(manual), "",
              "## Dočasné chyby a čekání na potvrzení", "", *table(retry), "",
              "Zpracovávaná videa se kontrolují znovu. Chybějící zdrojové stopy se znovu prověřují nejdříve za sedm dní.", "",
              "Nejasný výsledek vložení se pouze ověřuje; titulky se neposílají podruhé. HTTP 200 samo o sobě není potvrzení.", ""]
    return "\n".join(lines)


class Backfill:
    def __init__(self, target, source_session, root: Path, state_path: Path, report_path: Path,
                 persist=None, dry_run=False):
        self.target, self.source_session = target, source_session
        self.root, self.state_path, self.report_path = root, state_path, report_path
        self.persist, self.dry_run = persist, dry_run
        self.state = json.loads(state_path.read_text()) if state_path.exists() else {"schema_version": 1, "videos": {}, "next_page": 0}
        self.sources = source_map(root)

    def save(self, publish=True):
        self.state["updated_at"] = now().isoformat()
        atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        atomic_write(self.report_path, report_markdown(self.state))
        if self.persist and publish:
            # An upload intent MUST reach the remote repository before POST.
            self.persist(self.state_path, "flush")

    def status(self, row, status, hours=0):
        row.update(status=status, checked_at=now().isoformat(), retry_after=(now() + timedelta(hours=hours)).isoformat())
        print(f"subtitle_target={row['target_video_id']} status={status}", flush=True)

    def inspect(self, target):
        row = self.state["videos"].setdefault(target.video_id, {"target_video_id": target.video_id, "status": "pending"})
        row.update(title=target.title, detail_url=target.detail_url, seen_at=now().isoformat())
        if target.video_id in self.sources:
            row["source"] = self.sources[target.video_id]
        if row.get("intent"):
            if row.get("status") == "attached_verified" and not any(track["id"] == row["intent"].get("subtitle_id") and not track["processing"] for track in target.tracks):
                self.status(row, "upload_unconfirmed", 1)
            return row
        if target.has_czech:
            self.status(row, "already_has_czech", 24)
        elif target.unknown_tracks:
            self.status(row, "existing_tracks_uncertain", 24)
        elif not target.ready:
            self.status(row, "target_processing", 1)
        elif row["status"] in {"target_processing", "already_has_czech", "existing_tracks_uncertain"}:
            self.status(row, "pending")
        if target.ready and not target.has_czech and not target.unknown_tracks and not row.get("source"):
            self.status(row, "source_provenance_missing", 24)
        return row

    def inventory(self, max_pages):
        # Start with the oldest pages: most new uploads are still processing.
        _, last = self.target.listing()
        page = min(self.state.get("next_page") or last, last)
        seen = set()
        for _ in range(max_pages):
            targets, _ = self.target.listing(page)
            signature = tuple(target.video_id for target in targets)
            if signature in seen:
                raise TargetUnavailable("target_inventory_pagination_repeated")
            seen.add(signature)
            for target in targets:
                self.inspect(target)
            self.state["next_page"] = page - 1
            self.save(publish=False)
            print(f"subtitle_inventory_page={page} rows={len(targets)}", flush=True)
            if page == 1:
                self.state["last_full_sweep_at"] = now().isoformat()
                break
            page -= 1
        self.save()

    def reconcile(self, row):
        target = self.target.refresh(row["target_video_id"], row["title"])
        intent = row["intent"]
        stem = Path(intent["filename"]).stem
        own = [track for track in target.tracks if track["id"] not in intent["before_ids"]
               and (stem in track["name"] or track["id"] == intent.get("subtitle_id"))]
        if len(own) != 1:
            self.status(row, "upload_unconfirmed", 1)
            return False
        track = own[0]
        intent["subtitle_id"] = track["id"]
        if track["processing"]:
            self.status(row, "attached_processing", 1)
            return False
        # Change only the newly created, hash-named subtitle owned by this job.
        if not intent.get("language_confirmed"):
            if self.dry_run:
                self.status(row, "upload_unconfirmed", 1)
                return False
            self.target.set_own_czech_language(track, target.video_id)
            # Read again after the change. The filename itself proves Czech
            # provenance; the selector verifies the site's language assignment.
            checked = self.target.refresh(target.video_id, row["title"])
            found = next((item for item in checked.tracks if item["id"] == track["id"]), None)
            if not found or found["processing"]:
                self.status(row, "upload_unconfirmed", 1)
                return False
            intent["language_confirmed"] = True
        self.status(row, "attached_verified", 24)
        return True

    def process(self, row):
        if row.get("intent"):
            return self.reconcile(row)
        target = self.target.refresh(row["target_video_id"], row["title"])
        self.inspect(target)
        if not target.ready or target.has_czech or target.unknown_tracks or not row.get("source"):
            return False
        print(f"subtitle_extract_start target={target.video_id} source={row['source']['source_id']}", flush=True)
        payload, evidence = extract_original(self.source_session, row["source"])
        digest = hashlib.sha256(payload).hexdigest()
        row["extraction"] = {**evidence, "sha256": digest, "bytes": len(payload), "format": "utf8_srt_crlf"}
        if self.dry_run:
            self.status(row, "source_subtitle_available")
            return False
        # Extraction may take minutes; check processing/existing captions again.
        target = self.target.refresh(target.video_id, row["title"])
        self.inspect(target)
        if not target.ready or target.has_czech or target.unknown_tracks:
            return False
        row["intent"] = {"filename": f"cs-{target.video_id}-{digest[:10]}.srt",
                         "before_ids": [track["id"] for track in target.tracks],
                         "created_at": now().isoformat(), "sha256": digest}
        self.status(row, "upload_unconfirmed", 1)
        self.save()
        self.target.upload(target, row["intent"]["filename"], payload)
        time.sleep(5)
        return self.reconcile(row)

    def run(self, max_pages, max_sources, limit):
        self.inventory(max_pages)
        attempted, attached, submitted = 0, 0, 0
        deadline = time.monotonic() + 35 * 60
        rows = sorted(self.state["videos"].values(), key=lambda row: (row.get("checked_at", ""), int(row["target_video_id"])))
        for row in rows:
            if attempted >= max_sources or submitted >= limit or time.monotonic() >= deadline:
                break
            if row.get("retry_after", "") > now().isoformat():
                continue
            if row.get("status") in {"attached_verified", "already_has_czech", "source_provenance_missing", "existing_tracks_uncertain", "target_processing"}:
                continue
            attempted += 1
            had_intent = bool(row.get("intent"))
            try:
                attached += int(self.process(row))
            except SubtitleUnavailable as error:
                reason = str(error)
                self.status(row, reason, 168 if reason in MANUAL_REASONS else 6)
            except SdilejError:
                self.status(row, "source_unavailable", 6)
            except (TargetUnavailable, requests.RequestException) as error:
                # Do not log exception text; it may include signed media URLs.
                self.status(row, "upload_unconfirmed" if row.get("intent") else "target_unavailable" if isinstance(error, TargetUnavailable) else "source_unavailable", 1)
            submitted += int(not had_intent and bool(row.get("intent")))
            self.save()
        self.save()
        print(json.dumps({"inspected_sources": attempted, "attachments_submitted": submitted, "attached_verified": attached,
                          "statuses": dict(Counter(row.get("status") for row in self.state["videos"].values()))}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, default=Path("state/subtitle-backfill.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/subtitle-backfill.md"))
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-sources", type=int, default=12)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--git-persist", action="store_true")
    parser.add_argument("--browser-state", type=Path, help="Local diagnostic only; never saved to reports")
    args = parser.parse_args()
    if min(args.max_pages, args.max_sources, args.limit) < 1:
        parser.error("Limits must be positive")
    if args.dry_run and args.git_persist:
        parser.error("Dry-run reports must not replace production state")
    if args.browser_state and not args.dry_run:
        parser.error("Browser sessions are supported for read-only diagnostics only")
    if args.browser_state:
        source, target = requests.Session(), requests.Session()
        for cookie in json.loads(args.browser_state.read_text())["cookies"]:
            domain = cookie["domain"].lstrip(".")
            session = source if domain == "sdilej.cz" or domain.endswith(".sdilej.cz") else target if domain in {"prehrajto.cz", "prehraj.to"} else None
            if session is not None:
                session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    else:
        source = source_login(os.environ["SDILEJ_EMAIL"], os.environ["SDILEJ_PASSWORD"])
        target = target_login(os.environ["PREHRAJTO_EMAIL"], os.environ["PREHRAJTO_PASSWORD"])
    state_path = args.root / args.state
    report_path = args.root / args.report
    persist = GitStatePersister(args.root, (report_path,)) if args.git_persist else None
    worker = Backfill(SubtitleTarget(target), source, args.root, state_path, report_path, persist, args.dry_run)
    try:
        worker.run(args.max_pages, args.max_sources, args.limit)
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Tracebacks from requests/subprocess can leak temporary auth URLs.
        print(f"subtitle_backfill_failed type={type(error).__name__}", flush=True)
        raise SystemExit(1) from None
