from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests
from bs4 import BeautifulSoup
from requests_toolbelt.multipart.encoder import MultipartEncoder

from .models import Candidate


BASE_URL = "https://prehraj.to"
UPLOAD_URL = "https://api.premiumcdn.net/upload/"
EXPECTED_EMAIL = "sdilej.prehrajto@seznam.cz"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


class PrehrajtoError(RuntimeError):
    def __init__(self, message: str, *, target_video_id: str | None = None):
        super().__init__(message)
        self.target_video_id = target_video_id


def login(
    email: str,
    password: str,
    *,
    session: requests.Session | None = None,
) -> requests.Session:
    if email.strip().lower() != EXPECTED_EMAIL:
        raise PrehrajtoError(f"Refusing target account other than {EXPECTED_EMAIL}")
    if not password:
        raise PrehrajtoError("PREHRAJTO_PASSWORD is required")
    client = session or requests.Session()
    client.headers["User-Agent"] = USER_AGENT
    prime = client.get(BASE_URL + "/", timeout=30)
    prime.raise_for_status()
    response = client.post(
        BASE_URL + "/?frm=homepageLoginForm-loginForm",
        files={
            "email": (None, email),
            "password": (None, password),
            "_do": (None, "homepageLoginForm-loginForm-submit"),
            "login": (None, "Přihlásit se"),
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": BASE_URL + "/",
        },
        allow_redirects=False,
        timeout=30,
    )
    response.raise_for_status()
    check = client.get(BASE_URL + "/profil", allow_redirects=False, timeout=30)
    if check.status_code != 200:
        raise PrehrajtoError("Target account login verification failed")
    return client


class RemoteReader:
    """Bounded-memory file-like reader used by MultipartEncoder."""

    def __init__(
        self,
        response: requests.Response,
        total: int,
        *,
        progress_interval: int = 256 * 1024 * 1024,
    ):
        self.response = response
        self.total = total
        self.position = 0
        self.started_at = time.monotonic()
        self.last_progress_at = self.started_at
        self.progress_interval = progress_interval
        self._next_progress = progress_interval

    @property
    def len(self) -> int:
        return max(0, self.total - self.position)

    def read(self, size: int = -1) -> bytes:
        if self.position >= self.total:
            return b""
        if size is None or size < 0:
            size = min(1024 * 1024, self.total - self.position)
        else:
            size = min(size, self.total - self.position)
        data = self.response.raw.read(size)
        if not data and self.position < self.total:
            raise PrehrajtoError(
                f"Source ended after {self.position} of {self.total} bytes"
            )
        self.position += len(data)
        if data:
            self.last_progress_at = time.monotonic()
        if self.position >= self._next_progress or self.position == self.total:
            elapsed = max(time.monotonic() - self.started_at, 0.001)
            speed_mib = self.position / elapsed / 1024 / 1024
            percent = self.position * 100 / self.total
            print(
                f"relay_progress={self.position}/{self.total} "
                f"percent={percent:.1f} speed_mib_s={speed_mib:.2f}",
                flush=True,
            )
            self._next_progress = self.position + self.progress_interval
        return data


def response_total_size(response: requests.Response) -> int:
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[1]
        if total.isdigit():
            return int(total)
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    raise PrehrajtoError("Source did not provide an exact content length")


