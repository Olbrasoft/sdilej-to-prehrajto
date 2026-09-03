from __future__ import annotations

import html
import json
import mimetypes
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import replace
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

from .matching import classify_candidate
from .models import Candidate, Film, LanguageTier, MatchTier
from .ranking import (
    infer_video_codec,
    language_tier,
    quality_acceptable,
    resolution_rank,
)
from .security import redact, safe_url
from .language import LanguageDetectionError


BASE_URL = "https://sdilej.cz"
LOGIN_PAGE = f"{BASE_URL}/prihlasit"
LOGIN_URL = f"{BASE_URL}/sql.php"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
VIDEO_URL_RE = re.compile(r"^https://sdilej\.cz/(?P<id>\d+)/")
VIDEO_EXTENSIONS = {"avi", "m2ts", "m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "ts", "webm", "wmv"}
SIZE_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>KB|MB|GB)(?![A-Za-z])",
    re.I,
)
DURATION_RE = re.compile(r"(?:Délka:\s*)?(?P<time>\d{1,2}:\d{2}:\d{2})", re.I)
DIMENSIONS_RE = re.compile(r"(?P<width>\d{3,4})\s*[x×]\s*(?P<height>\d{3,4})", re.I)
PLAYER_URL_RE = re.compile(
    r"https://stream\d+\.sdilej\.cz/(?:sdilej_profi|download_free_stream)\.php\?[^\"'<> ]+",
    re.I,
)


class SdilejError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(redact(message))
        self.permanent = permanent


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    normalized = normalized.lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normalized)).strip("-")


def parse_size(text: str) -> int | None:
    match = SIZE_RE.search(text)
    if not match:
        return None
    value = float(match.group("value").replace(",", "."))
    multiplier = {"kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000}[
        match.group("unit").lower()
    ]
    return int(value * multiplier)


def parse_duration(text: str) -> int | None:
    match = re.search(r"Délka:?\s*(?P<time>\d{1,2}:\d{2}:\d{2})", text, re.I)
    if not match:
        match = DURATION_RE.search(text)
    if not match:
        return None
    hours, minutes, seconds = [int(part) for part in match.group("time").split(":")]
    return hours * 3600 + minutes * 60 + seconds


def parse_resolution(text: str) -> tuple[int, int]:
    match = DIMENSIONS_RE.search(text)
    if match:
        return int(match.group("width")), int(match.group("height"))
    lowered = text.lower()
    if "4k" in lowered or "2160p" in lowered or "uhd" in lowered:
        return 3840, 2160
    if "1440p" in lowered:
        return 2560, 1440
    if "1080p" in lowered or "full hd" in lowered or "fullhd" in lowered:
        return 1920, 1080
    if "720p" in lowered or re.search(r"\bhd\b", lowered):
        return 1280, 720
    if "480p" in lowered:
        return 854, 480
    return 0, 0


def parse_search_html(html_text: str, query: str | None = None) -> list[Candidate]:
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[Candidate] = []
    seen: set[str] = set()
    for box in soup.select("div.videobox"):
        anchor = box.select_one("a.webm-hover[href]") or box.select_one("a[href]")
        if anchor is None:
            continue
        url = urllib.parse.urljoin(BASE_URL, anchor.get("href", ""))
        match = VIDEO_URL_RE.match(url)
        if not match or match.group("id") in seen:
            continue
        title = html.unescape(anchor.get("title") or anchor.get_text(" ", strip=True))
        extension = urllib.parse.urlsplit(url).path.rsplit(".", 1)[-1].lower()
        if extension not in VIDEO_EXTENSIONS:
            continue
        seen.add(match.group("id"))
        metadata = box.get_text(" ", strip=True)
        width, height = parse_resolution(metadata)
        results.append(
            Candidate(
                source_id=match.group("id"),
                url=url,
                title=title,
                size_bytes=parse_size(metadata),
                duration_sec=parse_duration(metadata),
                width=width,
                height=height,
                query=query,
            )
        )
    return results


def parse_detail_html(html_text: str, candidate: Candidate) -> Candidate:
    soup = BeautifulSoup(html_text, "html.parser")
    heading = soup.select_one("h1")
    filename = heading.get_text(" ", strip=True) if heading else candidate.title
    page_text = soup.get_text(" ", strip=True)
    width, height = parse_resolution(page_text)
    fast_link = next(
        (
            urllib.parse.urljoin(BASE_URL, anchor.get("href", ""))
            for anchor in soup.select("a[href]")
            # The current site wraps "rychle" in a nested span, so relying on
            # one flattened text node rejects a valid authenticated download.
            if " ".join(anchor.stripped_strings).casefold() == "stáhnout rychle"
            and "sdilej_profi.php" in anchor.get("href", "")
        ),
        None,
    )
    if not fast_link:
        raise SdilejError(
            "Authenticated fast download link is unavailable; premium login is required"
        )
    player_match = PLAYER_URL_RE.search(html.unescape(html_text))
    if not player_match:
        raise SdilejError("Player URL for language sampling is unavailable")
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return replace(
        candidate,
        title=filename,
        size_bytes=parse_size(page_text) or candidate.size_bytes,
        duration_sec=parse_duration(page_text) or candidate.duration_sec,
        width=width or candidate.width,
        height=height or candidate.height,
        filename=filename,
        mime_type=mime_type,
        video_codec=infer_video_codec(filename) or candidate.video_codec,
        download_url=fast_link,
        sample_url=player_match.group(0).replace("&amp;", "&"),
    )


