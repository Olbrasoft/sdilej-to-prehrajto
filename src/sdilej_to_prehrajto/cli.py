from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backlog import load_backlog
from .git_state import GitStatePersister
from .language import WhisperLanguageDetector
from .models import Film
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


def exclude_uploaded_films(
    films: list[Film], upload_state: StateStore
) -> list[Film]:
    return [film for film in films if not upload_state.uploaded(film.cr_film_id)]


def additional_worker_count(workers: int, plan_size: int) -> int:
    return max(0, min(workers, plan_size) - 1)


def prepare_source_batch(
    pipelines: list[SyncPipeline],
    films: list[Film],
    limit: int,
    *,
    max_scan: int,
    deadline_monotonic: float | None,
) -> list[dict]:
    """Search disjoint backlog slices concurrently and merge their results."""
    worker_count = min(len(pipelines), limit)
    base_limit, extra = divmod(limit, worker_count)

    def prepare(worker_index: int) -> list[dict]:
        worker_limit = base_limit + (1 if worker_index < extra else 0)
        return pipelines[worker_index].prepare_sources(
            films[worker_index::worker_count],
            worker_limit,
            max_scan=max_scan,
            deadline_monotonic=deadline_monotonic,
        )

    if worker_count == 1:
        return prepare(0)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        batches = executor.map(prepare, range(worker_count))
        return [row for batch in batches for row in batch]


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
    result.add_argument("--prepare-runtime-minutes", type=int, default=0)
    result.add_argument(
        "--prepare-workers",
        type=int,
        default=int(os.environ.get("PREPARE_WORKERS", "1")),
    )
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
    if not 0 <= args.prepare_runtime_minutes <= 330:
        raise ValueError("--prepare-runtime-minutes must be between 0 and 330")
    if not 1 <= args.prepare_workers <= 2:
        raise ValueError("--prepare-workers must be between 1 and 2")
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
    if args.mode in {"upload", "continuous"}:
        released_claims = state.release_claims_from_other_run(
            os.environ.get("GITHUB_RUN_ID")
        )
        if released_claims:
            print(f"released_orphaned_claims={released_claims}", flush=True)
    subtitle_queue = SubtitleQueue(subtitle_path)
    selected_sources = SelectedSourceStore(source_manifest_path)
    pipeline = SyncPipeline(
        source_provider=SdilejProvider(source_session, WhisperLanguageDetector()),
        source_session=source_session,
        target_session=target_session,
        state=state,
        subtitle_queue=subtitle_queue,
        selected_sources=selected_sources,
    )
    if args.max_scan < args.limit:
        raise ValueError("--max-scan must be at least --limit")
    if args.mode == "prepare":
        prepare_pipelines = [pipeline]
        for _worker_index in range(1, args.prepare_workers):
            worker_session = login_sdilej(source_email, source_password)
            prepare_pipelines.append(
                SyncPipeline(
                    source_provider=SdilejProvider(
                        worker_session, WhisperLanguageDetector()
                    ),
                    source_session=worker_session,
                    target_session=target_session,
                    state=state,
                    subtitle_queue=subtitle_queue,
                    selected_sources=selected_sources,
                )
            )
        backlog = exclude_uploaded_films(
            load_backlog(args.backlog), StateStore(default_state)
        )
        deadline = (
            time.monotonic() + args.prepare_runtime_minutes * 60
            if args.prepare_runtime_minutes
            else None
        )
        plan: list[dict] = []
        while True:
            before = (len(pipeline.selected_sources), state.tracked_films())
            batch = prepare_source_batch(
                prepare_pipelines,
                backlog,
                args.limit,
                max_scan=args.max_scan,
                deadline_monotonic=deadline,
            )
            plan.extend(batch)
            pipeline.selected_sources.compact()
            state.persist_external("flush")
            after = (len(pipeline.selected_sources), state.tracked_films())
            if deadline is None or time.monotonic() >= deadline:
                break
            if after == before:
                # A no-change batch does not mean the 28k-film backlog is
                # exhausted. Previously inspected films may now be deferred,
                # allowing the next pass to reach a lower backlog segment.
                # Avoid a hot loop only when every remaining film is deferred.
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(5.0, remaining))
        digest = write_plan(args.plan_out, plan)
        write_report(args.report_out, plan, digest)
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
    additional_workers = additional_worker_count(args.workers, len(plan))
    if additional_workers:
        # Every login pair performs several independent network requests. Doing
        # three pairs serially can leave a four-worker run apparently idle for
        # minutes before the first target video is prepared.
        def login_worker_pair() -> tuple[object, object]:
            return (
                login_sdilej(source_email, source_password),
                login_prehrajto(target_email, target_password),
            )

        with ThreadPoolExecutor(max_workers=additional_workers) as executor:
            session_pairs.extend(
                executor.map(
                    lambda _index: login_worker_pair(),
                    range(additional_workers),
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
