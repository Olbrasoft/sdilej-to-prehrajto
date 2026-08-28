from sdilej_to_prehrajto.pipeline import plan_sha


def row(probability: float) -> dict:
    return {
        "film": {"cr_film_id": 1},
        "selected": {
            "source_id": "10",
            "width": 3840,
            "height": 2160,
            "audio_language": "cs",
            "language_tier": "czech_audio",
            "language_probability": probability,
        },
        "display_name": "Film (2000) 4K CZ Dabing",
    }


def test_plan_approval_digest_ignores_nondeterministic_probability() -> None:
    assert plan_sha([row(0.91)]) == plan_sha([row(0.99)])
