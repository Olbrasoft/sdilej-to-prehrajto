import pytest

from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier
from sdilej_to_prehrajto.ranking import rank_candidates
from sdilej_to_prehrajto.sdilej import (
    DeepScanRequired,
    PremiumRequiredError,
    SdilejError,
    SdilejProvider,
    audio_language_hint,
    parse_detail_html,
    parse_search_html,
    parse_size,
)


SEARCH_HTML = """
<div class="videobox">
  <a class="webm-hover" href="https://sdilej.cz/32460472/angelika.mkv"
     title="Angelika 3 Angelika a král (1966) 4K CZ mkv"></a>
  <p>6.2GB / Délka: 01:40:05 / 4K</p>
</div>
<div class="videobox">
  <a href="https://sdilej.cz/34064695/book.epub" title="Book epub"></a>
  <p>382KB</p>
</div>
"""


def test_search_rejects_successful_http_challenge_page(monkeypatch):
    provider = SdilejProvider(FakeSession(), None)
    monkeypatch.setattr(provider, "_get", lambda _url: FakeResponse("<h1>Checking your browser</h1>"))
    with pytest.raises(SdilejError, match="Unrecognized search response"):
        provider.search_by_quality("Film")


def test_deep_scan_does_not_treat_failed_verification_as_empty(monkeypatch):
    provider = SdilejProvider(FakeSession(), None, allow_unresolved_fallback=True, request_gap_seconds=0)
    def fail(*_args):
        raise LanguageDetectionError("sample unavailable")
    monkeypatch.setattr(provider, "_verify_candidate", fail)
    with pytest.raises(SdilejError, match="verification failed"):
        provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))


DETAIL_HTML = """
<h1>Angelika 3 Angelika a král (1966) 4K h265 AC3 5.1 CZ.mkv</h1>
<div>Velikost 6.2 GB</div><div>Délka 01:40:05</div>
<div>Rozlišení 3840x1632</div>
<a href="https://data8.sdilej.cz/sdilej_profi.php?id=32460472&amp;session=secret">Stáhnout<span>rychle</span></a>
<script>var src="https://stream2.sdilej.cz/sdilej_profi.php?id=32460472&amp;stream=1&amp;session=secret";</script>
"""


def test_search_parser_reads_quality_size_and_runtime() -> None:
    rows = parse_search_html(SEARCH_HTML, "Angelika a král 1966")
    assert len(rows) == 1
    assert rows[0].source_id == "32460472"
    assert rows[0].size_bytes == 6_200_000_000
    assert rows[0].duration_sec == 6005
    assert (rows[0].width, rows[0].height) == (3840, 2160)


def test_size_parser_does_not_treat_gbr_country_code_as_gigabytes() -> None:
    page_text = "Network (1976 GBR) Velikost 1.8 GB"

    assert parse_size(page_text) == 1_800_000_000


def test_detail_parser_uses_original_metadata_and_fast_link() -> None:
    candidate = Candidate("32460472", "https://sdilej.cz/32460472/x", "x")
    result = parse_detail_html(DETAIL_HTML, candidate)
    assert (result.width, result.height) == (3840, 1632)
    assert result.duration_sec == 6005
    assert result.filename.endswith("CZ.mkv")
    assert "sdilej_profi.php" in result.download_url
    assert "stream=1" in result.sample_url


def test_detail_parser_rejects_nonpremium_button() -> None:
    candidate = Candidate("1", "https://sdilej.cz/1/x", "x")
    html = '<h1>x.mkv</h1><a href="/cenik">Stáhnout rychle</a>'

    with pytest.raises(PremiumRequiredError, match="premium"):
        parse_detail_html(html, candidate)


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


class FakeSession:
    def __init__(self):
        self.seen_urls = []

    def get(self, url: str, **_kwargs) -> FakeResponse:
        self.seen_urls.append(url)
        if "/s/" in url:
            return FakeResponse(
                """
                <div class="videobox"><a href="/10/film-4k.mkv" title="Film (2000) 4K"></a>
                <p>15GB / Délka 01:40:00 / 4K</p></div>
                <div class="videobox"><a href="/20/film-hd.mkv" title="Film (2000) 720p"></a>
                <p>3GB / Délka 01:40:00 / 720p</p></div>
                <div class="videobox"><a href="/30/film-sd.mkv" title="Film (2000) 480p"></a>
                <p>1GB / Délka 01:40:00 / 480p</p></div>
                """
            )
        source_id = url.split("/")[3]
        width = {"10": "3840x2160", "20": "1280x720", "30": "854x480"}[source_id]
        return FakeResponse(
            f"""
            <h1>Film (2000).mkv</h1><div>Délka 01:40:00</div><div>{width}</div>
            <a href="https://data.sdilej.cz/sdilej_profi.php?id={source_id}">Stáhnout rychle</a>
            <script>https://stream2.sdilej.cz/sdilej_profi.php?id={source_id}&amp;stream=1</script>
            """
        )


