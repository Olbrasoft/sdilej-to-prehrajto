from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier
from sdilej_to_prehrajto.ranking import rank_candidates
from sdilej_to_prehrajto.sdilej import (
    SdilejProvider,
    audio_language_hint,
    parse_detail_html,
    parse_search_html,
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


DETAIL_HTML = """
<h1>Angelika 3 Angelika a král (1966) 4K h265 AC3 5.1 CZ.mkv</h1>
<div>Velikost 6.2 GB</div><div>Délka 01:40:05</div>
<div>Rozlišení 3840x1632</div>
<a href="https://data8.sdilej.cz/sdilej_profi.php?id=32460472&amp;session=secret">Stáhnout rychle</a>
<script>var src="https://stream2.sdilej.cz/sdilej_profi.php?id=32460472&amp;stream=1&amp;session=secret";</script>
"""


def test_search_parser_reads_quality_size_and_runtime() -> None:
    rows = parse_search_html(SEARCH_HTML, "Angelika a král 1966")
    assert len(rows) == 1
    assert rows[0].source_id == "32460472"
    assert rows[0].size_bytes == 6_200_000_000
    assert rows[0].duration_sec == 6005
    assert (rows[0].width, rows[0].height) == (3840, 2160)


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
    try:
        parse_detail_html(html, candidate)
    except Exception as error:
        assert "premium" in str(error).lower()
    else:
        raise AssertionError("non-premium link was accepted")


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


class FakeSession:
    def get(self, url: str, **_kwargs) -> FakeResponse:
        if "/s/video-" in url:
            return FakeResponse(
                """
                <div class="videobox"><a href="/10/film-4k.mkv" title="Film (2000) 4K"></a>
                <p>Délka 01:40:00 / 4K</p></div>
                <div class="videobox"><a href="/20/film-hd.mkv" title="Film (2000) 720p"></a>
                <p>Délka 01:40:00 / 720p</p></div>
                <div class="videobox"><a href="/30/film-sd.mkv" title="Film (2000) 480p"></a>
                <p>Délka 01:40:00 / 480p</p></div>
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
    provider = SdilejProvider(FakeSession(), detector, request_gap_seconds=0)
    film = Film(1, "film", "Film", None, 2000, 100, "en")
    discovered = provider.discover(film)
    assert rank_candidates(discovered)[0].language_tier == LanguageTier.CZECH_AUDIO
    assert len(detector.seen) == 2


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
            if "/s/video-" not in url:
                response.text = response.text.replace(
                    "Film (2000).mkv", "Film (2000) CZ dubbing.mkv"
                )
            return response

    provider = SdilejProvider(
        CzechNamedSession(), ConflictDetector(), max_candidates=1, request_gap_seconds=0
    )
    film = Film(1, "film", "Film", None, 2000, 100, "en")
    discovered = provider.discover(film)
    assert discovered[0].language_tier == LanguageTier.CZECH_AUDIO
    assert discovered[0].language_evidence == "whisper_multisample_filename_conflict"
