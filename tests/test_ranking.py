from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import (
    display_name,
    infer_video_codec,
    quality_acceptable,
    rank_candidates,
    resolution_label,
)


def candidate(language: LanguageTier, width: int) -> Candidate:
    return Candidate(
        source_id=f"{language}-{width}",
        url="https://sdilej.cz/1/x",
        title="Film",
        size_bytes=20_000_000_000,
        duration_sec=6000,
        width=width,
        height=width * 9 // 16,
        language_tier=language,
        match_tier=MatchTier.STRONG,
    )


def film(original_language: str = "en") -> Film:
    return Film(1, "film", "Film", None, 2000, 100, original_language)


def test_language_outweighs_resolution() -> None:
    czech_720 = candidate(LanguageTier.CZECH_AUDIO, 1280)
    foreign_4k = candidate(LanguageTier.FOREIGN_AUDIO, 3840)
    assert rank_candidates([foreign_4k, czech_720])[0] is czech_720


def test_resolution_breaks_tie_inside_language() -> None:
    czech_720 = candidate(LanguageTier.CZECH_AUDIO, 1280)
    czech_4k = candidate(LanguageTier.CZECH_AUDIO, 3840)
    assert rank_candidates([czech_720, czech_4k])[0] is czech_4k


def test_smallest_candidate_above_quality_floor_wins() -> None:
    oversized = candidate(LanguageTier.FOREIGN_AUDIO, 1920)
    oversized.source_id = "oversized"
    oversized.size_bytes = 17_500_000_000
    balanced = candidate(LanguageTier.FOREIGN_AUDIO, 1920)
    balanced.source_id = "balanced"
    balanced.size_bytes = 5_000_000_000
    overcompressed = candidate(LanguageTier.FOREIGN_AUDIO, 1920)
    overcompressed.source_id = "overcompressed"
    overcompressed.size_bytes = 2_900_000_000
    oversized.duration_sec = balanced.duration_sec = overcompressed.duration_sec = 8627

    ranked = rank_candidates([oversized, balanced, overcompressed])

    assert quality_acceptable(overcompressed) is False
    assert ranked == [balanced, oversized]


def test_efficient_codec_allows_lower_bitrate() -> None:
    efficient = candidate(LanguageTier.CZECH_AUDIO, 1920)
    efficient.size_bytes = 2_900_000_000
    efficient.duration_sec = 8627
    efficient.video_codec = "h265"

    assert quality_acceptable(efficient) is True


def test_compact_1080p_senna_source_meets_quality_floor() -> None:
    senna = candidate(LanguageTier.CZECH_AUDIO, 1920)
    senna.size_bytes = 2_200_000_000
    senna.duration_sec = 6097
    senna.video_codec = "h264"

    assert 2.8 < (senna.average_bitrate_mbps or 0) < 3.0
    assert quality_acceptable(senna) is True


def test_unknown_4k_codec_uses_compact_floor_and_beats_1080p() -> None:
    compact_4k = candidate(LanguageTier.CZECH_AUDIO, 3840)
    compact_4k.source_id = "compact-4k"
    compact_4k.size_bytes = 9_259_169_705
    compact_4k.duration_sec = 8554
    compact_4k.video_codec = None
    large_1080 = candidate(LanguageTier.CZECH_AUDIO, 1920)
    large_1080.source_id = "large-1080"
    large_1080.size_bytes = 12_400_000_000
    large_1080.duration_sec = 8554

    ranked = rank_candidates([large_1080, compact_4k])

    assert quality_acceptable(compact_4k) is True
    assert ranked[0] is compact_4k


def test_compact_hevc_cropped_4k_source_meets_quality_floor() -> None:
    compact_4k = candidate(LanguageTier.CZECH_AUDIO, 3840)
    compact_4k.size_bytes = 4_900_000_000
    compact_4k.duration_sec = 6997
    compact_4k.height = 1600
    compact_4k.video_codec = "h265"

    assert 5.5 < (compact_4k.average_bitrate_mbps or 0) < 5.7
    assert quality_acceptable(compact_4k) is True


def test_codec_inference_accepts_common_punctuation() -> None:
    assert infer_video_codec("Film.2160p.H.265.mkv") == "h265"
    assert infer_video_codec("Film 4K HEVC.mkv") == "h265"
    assert infer_video_codec("Film H.264.mp4") == "h264"


def test_display_names_include_verified_resolution_and_language() -> None:
    assert display_name(film("cs"), candidate(LanguageTier.CZECH_AUDIO, 3840)) == "Film (2000) 4K"
    assert display_name(film("en"), candidate(LanguageTier.CZECH_AUDIO, 1920)) == "Film (2000) 1080p CZ Dabing"
    assert display_name(film("en"), candidate(LanguageTier.FOREIGN_AUDIO, 1920)) == "Film (2000) 1080p CZ Titulky"


def test_resolution_label_uses_height_for_cropped_1080p_video() -> None:
    assert resolution_label(1808, 1080) == "1080p"
