from __future__ import annotations

import argparse
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


REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_CONTINUOUS_FILMS = 50


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("mode", choices=("plan", "upload", "continuous"))
    result.add_argument("--limit", type=int, default=1)
    result.add_argument("--approved-plan-sha")
    result.add_argument("--backlog", type=Path, default=REPO_ROOT / "backlog/films.jsonl.gz")
    result.add_argument("--state", type=Path, default=REPO_ROOT / "state/sync.json")
    result.add_argument("--plan-out", type=Path, default=REPO_ROOT / "artifacts/plan.json")
    result.add_argument("--report-out", type=Path, default=REPO_ROOT / "artifacts/plan.md")
    result.add_argument("--persist-git-state", action="store_true")
    result.add_argument("--max-scan", type=int, default=200)
    return result


def main() -> int:
    args = parser().parse_args()
    maximum = MAX_CONTINUOUS_FILMS if args.mode == "continuous" else MAX_PILOT_FILMS
    if not 1 <= args.limit <= maximum:
        raise ValueError(f"Limit must be between 1 and {maximum}")
    if args.mode == "continuous" and os.environ.get("CONTINUOUS_ENABLED") != "true":
        raise RuntimeError("Continuous mode requires CONTINUOUS_ENABLED=true")
    source_session = login_sdilej(
        required_env("SDILEJ_EMAIL"), required_env("SDILEJ_PASSWORD")
    )
    target_session = login_prehrajto(
        required_env("PREHRAJTO_EMAIL"), required_env("PREHRAJTO_PASSWORD")
    )
    subtitle_path = REPO_ROOT / "plans/subtitle-followup.jsonl"
    source_manifest_path = REPO_ROOT / "manifests/selected-sources.jsonl"
    persister = (
        GitStatePersister(REPO_ROOT, (subtitle_path, source_manifest_path))
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
    plan = pipeline.build_plan(
        load_backlog(args.backlog), args.limit, max_scan=args.max_scan
    )
    digest = write_plan(args.plan_out, plan)
    write_report(args.report_out, plan, digest)
    print(f"planned={len(plan)} plan_sha={digest}")
    if args.mode == "plan":
        return 0
    if args.mode == "upload":
        if not args.approved_plan_sha:
            raise ValueError("--approved-plan-sha is required for upload")
        if args.approved_plan_sha != plan_sha(plan):
            raise ValueError("Approved plan SHA does not match the freshly resolved plan")
    pipeline.execute(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
