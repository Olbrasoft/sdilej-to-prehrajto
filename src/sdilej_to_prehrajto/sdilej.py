from __future__ import annotations

import html
import mimetypes
import re
import time
import unicodedata
import urllib.parse
from dataclasses import replace

import requests
from bs4 import BeautifulSoup

from .matching import classify_candidate
from .models import Candidate, Film, LanguageTier, MatchTier
from .ranking import language_tier
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
SIZE_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>KB|MB|GB)", re.I)
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
            if anchor.get_text(" ", strip=True).casefold() == "stáhnout rychle"
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
        download_url=fast_link,
        sample_url=player_match.group(0).replace("&amp;", "&"),
    )


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
        minimum_language_probability: float = 0.65,
        request_gap_seconds: float = 2.0,
    ):
        self.session = session
        self.language_detector = language_detector
        self.max_candidates = max_candidates
        self.minimum_language_probability = minimum_language_probability
        self.request_gap_seconds = request_gap_seconds
        self._last_request = 0.0

    def _get(self, url: str) -> requests.Response:
        wait = self.request_gap_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.session.get(url, timeout=45)
        except requests.RequestException as error:
            raise SdilejError(f"Request failed for {safe_url(url)}") from error
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response

    def search(self, query: str) -> list[Candidate]:
        url = f"{BASE_URL}/{slugify(query)}/s/video-"
        return parse_search_html(self._get(url).text, query=query)

    def refresh_approved(self, candidate: Candidate) -> Candidate:
        """Refresh expiring URLs without repeating search or language analysis."""
        return parse_detail_html(self._get(candidate.url).text, candidate)

    def discover(self, film: Film) -> list[Candidate]:
        candidates: dict[str, Candidate] = {}
        for title in dict.fromkeys(
            item for item in (film.title, film.original_title) if item
        ):
            query = f"{title} {film.year}" if film.year else title
            for candidate in self.search(query):
                matched = classify_candidate(
                    film,
                    candidate.title,
                    duration_sec=candidate.duration_sec,
                )
                candidate.match_tier = matched.tier
                candidate.match_evidence = matched.evidence
                if matched.tier in (MatchTier.STRONG, MatchTier.SOLID):
                    candidates.setdefault(candidate.source_id, candidate)

        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                -item.resolution_pixels,
                -(item.size_bytes or 0),
                item.source_id,
            ),
        )
        if self.max_candidates is not None:
            ordered = ordered[: self.max_candidates]
        resolved: list[Candidate] = []
        for candidate in ordered:
            try:
                detail = parse_detail_html(self._get(candidate.url).text, candidate)
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
            except (SdilejError, LanguageDetectionError, requests.RequestException):
                continue
            detail.audio_language = language
            detail.language_probability = probability
            detail.language_evidence = (
                "whisper_multisample_filename_conflict"
                if resampled
                else "whisper_remote_sample"
            )
            if probability < self.minimum_language_probability:
                continue
            detail.language_tier = language_tier(language)
            resolved.append(detail)
            # Candidates are ordered by quality. Once Czech audio is confirmed,
            # no lower-quality result can outrank it.
            if detail.language_tier == LanguageTier.CZECH_AUDIO:
                break
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
