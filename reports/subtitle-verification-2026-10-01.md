# First live subtitle attachment verification

Verified on 2026-10-01 at approximately 14:30 UTC (16:30 Europe/Prague).

## Result

The production worker successfully attached an existing Czech text subtitle to a processed video that previously had no subtitles. No translation, transcription, OCR, or alternate source was used.

- Film: Valčík s Bašírem (2008) 1080p CZ Titulky.
- Target video ID: `29207632`.
- Target: https://prehrajto.cz/valcik-s-basirem-2008-1080p-cz-titulky-mp4/e373907a6c2ae12a
- Exact recorded source: https://sdilej.cz/29944164/valcik-s-basirem-2008-animovany-dokumentarni-historicky-valecny-zivotopisny-cztit.mkv
- Source track: stream index `0`, codec `subrip`.
- Upload: UTF-8 SRT with CRLF, 65,095 bytes.
- Durable upload intent recorded at `2026-10-01T14:26:56.471192+00:00`, with `before_ids: []`.
- Attachment confirmed by worker at `2026-10-01T14:27:11.661664+00:00`.
- New subtitle ID: `13100558`.
- Workflow: https://github.com/Olbrasoft/sdilej-to-prehrajto/actions/runs/36874253495
- Run result: success; 29 source inspections, one attachment submitted, one attachment verified.

## Independent browser verification

Playwright opened the authenticated uploaded-video listing and expanded this video's subtitle panel. The page showed `Titulky (1)`, the unique uploaded filename `cs-29207632-8c4adf6e68`, and Czech language `CZ`.

Playwright then opened the public video detail. The actual player playlist exposed one caption track labelled `CZ - 13100558 - cs-29207632…`. Fetching that player's subtitle file returned HTTP 200 and valid WebVTT containing 787 cues and preserved Czech diacritics.

After normalizing the served WebVTT back to SRT with the same cue numbering, timestamps and CRLF layout, its SHA-256 exactly matched the source-extracted uploaded SRT:

```
8c4adf6e686ae7d568aed97f42f79512ae15e4bbabca63bb81506f14b59c921e
```

This verifies the delivered caption text and timing, not merely an HTTP response or a successful workflow status. The full film was not watched for a separate human timing review. Signed CDN URLs, cookies, and subtitle dialogue are intentionally not recorded here.

A local screenshot is stored in the ignored `artifacts/subtitle-proof-2026-10-01.png` file.

## Fixes exercised by this run

- `09d1e1e`: switched slow Azure HTTP package downloads to Ubuntu HTTPS and disabled optional recommended packages.
- `6befd15`: kept subtitle HTTP requests on the same origin as authenticated login (`prehraj.to`); added a regression test and safe failure reason logging.
- Local suite: 206 passed. GitHub CI for the fixes: success.

The hourly, single-worker backfill remains enabled. Existing caption tracks are retained. Image-based Czech captions were not converted; OCR remains subject to a separate user decision.