def probe_media(download_url: str | None) -> dict[str, Any]:
    """Read original metadata through HTTP ranges without downloading the film."""
    if not download_url:
        return {}
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height:format=duration",
                "-of",
                "json",
                download_url,
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            return {}
        payload = json.loads(result.stdout)
        stream = (payload.get("streams") or [{}])[0]
        codec = stream.get("codec_name")
        duration = (payload.get("format") or {}).get("duration")
        return {
            "video_codec": {"hevc": "h265", "avc": "h264"}.get(codec, codec),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "duration_sec": int(float(duration)) if duration else None,
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {}


def login(
    email: str,
    password: str,
    *,
    session: requests.Session | None = None,
) -> requests.Session:
    if not email or not password:
        raise SdilejError("SDILEJ_EMAIL and SDILEJ_PASSWORD are required")
    client = session or requests.Session()
    client.headers.update({"User-Agent": USER_AGENT})
    prime = client.get(LOGIN_PAGE, timeout=30)
    prime.raise_for_status()
    response = client.post(
        LOGIN_URL,
        data={"login": email, "heslo": password},
        headers={"Referer": LOGIN_PAGE},
        allow_redirects=True,
        timeout=30,
    )
    response.raise_for_status()
    profile = client.get(f"{BASE_URL}/nastaveni", allow_redirects=False, timeout=30)
    if profile.status_code != 200 or "SDILEJ" not in client.cookies:
        raise SdilejError("Sdilej.cz login verification failed")
    return client


class SdilejProvider:
    def __init__(
        self,
        session: requests.Session,
        language_detector,
        *,
        max_candidates: int | None = None,
        discovery_timeout_seconds: float = 300,
        minimum_language_probability: float = 0.65,
        request_gap_seconds: float = 2.0,
        media_probe: Callable[[str | None], dict[str, Any]] = probe_media,
    ):
        self.session = session
        self.language_detector = language_detector
        self.max_candidates = max_candidates
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self.minimum_language_probability = minimum_language_probability
        self.request_gap_seconds = request_gap_seconds
        self.media_probe = media_probe
        self._last_request = 0.0
        self._request_lock = threading.RLock()

    def _verify_candidate(self, film: Film, candidate: Candidate) -> Candidate | None:
        detail = parse_detail_html(self._get(candidate.url).text, candidate)
        media = self.media_probe(detail.download_url)
        detail.video_codec = media.get("video_codec") or detail.video_codec
        detail.width = int(media.get("width") or detail.width)
        detail.height = int(media.get("height") or detail.height)
        detail.duration_sec = int(media.get("duration_sec") or detail.duration_sec or 0)
        verified_match = classify_candidate(
            film,
            detail.title,
            duration_sec=detail.duration_sec,
        )
        detail.match_tier = verified_match.tier
        detail.match_evidence = verified_match.evidence
        if verified_match.tier not in (MatchTier.STRONG, MatchTier.SOLID):
            return None
        if not quality_acceptable(detail):
            return None
        language, probability = self.language_detector.detect(detail.sample_url)
        hint = audio_language_hint(detail.filename)
        resampled = False
        if hint and language_tier(language) != language_tier(hint):
            consensus = getattr(self.language_detector, "detect_consensus", None)
            if consensus:
                resampled = True
                language, probability = consensus(
                    detail.sample_url,
                    detail.duration_sec,
                    initial=(language, probability),
                    preferred_language=hint,
                )
        detail.audio_language = language
        detail.language_probability = probability
        detail.language_evidence = (
            "whisper_multisample_filename_conflict"
            if resampled
            else "whisper_remote_sample"
        )
        if probability < self.minimum_language_probability:
            # An uncertain language sample is not proof that a higher-resolution
            # source is unsuitable. Treat it as retryable so discovery never
            # silently falls back to a lower quality tier.
            raise LanguageDetectionError("Whisper language confidence is too low")
        detail.language_tier = language_tier(language)
        return detail

    def _get(
        self, url: str, *, session: requests.Session | None = None
    ) -> requests.Response:
        # Upload workers use independent authenticated sessions. Serializing
        # those detail refreshes behind the discovery rate-limit lock turns one
        # slow 45-second request into a several-minute six-worker startup gap.
        # Keep discovery throttled, but allow independent worker sessions to
        # refresh their already-approved source URLs concurrently.
        if session is not None:
            try:
                response = session.get(url, timeout=45)
            except requests.RequestException as error:
                raise SdilejError(f"Request failed for {safe_url(url)}") from error
            response.raise_for_status()
            return response
        with self._request_lock:
            wait = self.request_gap_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = (session or self.session).get(url, timeout=45)
            except requests.RequestException as error:
                raise SdilejError(f"Request failed for {safe_url(url)}") from error
            finally:
                self._last_request = time.monotonic()
        response.raise_for_status()
        return response

    def search(self, query: str, quality: str | None = None) -> list[Candidate]:
        suffix = f"--{quality}" if quality else ""
        url = f"{BASE_URL}/{slugify(query)}/s/{suffix}"
        return parse_search_html(self._get(url).text, query=query)

    def search_by_quality(self, query: str) -> list[Candidate]:
        return parse_search_html(
            self._get(f"{BASE_URL}/{slugify(query)}/s/-6").text,
            query=query,
        )

    def refresh_approved(
        self,
        candidate: Candidate,
        *,
        session: requests.Session | None = None,
    ) -> Candidate:
        """Refresh expiring URLs without repeating search or language analysis."""
        return parse_detail_html(
            self._get(candidate.url, session=session).text, candidate
        )

    def discover(self, film: Film) -> list[Candidate]:
        deadline = time.monotonic() + self.discovery_timeout_seconds
        candidates: dict[str, Candidate] = {}
        matched_by_rank: dict[int, list[Candidate]] = {}
        resolved: list[Candidate] = []
        inspected = 0
        titles = dict.fromkeys(
            item for item in (film.title, film.original_title) if item
        )
        for title in titles:
            queries = dict.fromkeys(
                item for item in (f"{title} {film.year}" if film.year else title, title)
                if item
            )
            for query in queries:
                if time.monotonic() >= deadline:
                    raise SdilejError("Discovery deadline expired before search completed")
                for candidate in self.search_by_quality(query):
                    if candidate.source_id in candidates:
                        continue
                    candidates[candidate.source_id] = candidate
                    matched = classify_candidate(
                        film,
                        candidate.title,
                        duration_sec=candidate.duration_sec,
                    )
                    candidate.match_tier = matched.tier
                    candidate.match_evidence = matched.evidence
                    if matched.tier in (MatchTier.STRONG, MatchTier.SOLID):
                        rank = resolution_rank(candidate.width, candidate.height)
                        matched_by_rank.setdefault(rank, []).append(candidate)

        for minimum_tier_rank in sorted(matched_by_rank, reverse=True):
            ordered = sorted(
                matched_by_rank[minimum_tier_rank],
                key=lambda item: (
                    item.size_bytes or 0,
                    item.source_id,
                ),
            )
            unresolved_in_tier = False
            for candidate in ordered:
                if time.monotonic() >= deadline:
                    raise SdilejError(
                        "Discovery deadline expired before verification completed"
                    )
                if self.max_candidates is not None and inspected >= self.max_candidates:
                    if not resolved:
                        raise SdilejError(
                            "Candidate limit reached before verification completed"
                        )
                    return resolved
                inspected += 1
                detail = None
                verification_completed = False
                # A broken remote media endpoint is usually deterministic. Two
                # attempts cover a transient failure without blocking source
                # preparation for tens of minutes on the same candidate.
                for _attempt in range(2):
                    if time.monotonic() >= deadline:
                        # An expired verification is not proof that no source
                        # exists. Let the caller record a short retry instead
                        # of incorrectly deferring this film for 30 days.
                        raise SdilejError(
                            "Discovery deadline expired before verification completed"
                        )
                    try:
                        detail = self._verify_candidate(film, candidate)
                        verification_completed = True
                        break
                    except (
                        SdilejError,
                        LanguageDetectionError,
                        requests.RequestException,
                    ):
                        continue
                if not verification_completed:
                    unresolved_in_tier = True
                    continue
                if detail is None:
                    continue
                resolved.append(detail)
                # This tier is ordered from smallest to largest. Once Czech audio
                # passes the quality floor, no later candidate can outrank it.
                if (
                    detail.language_tier == LanguageTier.CZECH_AUDIO
                    and resolution_rank(detail.width, detail.height)
                    >= minimum_tier_rank
                    and not unresolved_in_tier
                ):
                    return resolved
            if unresolved_in_tier:
                # A failed 4K (or other higher-tier) verification leaves its
                # language unknown. Falling through could upload 1080p even
                # though that unresolved source is Czech 4K. Defer the film and
                # retry it in a later preparation pass instead.
                raise SdilejError(
                    "A preferred quality tier could not be fully verified"
                )
        return resolved

def audio_language_hint(filename: str | None) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", (filename or "").lower()).strip()
    if re.search(r"\b(?:cz|cze|czech|cesky)\s+(?:tit|titulky|sub)\b", normalized):
        return None
    if re.search(r"\b(?:cz|cze|czech|cesky)(?:\s+(?:dab|dabing|dubbing))?\b", normalized):
        return "cs"
    if re.search(r"\b(?:sk|svk|slovak)(?:\s+(?:dab|dabing|dubbing))?\b", normalized):
        return "sk"
    return None
