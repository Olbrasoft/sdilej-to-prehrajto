from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .backlog import load_backlog
from .git_state import GitStatePersister
from .language import WhisperLanguageDetector
from .pipeline import MAX_PILOT_FILMS, SyncPipeline, plan_sha, write_plan, write_report
from .prehrajto import login as login_prehrajto
from .sdilej import SdilejProvider, login as login_sdilej
from .state import StateStore
from .sources import SelectedSourceStore
from .subtitles import SubtitleQueue


def repo_root() -> Path:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        return Path(workspace).resolve()
    current = Path.cwd().resolve()
    if (current / "backlog/films.jsonl.gz").exists():
        return current
    return Path(__file__).resolve().parents[2]


REPO_ROOT = repo_root()
MAX_CONTINUOUS_FILMS = 50
MAX_PREPARE_FILMS = 200


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument(
        "mode", choices=("plan", "prepare", "upload", "continuous", "export-results")
    )
    result.add_argument("--limit", type=int, default=1)
    result.add_argument("--approved-plan-sha")
    result.add_argument("--approved-plan-file", type=Path)
    result.add_argument("--backlog", type=Path, default=REPO_ROOT / "backlog/films.jsonl.gz")
    result.add_argument("--state", type=Path, default=REPO_ROOT / "state/sync.json")
    result.add_argument("--plan-out", type=Path, default=REPO_ROOT / "artifacts/plan.json")
    result.add_argument("--report-out", type=Path, default=REPO_ROOT / "artifacts/plan.md")
    result.add_argument(
        "--results-out",
        type=Path,
        default=REPO_ROOT / "manifests/film-results.jsonl",
    )
    result.add_argument("--persist-git-state", action="store_true")
    result.add_argument("--max-scan", type=int, default=200)
    result.add_argument(
        "--workers", type=int, default=int(os.environ.get("UPLOAD_WORKERS", "4"))
    )
    return result


def main() -> int:
    args = parser().parse_args()
    source_manifest_path = REPO_ROOT / "manifests/selected-sources.jsonl"
    if args.mode == "export-results":
        SelectedSourceStore(source_manifest_path).export_results(
            args.state, args.results_out
        )
        print(f"results={args.results_out}")
        return 0
    if args.mode == "prepare":
        maximum = MAX_PREPARE_FILMS
    elif args.mode == "continuous":
        maximum = MAX_CONTINUOUS_FILMS
    else:
        maximum = MAX_PILOT_FILMS
    if not 1 <= args.limit <= maximum:
        raise ValueError(f"Limit must be between 1 and {maximum}")
    if args.mode == "continuous" and os.environ.get("CONTINUOUS_ENABLED") != "true":
        raise RuntimeError("Continuous mode requires CONTINUOUS_ENABLED=true")
    if not 1 <= args.workers <= 4:
        raise ValueError("--workers must be between 1 and 4")
    default_state = REPO_ROOT / "state/sync.json"
    if args.mode in {"plan", "prepare"} and args.state.resolve() == default_state:
        args.state = REPO_ROOT / "state/source-scan.json"
    source_email = required_env("SDILEJ_EMAIL")
    source_password = required_env("SDILEJ_PASSWORD")
    target_email = required_env("PREHRAJTO_EMAIL")
    target_password = required_env("PREHRAJTO_PASSWORD")
    source_session = login_sdilej(source_email, source_password)
    target_session = login_prehrajto(target_email, target_password)
    subtitle_path = REPO_ROOT / "plans/subtitle-followup.jsonl"
    persister = (
        GitStatePersister(
            REPO_ROOT,
            (
                subtitle_path,
                source_manifest_path,
            ),
        )
        if args.persist_git_state
        else None
    )
    state = StateStore(args.state, on_persist=persister)
    pipeline = SyncPipeline(
        source_provider=SdilejProvider(source_session, WhisperLanguageDetector()),
        source_session=source_session,
        target_session=target_session,
        state=state,
        subtitle_queue=SubtitleQueue(subtitle_path),
        selected_sources=SelectedSourceStore(source_manifest_path),
    )
    if args.max_scan < args.limit:
        raise ValueError("--max-scan must be at least --limit")
    if args.mode == "prepare":
        plan = pipeline.prepare_sources(
            load_backlog(args.backlog), args.limit, max_scan=args.max_scan
        )
        digest = write_plan(args.plan_out, plan)
        write_report(args.report_out, plan, digest)
        pipeline.selected_sources.compact()
        state.persist_external("flush")
        print(f"prepared={len(plan)} plan_sha={digest}")
        return 0
    if args.mode == "upload" and args.approved_plan_file:
        approved = json.loads(args.approved_plan_file.read_text(encoding="utf-8"))
        plan = approved["films"]
        digest = plan_sha(plan)
        if approved.get("plan_sha") != digest:
            raise ValueError("Approved plan file digest is invalid")
        if not args.approved_plan_sha:
            raise ValueError("--approved-plan-sha is required for upload")
        if args.approved_plan_sha != digest:
            raise ValueError("Approved plan SHA does not match the reviewed plan")
        if len(plan) != args.limit:
            raise ValueError("Approved plan size does not match --limit")
        pipeline.load_approved_plan(plan)
        print(f"approved_plan_loaded={len(plan)} plan_sha={digest}")
    else:
        plan = pipeline.build_plan(
            load_backlog(args.backlog),
            args.limit,
            max_scan=args.max_scan,
            verified_only=args.mode == "continuous",
        )
        digest = write_plan(args.plan_out, plan)
        write_report(args.report_out, plan, digest)
        print(f"planned={len(plan)} plan_sha={digest}")
    if args.mode == "plan":
        pipeline.selected_sources.compact()
        state.persist_external("flush")
        return 0
    if args.mode == "upload":
        if not args.approved_plan_sha:
            raise ValueError("--approved-plan-sha is required for upload")
        if args.approved_plan_sha != digest:
            raise ValueError("Approved plan SHA does not match the reviewed plan")
    session_pairs = [(source_session, target_session)]
    for _index in range(1, min(args.workers, len(plan))):
        session_pairs.append(
            (
                login_sdilej(source_email, source_password),
                login_prehrajto(target_email, target_password),
            )
        )
    try:
        pipeline.execute(plan, session_pairs=session_pairs)
    finally:
        pipeline.selected_sources.compact()
        state.persist_external("flush")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
