#!/usr/bin/env python3
"""Export a read-only, rating-prioritized film backlog from the CR database."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import psycopg2.extras


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "backlog" / "films.jsonl.gz"

RATING_PRIORS = {
    "imdb": (6.8, 2_500),
    "csfd": (6.8, 1_000),
    "tmdb": (6.5, 500),
}


def fetch_films(connection) -> list[dict[str, Any]]:
    sql = """
        SELECT
            id AS cr_film_id,
            slug AS cr_slug,
            title,
            original_title,
            year,
            runtime_min,
            lang AS original_language,
            description,
            imdb_id,
            tmdb_id,
            csfd_id,
            imdb_rating,
            imdb_votes,
            csfd_rating,
            csfd_rating_count,
            tmdb_rating,
            tmdb_vote_count
        FROM films
        WHERE title IS NOT NULL
          AND btrim(title) <> ''
        ORDER BY id
    """
    with connection.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cursor:
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()["transaction_read_only"] != "on":
            raise RuntimeError("Database session is not read-only")
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


def rating_details(film: dict[str, Any]) -> tuple[str | None, float | None, int, float]:
    if film.get("imdb_rating") is not None:
        source = "imdb"
        rating = float(film["imdb_rating"])
        votes = int(film.get("imdb_votes") or 0)
    elif film.get("csfd_rating") is not None:
        source = "csfd"
        rating = float(film["csfd_rating"]) / 10.0
        votes = int(film.get("csfd_rating_count") or 0)
    elif film.get("tmdb_rating") is not None:
        source = "tmdb"
        rating = float(film["tmdb_rating"])
        votes = int(film.get("tmdb_vote_count") or 0)
    else:
        return None, None, 0, 0.0

    prior_rating, prior_votes = RATING_PRIORS[source]
    score = ((votes * rating) + (prior_votes * prior_rating)) / (votes + prior_votes)
    return source, rating, votes, round(score, 6)


def search_titles(film: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    for value in (film.get("title"), film.get("original_title")):
        title = (value or "").strip()
        if title and title.casefold() not in {item.casefold() for item in titles}:
            titles.append(title)
    return titles


def prepare_films(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        source, rating, votes, score = rating_details(row)
        prepared.append(
            {
                **row,
                "search_titles": search_titles(row),
                "rating_source": source,
                "rating_value": rating,
                "rating_votes": votes,
                "priority_score": score,
            }
        )

    prepared.sort(
        key=lambda film: (
            -film["priority_score"],
            -film["rating_votes"],
            -(film["rating_value"] or 0.0),
            -(film.get("year") or 0),
            film["cr_film_id"],
        )
    )
    for rank, film in enumerate(prepared, start=1):
        film["priority_rank"] = rank
    return prepared


def write_jsonl_gzip(path: Path, films: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with temporary_path.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8") as output:
                    for film in films:
                        output.write(json.dumps(film, ensure_ascii=False) + "\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if not args.db_url:
        parser.error("--db-url or DATABASE_URL is required")

    connection = psycopg2.connect(args.db_url)
    try:
        connection.set_session(readonly=True, autocommit=False)
        films = prepare_films(fetch_films(connection))
        connection.rollback()
    finally:
        connection.close()

    if args.limit is not None:
        films = films[: args.limit]
    write_jsonl_gzip(args.out, films)

    coverage: dict[str, int] = {}
    for film in films:
        source = film["rating_source"] or "none"
        coverage[source] = coverage.get(source, 0) + 1
    print(f"Wrote {len(films)} films to {args.out}")
    print("Rating sources: " + ", ".join(f"{key}={value}" for key, value in sorted(coverage.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