def prepare_video(
    session: requests.Session,
    *,
    upload_name: str,
    size: int,
    mime_type: str,
    description: str,
) -> tuple[str, dict]:
    session.get(BASE_URL + "/profil/nahrat-soubor", timeout=30).raise_for_status()
    response = session.post(
        BASE_URL + "/profil/nahrat-soubor?do=prepareVideo",
        headers={"X-Requested-With": "XMLHttpRequest"},
        data={
            "description": description,
            "name": upload_name,
            "size": str(size),
            "type": mime_type,
            "erotic": "false",
            "folder": "",
            "private": "false",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    video_id = str(json.loads(payload["params"])["video_id"])
    return video_id, payload


def rename_video(session: requests.Session, video_id: str, name: str) -> None:
    response = session.post(
        BASE_URL
        + f"/profil/nahrana-videa?uploadedVideoListing-videoId={video_id}"
        "&do=uploadedVideoListing-changeVideoName",
        data={"uploadedVideoListing-name": name},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    response.raise_for_status()


def uploaded_video_confirmed(
    session: requests.Session,
    video_id: str,
    display_name: str,
) -> bool:
    response = session.get(
        BASE_URL + "/profil/nahrana-videa",
        params={"searchPhrase": display_name},
        timeout=30,
    )
    response.raise_for_status()
    return (
        _uploaded_video_id_from_html(
            response.text,
            display_name,
            include_processing=False,
            allow_filename_extension=False,
        )
        == video_id
    )


PROCESSING_SUFFIX_RE = re.compile(r"\s*\(zpracovává se\)\s*$", re.I)


def _visible_uploaded_name(element) -> str:
    return (element.get("value", "") or element.get_text(" ", strip=True)).strip()


def _normalized_uploaded_name(element) -> str:
    visible_name = PROCESSING_SUFFIX_RE.sub("", _visible_uploaded_name(element))
    return re.sub(r"\.[a-z0-9]{1,8}$", "", visible_name).casefold().strip()


def _uploaded_video_id_from_html(
    html_text: str,
    display_name: str,
    *,
    include_processing: bool = True,
    allow_filename_extension: bool = True,
) -> str | None:
    """Find an exact uploaded name and ID belonging to the same listing row."""
    wanted = display_name.casefold().strip()
    soup = BeautifulSoup(html_text, "html.parser")
    named_elements = soup.find_all(["h1", "h2", "h3", "input"])
    for element in named_elements:
        visible_name = PROCESSING_SUFFIX_RE.sub(
            "", _visible_uploaded_name(element)
        ).casefold().strip()
        compared_name = (
            _normalized_uploaded_name(element)
            if allow_filename_extension
            else visible_name
        )
        if compared_name != wanted:
            continue
        if not include_processing and PROCESSING_SUFFIX_RE.search(
            _visible_uploaded_name(element)
        ):
            continue
        node = element
        for _level in range(8):
            if node is None:
                break
            # Do not climb into a shared listing container and accidentally
            # associate this title with an ID from a different video row.
            other_names = {
                _normalized_uploaded_name(item)
                for item in node.find_all(["h1", "h2", "h3", "input"])
                if item is not element and _normalized_uploaded_name(item)
            }
            if other_names - {wanted}:
                break
            match = re.search(r"(?:videoId|video-id)[=/\"':-]+(\d+)", str(node), re.I)
            if match:
                return match.group(1)
            node = node.parent
    return None


def uploaded_video_id_by_name(session: requests.Session, display_name: str) -> str | None:
    """Return an existing target ID only when its listing row has the exact name."""
    response = session.get(
        BASE_URL + "/profil/nahrana-videa",
        params={"searchPhrase": display_name},
        timeout=30,
    )
    response.raise_for_status()
    return _uploaded_video_id_from_html(response.text, display_name)


def uploaded_video_count(session: requests.Session) -> int | None:
    response = session.get(BASE_URL + "/profil/statistiky", timeout=30)
    response.raise_for_status()
    text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
    match = re.search(r"Nahráno videí celkem\s+(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def upload_confirmed_after_baseline(
    session: requests.Session,
    video_id: str,
    display_name: str,
    baseline_count: int | None,
) -> bool:
    current_count = uploaded_video_count(session)
    return (
        baseline_count is not None
        and current_count is not None
        and current_count >= baseline_count + 1
        and uploaded_video_confirmed(session, video_id, display_name)
    )


def upload_present_after_baseline(
    session: requests.Session,
    video_id: str,
    display_name: str,
    baseline_count: int | None,
) -> bool:
    """Return whether the target accepted the upload, even if still processing."""
    current_count = uploaded_video_count(session)
    return (
        baseline_count is not None
        and current_count is not None
        and current_count >= baseline_count + 1
        and uploaded_video_id_by_name(session, display_name) == video_id
    )


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    size_bytes: int
    source_bytes_read: int
    completed: bool = True


def relay_upload(
    target_session: requests.Session,
    source_session: requests.Session,
    candidate: Candidate,
    display_name: str,
    description: str = "",
    *,
    on_prepared: Callable[[str, int], None] | None = None,
    upload_requester: Callable[..., requests.Response] | None = None,
    stall_timeout_seconds: float = 300,
    monitor_interval_seconds: float = 10,
    confirmation_timeout_seconds: float = 300,
    confirmation_interval_seconds: float = 5,
) -> UploadResult:
    if not candidate.download_url:
        raise PrehrajtoError("Candidate has no authenticated download URL")
    extension = ""
    if candidate.filename and "." in candidate.filename:
        extension = "." + candidate.filename.rsplit(".", 1)[1].lower()
    if not extension:
        extension = mimetypes.guess_extension(candidate.mime_type or "") or ".bin"
    upload_name = display_name + extension
    mime_type = candidate.mime_type or mimetypes.guess_type(upload_name)[0]
    mime_type = mime_type or "application/octet-stream"

    baseline_count = uploaded_video_count(target_session)
    source_response = source_session.get(
        candidate.download_url,
        headers={
            "Accept-Encoding": "identity",
            "Range": "bytes=0-",
            "Referer": candidate.url,
            "User-Agent": USER_AGENT,
        },
        stream=True,
        allow_redirects=True,
        timeout=(30, 300),
    )
    try:
        if source_response.status_code not in (200, 206):
            raise PrehrajtoError(
                f"Source download returned HTTP {source_response.status_code}"
            )
        size = response_total_size(source_response)
        video_id, prepared = prepare_video(
            target_session,
            upload_name=upload_name,
            size=size,
            mime_type=mime_type,
            description=description,
        )
        if on_prepared:
            on_prepared(video_id, size)

        reader = RemoteReader(source_response, size)
        encoder = MultipartEncoder(
            fields=[
                ("files", (upload_name, reader, mime_type)),
                ("response", prepared["response"]),
                ("project", prepared["project"]),
                ("nonce", prepared["nonce"]),
                ("params", prepared["params"]),
                ("signature", prepared["signature"]),
            ]
        )
        upload_result: dict[str, object] = {}
        upload_done = threading.Event()

        def send_upload() -> None:
            try:
                requester = upload_requester or requests.post
                upload_result["response"] = requester(
                    UPLOAD_URL,
                    data=encoder,
                    headers={
                        "Content-Type": encoder.content_type,
                        "Content-Length": str(encoder.len),
                        "Referer": BASE_URL + "/",
                        "Origin": BASE_URL,
                        "User-Agent": USER_AGENT,
                    },
                    timeout=(30, 7200),
                )
            except BaseException as error:
                upload_result["error"] = error
            finally:
                upload_done.set()

        threading.Thread(target=send_upload, daemon=True).start()
        response = None
        while True:
            if upload_done.wait(monitor_interval_seconds):
                if "error" in upload_result:
                    raise PrehrajtoError(
                        "Target upload request failed", target_video_id=video_id
                    ) from upload_result["error"]
                response = upload_result["response"]
                break
            if reader.position < size:
                if time.monotonic() - reader.last_progress_at >= stall_timeout_seconds:
                    raise PrehrajtoError(
                        "Relay upload made no progress before timeout",
                        target_video_id=video_id,
                    )
                continue
            if upload_present_after_baseline(
                target_session, video_id, display_name, baseline_count
            ):
                # The API can finish ingesting and publish the video before
                # its upload request returns. Preserve that recovery path, but
                # still apply the final extension-free display name.
                rename_video(target_session, video_id, display_name)
                print(
                    "upload_confirmed=statistics_and_uploaded_listing",
                    flush=True,
                )
                completed = uploaded_video_confirmed(
                    target_session, video_id, display_name
                )
                return UploadResult(
                    video_id,
                    size,
                    reader.position,
                    completed=completed,
                )
            if time.monotonic() - reader.last_progress_at >= stall_timeout_seconds:
                raise PrehrajtoError(
                    "Target did not confirm completed relay before timeout",
                    target_video_id=video_id,
                )

        assert isinstance(response, requests.Response) or hasattr(
            response, "status_code"
        )
        if response.status_code not in (200, 201):
            raise PrehrajtoError(
                f"Target upload returned HTTP {response.status_code}",
                target_video_id=video_id,
            )
        if reader.position != size:
            raise PrehrajtoError(
                f"Source ended after {reader.position} of {size} bytes",
                target_video_id=video_id,
            )
        rename_video(target_session, video_id, display_name)
        confirmation_deadline = time.monotonic() + confirmation_timeout_seconds
        while not upload_confirmed_after_baseline(
            target_session, video_id, display_name, baseline_count
        ):
            if upload_present_after_baseline(
                target_session, video_id, display_name, baseline_count
            ):
                print("upload_pending=target_processing", flush=True)
                return UploadResult(
                    video_id,
                    size,
                    reader.position,
                    completed=False,
                )
            if time.monotonic() >= confirmation_deadline:
                raise PrehrajtoError(
                    "Target statistics did not confirm completed relay",
                    target_video_id=video_id,
                )
            time.sleep(confirmation_interval_seconds)
        print("upload_confirmed=statistics_and_uploaded_listing", flush=True)
        return UploadResult(video_id, size, reader.position)
    finally:
        source_response.close()