class FakeDetector:
    def __init__(self):
        self.seen: list[str] = []

    def detect(self, url: str) -> tuple[str, float]:
        self.seen.append(url)
        return ("cs", 0.99) if "id=20" in url else ("en", 0.99)


def test_discovery_does_not_drop_lower_quality_czech_audio() -> None:
    detector = FakeDetector()
    provider = SdilejProvider(
        FakeSession(), detector, request_gap_seconds=0, media_probe=lambda _url: {}
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")
    discovered = provider.discover(film)
    assert rank_candidates(discovered)[0].language_tier == LanguageTier.CZECH_AUDIO
    assert len(detector.seen) == 2


def test_discovery_stops_at_4k_when_czech_audio_is_verified() -> None:
    class AlwaysCzechDetector(FakeDetector):
        def detect(self, url: str) -> tuple[str, float]:
            self.seen.append(url)
            return "cs", 0.99

    detector = AlwaysCzechDetector()
    provider = SdilejProvider(
        FakeSession(), detector, request_gap_seconds=0, media_probe=lambda _url: {}
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")

    discovered = provider.discover(film)

    assert len(discovered) == 1
    assert (discovered[0].width, discovered[0].height) == (3840, 2160)
    assert discovered[0].language_tier == LanguageTier.CZECH_AUDIO
    assert len(detector.seen) == 1


def test_search_uses_exact_sdilej_quality_filter_urls() -> None:
    session = FakeSession()
    provider = SdilejProvider(
        session,
        FakeDetector(),
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    provider.search("Film 2000", "4k")
    provider.search("Film 2000", "1080")
    provider.search_by_quality("Film 2000")

    assert session.seen_urls == [
        "https://sdilej.cz/film-2000/s/--4k",
        "https://sdilej.cz/film-2000/s/--1080",
        "https://sdilej.cz/film-2000/s/-6",
    ]


def test_discovery_deadline_defers_film_without_searching() -> None:
    session = FakeSession()
    provider = SdilejProvider(
        session,
        FakeDetector(),
        discovery_timeout_seconds=0,
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    with pytest.raises(SdilejError, match="deadline"):
        provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))
    assert session.seen_urls == []


def test_discovery_deadline_during_verification_is_retryable(monkeypatch) -> None:
    provider = SdilejProvider(
        FakeSession(),
        FakeDetector(),
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )
    expired = False

    def fail_verification(_film, _candidate):
        nonlocal expired
        expired = True
        raise LanguageDetectionError("temporary sample failure")

    monkeypatch.setattr(provider, "_verify_candidate", fail_verification)
    monkeypatch.setattr(
        "sdilej_to_prehrajto.sdilej.time.monotonic",
        lambda: 301.0 if expired else 0.0,
    )

    with pytest.raises(SdilejError, match="verification completed"):
        provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))


def test_audio_hint_distinguishes_dubbing_from_subtitles() -> None:
    assert audio_language_hint("Schindleruv seznam 4K CZ.mkv") == "cs"
    assert audio_language_hint("Schindleruv seznam CZ dubbing.mkv") == "cs"
    assert audio_language_hint("Schindleruv seznam CZ titulky.mkv") is None


class ConflictDetector:
    def detect(self, _url: str) -> tuple[str, float]:
        return "pl", 0.68

    def detect_consensus(self, _url, _duration, **kwargs) -> tuple[str, float]:
        assert kwargs["preferred_language"] == "cs"
        return "cs", 0.88


def test_discovery_resamples_explicit_czech_source_on_whisper_conflict() -> None:
    class CzechNamedSession(FakeSession):
        def get(self, url: str, **kwargs) -> FakeResponse:
            response = super().get(url, **kwargs)
            if "/s/" not in url:
                response.text = response.text.replace(
                    "Film (2000).mkv", "Film (2000) CZ dubbing.mkv"
                )
            return response

    provider = SdilejProvider(
        CzechNamedSession(),
        ConflictDetector(),
        max_candidates=1,
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")
    discovered = provider.discover(film)
    assert discovered[0].language_tier == LanguageTier.CZECH_AUDIO
    assert discovered[0].language_evidence == "whisper_multisample_filename_conflict"


def test_discovery_uses_probed_original_media_metadata() -> None:
    seen_urls: list[str | None] = []

    def probe(url: str | None) -> dict[str, object]:
        seen_urls.append(url)
        return {
            "video_codec": "h265",
            "width": 3840,
            "height": 1600,
            "duration_sec": 6997,
        }

    provider = SdilejProvider(
        FakeSession(),
        type(
            "AlwaysCzechDetector",
            (),
            {"detect": lambda self, _url: ("cs", 0.99)},
        )(),
        max_candidates=1,
        request_gap_seconds=0,
        media_probe=probe,
    )
    discovered = provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))

    assert len(discovered) == 1
    assert seen_urls == ["https://data.sdilej.cz/sdilej_profi.php?id=10"]
    assert discovered[0].video_codec == "h265"
    assert (discovered[0].width, discovered[0].height) == (3840, 1600)
    assert discovered[0].duration_sec == 6997


