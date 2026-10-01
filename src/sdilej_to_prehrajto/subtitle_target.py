"""Non-destructive subtitle operations on the authenticated upload listing."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .subtitle_media import is_czech
from .prehrajto import BASE_URL as BASE

# Keep the authenticated origin used by login; cookies are not shared between
# the prehraj.to and prehrajto.cz aliases.
LISTING = BASE + "/profil/nahrana-videa"
PAGE_KEY = "uploadedVideoListing-visualPaginator-page"


class TargetUnavailable(RuntimeError):
    pass


@dataclass
class Target:
    video_id: str
    title: str
    ready: bool
    tracks: list[dict] = field(default_factory=list)
    action: str = ""
    page: int = 1
    detail_url: str = ""
    unknown_tracks: bool = False

    @property
    def has_czech(self):
        return any(track["czech"] for track in self.tracks)


def trusted_action(url: str, operation: str, video_id: str | None = None) -> bool:
    parsed = urlsplit(urljoin(BASE, url))
    query = parse_qs(parsed.query)
    return (parsed.scheme == "https" and parsed.hostname in {"prehrajto.cz", "prehraj.to"}
            and parsed.path == "/profil/nahrana-videa"
            and query.get("do") == [f"uploadedVideoListing-{operation}"]
            and (video_id is None or query.get("uploadedVideoListing-videoId") == [video_id]))


def parse_listing(html: str, page: int = 1) -> tuple[list[Target], int]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select('[id^="snippet-uploadedVideoListing-video-"]')
    if not blocks and not (soup.select('[id^="snippet-uploadedVideoListing"]') and "odhlásit" in soup.get_text().casefold()):
        raise TargetUnavailable("target_listing_unrecognized_or_logged_out")
    last_page = page
    for anchor in soup.select("a[href]"):
        value = parse_qs(urlsplit(anchor["href"]).query).get(PAGE_KEY, [""])[0]
        if value.isdigit():
            last_page = max(last_page, int(value))
    targets = []
    for block in blocks:
        video_id = block["id"].rsplit("-", 1)[-1]
        heading = block.find(id=f"snippet-uploadedVideoListing-videoName-{video_id}")
        if not video_id.isdigit() or heading is None:
            raise TargetUnavailable("target_row_unrecognized")
        visible_title = heading.get_text(" ", strip=True)
        title = re.sub(r"\s*\(zpracovává se\)\s*$", "", visible_title, flags=re.I)
        action = ""
        for form in block.select("form[action]"):
            video = form.select_one('input[name="video"]')
            if video and video.get("value") == video_id and form.select_one('input[name="files[]"]') and trusted_action(form["action"], "uploadSubtitles"):
                action = urljoin(BASE, form["action"])
        container = block.find(id=f"snippet-uploadedVideoListing-subtitles-{video_id}")
        tracks = []
        if container:
            for row in container.select("div.grid-x"):
                name = row.select_one("p.text-medium")
                remove = next((anchor for anchor in row.select("a[href]") if trusted_action(anchor["href"], "removeSubtitle", video_id)), None)
                if name is None or remove is None:
                    continue
                subtitle_id = parse_qs(urlsplit(remove["href"]).query).get("uploadedVideoListing-subtitleId", [""])[0]
                if not subtitle_id.isdigit():
                    continue
                label = name.get_text(" ", strip=True)
                selected = row.select_one("select.select-language option[selected]")
                # The site's unselected first option is CZ even for English
                # tracks. Never interpret that HTML default as language proof.
                czech = is_czech(label) or bool(selected and is_czech(selected.get_text(strip=True)))
                language_action = next((option.get("data-link", "") for option in row.select("option[data-link]")
                                        if is_czech(option.get_text(strip=True)) and trusted_action(option["data-link"], "changeSubtitleLanguage", video_id)
                                        and parse_qs(urlsplit(option["data-link"]).query).get("uploadedVideoListing-subtitleId") == [subtitle_id]
                                        and parse_qs(urlsplit(option["data-link"]).query).get("uploadedVideoListing-language") == ["cz"]), "")
                tracks.append({"id": subtitle_id, "name": label, "czech": czech,
                               "processing": "zpracovává" in row.get_text().casefold(),
                               "language_action": urljoin(BASE, language_action) if language_action else ""})
        count_node = block.find(id=f"snippet-uploadedVideoListing-subtitlescount-{video_id}")
        count_match = re.search(r"\((\d+)\)", count_node.get_text()) if count_node else None
        count = int(count_match[1]) if count_match else len(tracks)
        foreign = {"eng", "en", "english", "slk", "slo", "sk", "slovak", "deu", "ger", "de", "fra", "fr", "pol", "pl"}
        unknown = count != len(tracks) or bool(container and container.get_text(strip=True) and not tracks) or any(not track["czech"] and track["name"].casefold() not in foreign for track in tracks)
        detail = next((urljoin(BASE, anchor["href"]) for anchor in block.select("a[href]")
                       if re.fullmatch(r"/[^/?]+/[a-f0-9]{16,}(?:\?.*)?", anchor["href"])), "")
        targets.append(Target(video_id, title, bool(action and container is not None and "zpracovává" not in visible_title.casefold()),
                              tracks, action, page, detail, unknown))
    return targets, last_page


class SubtitleTarget:
    def __init__(self, session, interval: float = 2):
        self.session = session
        self.interval = interval
        self.last_request = 0.0

    def throttle(self):
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()

    def listing(self, page=1, search="CZ Titulky"):
        self.throttle()
        response = self.session.get(LISTING, params={PAGE_KEY: page, "searchPhrase": search}, timeout=45)
        response.raise_for_status()
        return parse_listing(response.text, page)

    def refresh(self, video_id: str, title: str) -> Target:
        # Exact ID is mandatory. A similar name or duplicate must not receive
        # the subtitle intended for this specific uploaded file.
        page, seen = 1, set()
        while page <= 20:
            targets, last = self.listing(page, title)
            signature = tuple(target.video_id for target in targets)
            if signature in seen:
                raise TargetUnavailable("target_pagination_repeated")
            seen.add(signature)
            for target in targets:
                if target.video_id == video_id:
                    return target
            if page >= last:
                break
            page += 1
        raise TargetUnavailable("target_id_not_found")

    def upload(self, target: Target, filename: str, content: bytes):
        if not target.ready or target.has_czech or target.unknown_tracks or not trusted_action(target.action, "uploadSubtitles"):
            raise TargetUnavailable("target_not_safe_to_attach")
        self.throttle()
        response = self.session.post(target.action, data={"video": target.video_id},
                                     files={"files[]": (filename, content, "application/x-subrip")},
                                     headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Origin": BASE, "Referer": LISTING},
                                     allow_redirects=False, timeout=60)
        if response.status_code != 200:
            raise TargetUnavailable("subtitle_upload_unconfirmed")
        # A 200 is not success: the caller reconciles the durable intent against
        # the real listing. Even a timeout must never trigger another POST.

    def set_own_czech_language(self, track: dict, video_id: str):
        action = track.get("language_action", "")
        if not trusted_action(action, "changeSubtitleLanguage", video_id):
            raise TargetUnavailable("subtitle_language_action_missing")
        self.throttle()
        response = self.session.get(action, headers={"X-Requested-With": "XMLHttpRequest"}, allow_redirects=False, timeout=45)
        if response.status_code != 200:
            raise TargetUnavailable("subtitle_language_unconfirmed")
