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
MAX_PREPARE_WORKERS = 6
MAX_DEEP_PREPARE_WORKERS = 3
FAST_DISCOVERY_CANDIDATES = 3
FAST_DISCOVERY_TIMEOUT_SECONDS = 90


def exclude_uploaded_films(
    films: list[Film], upload_state: StateStore
) -> list[Film]:
    return [film for film in films if not upload_state.uploaded(film.cr_film_id)]


def additional_worker_count(
    workers: int, plan_size: int, *, refill_enabled: bool = False
) -> int:
    if plan_size == 0:
        return 0
    capacity = workers if refill_enabled else min(workers, plan_size)
    return max(0, capacity - 1)


def deep_prepare_worker_count(workers: int) -> int:
    """Reserve fast capacity while scaling the accumulated deep queue."""
    return min(MAX_DEEP_PREPARE_WORKERS, max(0, workers - 1))


def prepare_source_batch(
    pipelines: list[SyncPipeline],
    films: list[Film],
    limit: int,
    *,
    max_scan: int,
    deadline_monotonic: float | None,
    deep_scan_only: bool = False,
) -> list[dict]:
    """Search disjoint backlog slices concurrently and merge their results."""
    worker_count = min(len(pipelines), limit)
    base_limit, extra = divmod(limit, worker_count)

    def prepare(worker_index: int) -> list[dict]:
        worker_limit = base_limit + (1 if worker_index < extra else 0)
        lane_options = {"deep_scan_only": True} if deep_scan_only else {}
        return pipelines[worker_index].prepare_sources(
            films[worker_index::worker_count],
            worker_limit,
            max_scan=max_scan,
            deadline_monotonic=deadline_monotonic,
            **lane_options,
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
        "--workers", type=int, default=int(os.environ.get("UPLOAD_WORKERS", "6"))
    )
    return result


def prepare_source_lane(
    pipelines: list[SyncPipeline],
    films: list[Film],
    limit: int,
    *,
    max_scan: int,
    deadline_monotonic: float | None,
    deep_scan_only: bool = False,
) -> list[dict]:
    """Keep one preparation lane active until its workflow deadline."""
    plan: list[dict] = []
    pipeline = pipelines[0]
    try:
        while True:
            batch = prepare_source_batch(
                pipelines,
                films,
                limit,
                max_scan=max_scan,
                deadline_monotonic=deadline_monotonic,
                deep_scan_only=deep_scan_only,
            )
            plan.extend(batch)
            if deadline_monotonic is None or time.monotonic() >= deadline_monotonic:
                break
            if not batch:
                remaining = max(0.0, deadline_monotonic - time.monotonic())
                time.sleep(min(5.0, remaining))
    finally:
        # Source, deep-queue and negative-result events already have bounded
        # durable checkpoint intervals. Flushing every empty polling pass
        # defeats those intervals and floods main with operational commits.
        pipeline.selected_sources.compact()
        pipeline.state.persist_external("flush")
    return plan


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
    if not 1 <= args.workers <= 6:
        raise ValueError("--workers must be between 1 and 6")
    if not 0 <= args.prepare_runtime_minutes <= 330:
        raise ValueError("--prepare-runtime-minutes must be between 0 and 330")
    if not 1 <= args.prepare_workers <= MAX_PREPARE_WORKERS:
        raise ValueError(
            f"--prepare-workers must be between 1 and {MAX_PREPARE_WORKERS}"
        )
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
    persisted_extras = [subtitle_path]
    if args.mode in {"plan", "prepare"}:
        persisted_extras.append(source_manifest_path)
    persister = (
        GitStatePersister(
            REPO_ROOT,
            tuple(persisted_extras),
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
        deep_workers = deep_prepare_worker_count(args.prepare_workers)
        if args.prepare_workers > 1:
            pipeline.source_provider.max_candidates = FAST_DISCOVERY_CANDIDATES
            pipeline.source_provider.discovery_timeout_seconds = (
                FAST_DISCOVERY_TIMEOUT_SECONDS
            )
        prepare_pipelines = [pipeline]
        for worker_index in range(1, args.prepare_workers):
            worker_session = login_sdilej(source_email, source_password)
            deep_worker = worker_index >= args.prepare_workers - deep_workers
            prepare_pipelines.append(
                SyncPipeline(
                    source_provider=SdilejProvider(
                        worker_session,
                        WhisperLanguageDetector(),
                        max_candidates=(
                            None if deep_worker else FAST_DISCOVERY_CANDIDATES
                        ),
                        discovery_timeout_seconds=(
                            300 if deep_worker else FAST_DISCOVERY_TIMEOUT_SECONDS
                        ),
                        allow_unresolved_fallback=deep_worker,
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
        if len(prepare_pipelines) == 1:
            plan = prepare_source_lane(
                prepare_pipelines,
                backlog,
                args.limit,
                max_scan=args.max_scan,
                deadline_monotonic=deadline,
            )
        else:
            # Keep dedicated workers on difficult films while the remaining
            # workers continue supplying easy, quickly verified sources.
            fast_pipelines = prepare_pipelines[:-deep_workers]
            deep_pipelines = prepare_pipelines[-deep_workers:]
            deep_limit = max(1, args.limit // len(prepare_pipelines))
            fast_limit = max(1, args.limit - deep_limit)
            with ThreadPoolExecutor(max_workers=2) as executor:
                fast_future = executor.submit(
                    prepare_source_lane,
                    fast_pipelines,
                    backlog,
                    fast_limit,
                    max_scan=args.max_scan,
                    deadline_monotonic=deadline,
                )
                deep_future = executor.submit(
                    prepare_source_lane,
                    deep_pipelines,
                    backlog,
                    deep_limit,
                    max_scan=args.max_scan,
                    deadline_monotonic=deadline,
                    deep_scan_only=True,
                )
                plan = fast_future.result() + deep_future.result()
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
        backlog = load_backlog(args.backlog)
        plan = pipeline.build_plan(
            backlog,
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
    additional_workers = additional_worker_count(
        args.workers,
        len(plan),
        refill_enabled=args.mode == "continuous",
    )
    if additional_workers:
        # Every login pair performs several independent network requests. Doing
        # five pairs serially can leave a six-worker run apparently idle for
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
    refill_plan = None
    if args.mode == "continuous":
        def refill_plan() -> list[dict]:
            payload = (
                persister.read_remote_file("manifests/selected-sources.jsonl")
                if persister is not None
                else source_manifest_path.read_text(encoding="utf-8")
            )
            merged = pipeline.selected_sources.merge_jsonl(payload)
            if merged:
                print(f"verified_sources_refreshed={merged}", flush=True)
            return pipeline.build_plan(
                backlog,
                args.limit,
                max_scan=args.max_scan,
                verified_only=True,
            )

    try:
        pipeline.execute(
            plan,
            session_pairs=session_pairs,
            refill_plan=refill_plan,
        )
    finally:
        state.persist_external("flush")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