def test_discovery_searches_title_with_and_without_year() -> None:
    class YearlessResultSession:
        def __init__(self) -> None:
            self.seen_urls: list[str] = []

        def get(self, url: str, **_kwargs) -> FakeResponse:
            self.seen_urls.append(url)
            if "/film-2000/s/" in url:
                return FakeResponse("Na tvůj dotaz jsme nic nenašli.")
            if "/film/s/-6" in url:
                return FakeResponse(
                    '<div class="videobox"><a href="/10/film-4k-cz.mkv" '
                    'title="Film 4K CZ"></a>'
                    '<p>6GB / Délka 01:40:00 / 4K</p></div>'
                )
            return FakeResponse(
                '<h1>Film 4K CZ.mkv</h1><div>Délka 01:40:00</div>'
                '<div>3840x1600</div>'
                '<a href="https://data.sdilej.cz/sdilej_profi.php?id=10">Stáhnout rychle</a>'
                '<script>https://stream2.sdilej.cz/sdilej_profi.php?id=10&amp;stream=1</script>'
            )

    class AlwaysCzechDetector:
        def detect(self, _url: str) -> tuple[str, float]:
            return "cs", 0.99

    session = YearlessResultSession()
    provider = SdilejProvider(
        session,
        AlwaysCzechDetector(),
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    discovered = provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))

    assert discovered[0].source_id == "10"
    assert "https://sdilej.cz/film-2000/s/-6" in session.seen_urls
    assert "https://sdilej.cz/film/s/-6" in session.seen_urls


def test_discovery_does_not_fall_back_when_4k_verification_is_transient() -> None:
    class QualityAwareSession:
        def get(self, url: str, **_kwargs) -> FakeResponse:
            if "/s/-6" in url:
                return FakeResponse(
                    '<div class="videobox"><a href="/10/film-4k.mkv" '
                    'title="Film (2000) 4K CZ"></a>'
                    '<p>6GB / Délka 01:40:00 / 4K</p></div>'
                    '<div class="videobox"><a href="/20/film-1080.mkv" '
                    'title="Film (2000) 1080p CZ"></a>'
                    '<p>4GB / Délka 01:40:00 / 1080p</p></div>'
                )
            if "/10/" in url:
                raise LanguageDetectionError("temporary 4K sampling failure")
            return FakeResponse(
                '<h1>Film (2000) 1080p CZ.mkv</h1><div>Délka 01:40:00</div>'
                '<div>1920x1080</div>'
                '<a href="https://data.sdilej.cz/sdilej_profi.php?id=20">Stáhnout rychle</a>'
                '<script>https://stream2.sdilej.cz/sdilej_profi.php?id=20&amp;stream=1</script>'
            )

    provider = SdilejProvider(
        QualityAwareSession(),
        FakeDetector(),
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    with pytest.raises(SdilejError, match="could not be fully verified"):
        provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))


def test_deep_discovery_uses_verified_fallback_after_4k_failure() -> None:
    class QualityAwareSession:
        def get(self, url: str, **_kwargs) -> FakeResponse:
            if "/s/-6" in url:
                return FakeResponse(
                    '<div class="videobox"><a href="/10/film-4k.mkv" '
                    'title="Film (2000) 4K CZ"></a>'
                    '<p>6GB / Délka 01:40:00 / 4K</p></div>'
                    '<div class="videobox"><a href="/20/film-1080.mkv" '
                    'title="Film (2000) 1080p CZ"></a>'
                    '<p>4GB / Délka 01:40:00 / 1080p</p></div>'
                )
            if "/10/" in url:
                raise LanguageDetectionError("unavailable 4K source")
            return FakeResponse(
                '<h1>Film (2000) 1080p CZ.mkv</h1><div>Délka 01:40:00</div>'
                '<div>1920x1080</div>'
                '<a href="https://data.sdilej.cz/sdilej_profi.php?id=20">Stáhnout rychle</a>'
                '<script>https://stream2.sdilej.cz/sdilej_profi.php?id=20&amp;stream=1</script>'
            )

    provider = SdilejProvider(
        QualityAwareSession(),
        FakeDetector(),
        allow_unresolved_fallback=True,
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    discovered = provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))

    assert [candidate.source_id for candidate in discovered] == ["20"]


def test_discovery_does_not_choose_larger_same_tier_after_smaller_failure() -> None:
    class SameTierSession:
        def get(self, url: str, **_kwargs) -> FakeResponse:
            if "/s/-6" in url:
                return FakeResponse(
                    '<div class="videobox"><a href="/10/small-4k.mkv" '
                    'title="Film (2000) 4K CZ"></a>'
                    '<p>6GB / Délka 01:40:00 / 4K</p></div>'
                    '<div class="videobox"><a href="/20/large-4k.mkv" '
                    'title="Film (2000) 4K CZ"></a>'
                    '<p>12GB / Délka 01:40:00 / 4K</p></div>'
                )
            if "/10/" in url:
                raise LanguageDetectionError("temporary smaller-source failure")
            return FakeResponse(
                '<h1>Film (2000) 4K CZ.mkv</h1><div>Délka 01:40:00</div>'
                '<div>3840x1600</div>'
                '<a href="https://data.sdilej.cz/sdilej_profi.php?id=20">Stáhnout rychle</a>'
                '<script>https://stream2.sdilej.cz/sdilej_profi.php?id=20&amp;stream=1</script>'
            )

    class AlwaysCzechDetector:
        def detect(self, _url: str) -> tuple[str, float]:
            return "cs", 0.99

    provider = SdilejProvider(
        SameTierSession(),
        AlwaysCzechDetector(),
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    with pytest.raises(SdilejError, match="could not be fully verified"):
        provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))


def test_fast_discovery_delegates_after_candidate_budget() -> None:
    provider = SdilejProvider(
        FakeSession(),
        FakeDetector(),
        max_candidates=1,
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    with pytest.raises(DeepScanRequired, match="Candidate limit"):
        provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))


def test_discovery_ignores_mislabeled_4k_until_real_4k_is_verified() -> None:
    class MislabeledQualitySession:
        def get(self, url: str, **_kwargs) -> FakeResponse:
            if "/s/-6" in url:
                return FakeResponse(
                    '<div class="videobox"><a href="/10/small-fake-4k.mkv" '
                    'title="Film (2000) 4K CZ"></a>'
                    '<p>3GB / Délka 01:40:00 / 4K</p></div>'
                    '<div class="videobox"><a href="/20/real-4k.mkv" '
                    'title="Film (2000) 4K CZ"></a>'
                    '<p>6GB / Délka 01:40:00 / 4K</p></div>'
                )
            if "/s/" in url:
                return FakeResponse("")
            source_id = url.split("/")[3]
            dimensions = "1920x1080" if source_id == "10" else "3840x1600"
            return FakeResponse(
                f'<h1>Film (2000) CZ.mkv</h1><div>Délka 01:40:00</div>'
                f'<div>{dimensions}</div>'
                f'<a href="https://data.sdilej.cz/sdilej_profi.php?id={source_id}">Stáhnout rychle</a>'
                f'<script>https://stream2.sdilej.cz/sdilej_profi.php?id={source_id}&amp;stream=1</script>'
            )

    class AlwaysCzechDetector:
        def detect(self, _url: str) -> tuple[str, float]:
            return "cs", 0.99

    provider = SdilejProvider(
        MislabeledQualitySession(),
        AlwaysCzechDetector(),
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    discovered = provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))

    assert rank_candidates(discovered)[0].source_id == "20"
    assert (rank_candidates(discovered)[0].width, rank_candidates(discovered)[0].height) == (
        3840,
        1600,
    )


def test_discovery_retries_transient_language_failure_for_best_source() -> None:
    class FlakyDetector:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _url: str) -> tuple[str, float]:
            self.calls += 1
            if self.calls == 1:
                raise LanguageDetectionError("temporary sample failure")
            return "cs", 0.99

    detector = FlakyDetector()
    provider = SdilejProvider(
        FakeSession(),
        detector,
        request_gap_seconds=0,
        media_probe=lambda _url: {},
    )

    discovered = provider.discover(Film(1, "film", "Film", None, 2000, 100, "en"))

    assert detector.calls == 2
    assert len(discovered) == 1
    assert (discovered[0].width, discovered[0].height) == (3840, 2160)
