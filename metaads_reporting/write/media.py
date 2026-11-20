"""Uploading an image, and reusing a video.

An ad does not carry a file. It carries a creative, and a creative carries an
``image_hash`` or a ``video_id`` that Meta already holds. So "change the image"
is two steps: put the file in the account's media library, then build a
creative that references it.

VIDEO IS ASYNCHRONOUS. The ``advideos`` edge returns an id for a video Meta
has not finished processing, and an ad built on it is rejected until
``status.video_status`` reaches ``ready``. So :func:`upload_video` uses Meta's
resumable (chunked) upload, :func:`wait_until_ready` polls for a bounded time,
and :func:`check_video_usable` refuses a video that is not ready yet.

Nothing here is reachable from the MCP server except through a preview/apply
pair, and the upload happens at APPLY time because it is a write.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..account_id import account_path
from .client import MutatingMetaAdsClient, MutationError

IMAGE_EDGE = "adimages"
VIDEO_EDGE = "advideos"

#: Formats Meta accepts for an ad image, by their magic bytes.
#:
#: Sniffed here rather than with `imghdr`, which was deprecated in Python 3.11
#: and REMOVED in 3.13 -- the interpreter this repo runs on. Pillow would do it
#: too, but a whole imaging dependency to read eight bytes is not a trade worth
#: making, and an explicit table says exactly which formats are accepted.
IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)

ALLOWED_IMAGE_TYPES = frozenset(kind for _, kind in IMAGE_MAGIC)


def sniff_image_type(head: bytes) -> str | None:
    r"""The image format from its first bytes, or None if it is not one.

    Raw docstring on purpose: the examples contain escape sequences, and in a
    normal docstring Python resolves them at parse time, so the doctest ends up
    running against literal bytes rather than the text written here.

    >>> sniff_image_type(b"\xff\xd8\xff\xe0 and more")
    'jpeg'
    >>> sniff_image_type(b"GIF89a...")
    'gif'
    >>> sniff_image_type(b"not an image") is None
    True
    """
    for magic, kind in IMAGE_MAGIC:
        if head.startswith(magic):
            return kind
    return None

#: Meta's documented ceiling for an ad image. Checked here so a 40MB photo
#: fails locally in a tenth of a second rather than after a long upload.
MAX_IMAGE_BYTES = 30 * 1024 * 1024

#: Below this, an image is almost certainly a thumbnail or a placeholder that
#: will look terrible in-feed. A warning, not a refusal.
SMALL_IMAGE_BYTES = 20 * 1024


@dataclass(frozen=True)
class LocalImage:
    """A file on disk, checked but not yet uploaded."""

    path: Path
    size_bytes: int
    image_type: str

    @property
    def warnings(self) -> list[str]:
        notes = []
        if self.size_bytes < SMALL_IMAGE_BYTES:
            notes.append(
                f"{self.path.name} is only {self.size_bytes // 1024}KB -- that is "
                f"thumbnail-sized and will look poor in feed"
            )
        if self.image_type == "gif":
            notes.append(
                "a GIF is uploaded as a still image unless the placement "
                "supports animation; check it renders as intended"
            )
        return notes

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "size_kb": round(self.size_bytes / 1024, 1),
            "type": self.image_type,
        }


def inspect_image(path: str | Path) -> LocalImage:
    """Check a local image WITHOUT uploading it, so a preview can describe it.

    Every refusal here is one that would otherwise cost a round trip and come
    back as a Meta error that does not name the file.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise MutationError(f"No file at {resolved}.")

    size = resolved.stat().st_size
    if size == 0:
        raise MutationError(f"{resolved} is empty.")
    if size > MAX_IMAGE_BYTES:
        raise MutationError(
            f"{resolved.name} is {size / 1024 / 1024:.1f}MB, over Meta's "
            f"{MAX_IMAGE_BYTES // 1024 // 1024}MB limit for an ad image."
        )

    with resolved.open("rb") as handle:
        kind = sniff_image_type(handle.read(16))
    if kind is None:
        raise MutationError(
            f"{resolved.name} is {kind or 'not an image'}. Meta accepts "
            f"{', '.join(sorted(ALLOWED_IMAGE_TYPES))} for an ad image."
        )
    return LocalImage(path=resolved, size_bytes=size, image_type=kind)


def upload_image(
    client: MutatingMetaAdsClient,
    image: LocalImage,
    *,
    apply: bool = False,
    account_id: str | None = None,
) -> str | None:
    """Put an image in the account's library and return its ``image_hash``.

    Returns None when ``apply`` is False -- there is no hash until the file is
    actually uploaded, and inventing one would make a preview lie. The preview
    describes the FILE instead; see :meth:`LocalImage.describe`.

    Uploading the same file twice is harmless: Meta deduplicates by content and
    hands back the same hash, so a retried apply does not litter the library.
    """
    if not apply:
        return None

    target = client.resolve_account_id(account_id)
    path = f"{account_path(target)}/{IMAGE_EDGE}"

    correlation = client._audit.attempt(
        account_id=target,
        node_id="(new image)",
        method="POST",
        edge=IMAGE_EDGE,
        payload={"filename": image.path.name, "bytes": image.size_bytes},
    )
    try:
        with image.path.open("rb") as handle:
            response = client.transport.request(
                "POST", path, files={image.path.name: handle}
            )
    except Exception as exc:
        client._audit.outcome(correlation, ok=False, error=str(exc))
        raise MutationError(f"Uploading {image.path.name} failed.\n{exc}") from exc

    client._audit.outcome(correlation, ok=True, response=response)

    # Meta answers {"images": {"<filename>": {"hash": ..., "url": ...}}}, keyed
    # by the field name the file was sent under -- not by a fixed key.
    images = response.get("images") or {}
    for entry in images.values():
        if isinstance(entry, dict) and entry.get("hash"):
            return entry["hash"]
    raise MutationError(
        f"Meta accepted the upload but returned no image hash: {response!r}"
    )


def image_md5(image: LocalImage) -> str:
    """The md5 of a local file's bytes: pins the file between preview and apply.

    NOT Meta's image_hash -- checked live on 2026-11-19, the hash Meta returned
    for an upload differed from the file's md5 -- so it is never shown as one.
    """
    digest = hashlib.md5()  # noqa: S324 - Meta's identifier, not security
    with image.path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ImageUpload:
    """A plan that puts one local file in the image library.

    Not a JSON create: the file goes up as multipart, through
    :func:`upload_image`, which logs its name and size rather than its bytes.
    ``ctx.send`` recognises it; nothing goes out unless ``apply`` is True.
    """

    image: LocalImage
    label: str
    requires: None = None
    entity_type: str = "adimage"


def image_dimensions(head: bytes) -> tuple[int, int] | None:
    r"""(width, height) from an image's header bytes, or None.

    PNG IHDR, GIF logical screen, JPEG SOFn, WebP VP8/VP8L/VP8X. A stdlib
    parser rather than Pillow: the placement checks need two integers, not an
    imaging library.

    >>> import struct
    >>> png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1080, 1350)
    >>> image_dimensions(png)
    (1080, 1350)
    >>> image_dimensions(b"GIF89a" + struct.pack("<HH", 600, 314))
    (600, 314)
    >>> image_dimensions(b"nothing") is None
    True
    """
    import struct

    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24 and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b"VP8 " and len(head) >= 30:
            w, h = struct.unpack("<HH", head[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(head) >= 25:
            b = head[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return w, h
        if chunk == b"VP8X" and len(head) >= 30:
            w = 1 + int.from_bytes(head[24:27], "little")
            h = 1 + int.from_bytes(head[27:30], "little")
            return w, h
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", head[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return w, h
            i += 2 + length
    return None


def local_image_dimensions(path: str | Path) -> tuple[int, int] | None:
    """Read just enough of a file to know its size in pixels."""
    with Path(path).expanduser().open("rb") as handle:
        return image_dimensions(handle.read(256 * 1024))


def check_video_usable(client: MutatingMetaAdsClient, video_id: str) -> dict[str, Any]:
    """Confirm an EXISTING video is finished processing and can go in an ad.

    A video that is still encoding yields an ad Meta rejects, with an error
    about an invalid video that does not say "wait a minute".
    """
    try:
        video = client.read.node(str(video_id), ("id", "status", "title", "length"))
    except Exception as exc:
        raise MutationError(
            f"No video {video_id} this token can see. On Meta a missing object "
            f"and a missing permission are the same error."
        ) from exc

    state = (video.get("status") or {}).get("video_status")
    if state and state != "ready":
        raise MutationError(
            f"Video {video_id} is {state}, not ready. An ad built on a video "
            f"that is still processing is rejected. Wait and try again."
        )
    return video


#: Meta's ceiling for an ad video file.
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024

#: What Meta recommends for ad video: MP4 or MOV, both ISO media files whose
#: first box is ``ftyp``. Anything else is refused locally rather than after
#: a long upload ends in an encoding error.
VIDEO_TYPES = {"qt  ": "mov"}


@dataclass(frozen=True)
class LocalVideo:
    """A video file on disk, checked but not yet uploaded."""

    path: Path
    size_bytes: int
    video_type: str
    duration_seconds: float | None

    def describe(self) -> str:
        length = (f", {int(self.duration_seconds // 60)}:{int(self.duration_seconds % 60):02d}"
                  if self.duration_seconds is not None else "")
        return (f"{self.path.name} ({self.size_bytes / 1024 / 1024:.1f}MB "
                f"{self.video_type}{length})")


def sniff_video_type(head: bytes) -> str | None:
    r"""mp4 or mov from a file's first bytes, or None.

    >>> sniff_video_type(b"\x00\x00\x00\x18ftypmp42"), sniff_video_type(b"\x00\x00\x00\x14ftypqt  ")
    ('mp4', 'mov')
    >>> sniff_video_type(b"RIFF....AVI ") is None
    True
    """
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    return VIDEO_TYPES.get(head[8:12].decode("latin-1"), "mp4")


def mp4_duration(path: Path) -> float | None:
    """Seconds, from the ``mvhd`` box inside ``moov``; None if not found.

    Reads box headers only (plus moov itself), so a 2GB file costs a few
    seeks, not a read of the whole file.
    """
    import struct

    def boxes(handle, end):
        while handle.tell() + 8 <= end:
            at = handle.tell()
            size, kind = struct.unpack(">I4s", handle.read(8))
            header = 8
            if size == 1:
                size = struct.unpack(">Q", handle.read(8))[0]
                header = 16
            elif size == 0:
                size = end - at
            if size < header:
                return
            yield kind, at + header, at + size
            handle.seek(at + size)

    try:
        with path.open("rb") as handle:
            total = path.stat().st_size
            for kind, body, end in boxes(handle, total):
                if kind != b"moov":
                    continue
                handle.seek(body)
                for inner, inner_body, _ in boxes(handle, end):
                    if inner != b"mvhd":
                        continue
                    handle.seek(inner_body)
                    version = handle.read(4)[0]
                    if version == 1:
                        handle.read(16)
                        scale, length = struct.unpack(">IQ", handle.read(12))
                    else:
                        handle.read(8)
                        scale, length = struct.unpack(">II", handle.read(8))
                    return round(length / scale, 2) if scale else None
    except (OSError, struct.error, IndexError):
        return None
    return None


def inspect_video(path: str | Path) -> LocalVideo:
    """Check a local video WITHOUT uploading it, so a preview can describe it."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise MutationError(f"No file at {resolved}.")
    size = resolved.stat().st_size
    if size == 0:
        raise MutationError(f"{resolved} is empty.")
    if size > MAX_VIDEO_BYTES:
        raise MutationError(f"{resolved.name} is {size / 1024 ** 3:.1f}GB, over Meta's 4GB limit "
                            "for an ad video.")
    with resolved.open("rb") as handle:
        kind = sniff_video_type(handle.read(16))
    if kind is None:
        raise MutationError(f"{resolved.name} is not an MP4 or MOV file, the formats Meta "
                            "recommends for ad video. Export it as MP4 (H.264) first.")
    return LocalVideo(path=resolved, size_bytes=size, video_type=kind,
                      duration_seconds=mp4_duration(resolved))


@dataclass(frozen=True)
class VideoUpload:
    """A plan that puts one local video in the library (see ``ctx.send``).

    ``wait_seconds`` bounds how long the apply waits for Meta to finish
    processing it; the video is usable in an ad only once it is ready.
    """

    video: LocalVideo
    label: str
    wait_seconds: int = 0
    requires: None = None
    entity_type: str = "advideo"


def preferred_thumbnail(client: MutatingMetaAdsClient, video_id: str) -> str | None:
    """The cover frame Meta suggests for a video, as a URL; None if it has none yet.

    A video creative needs a thumbnail. When none is given, this is the one
    Ads Manager would pick: the preferred entry of the video's ``thumbnails``,
    else its ``picture``.
    """
    try:
        row = client.read.node(str(video_id), ("id", "picture", "thumbnails{uri,is_preferred}"))
    except Exception:  # noqa: BLE001 - no thumbnail is a None, the caller decides
        return None
    thumbs = (row.get("thumbnails") or {}).get("data") or []
    preferred = [t.get("uri") for t in thumbs if t.get("is_preferred") and t.get("uri")]
    return (preferred or [t.get("uri") for t in thumbs if t.get("uri")] or [row.get("picture")])[0]


def upload_video(
    client: MutatingMetaAdsClient,
    video: LocalVideo,
    *,
    apply: bool = False,
    account_id: str | None = None,
) -> str | None:
    """Put a video in the account's library with Meta's resumable upload; its id.

    Three phases on the ``advideos`` edge: ``start`` (the file size) answers
    with a session and the first byte range to send, each ``transfer`` sends
    that range and answers with the next, and ``finish`` closes the session.
    Meta picks the chunk size, so a large file and a small one go the same way.
    The audit log keeps the file's name and size, never its bytes.

    The id comes back BEFORE Meta has processed the video: see
    :func:`wait_until_ready`. Returns None when ``apply`` is False.
    """
    if not apply:
        return None
    target = client.resolve_account_id(account_id)
    path = f"{account_path(target)}/{VIDEO_EDGE}"
    correlation = client._audit.attempt(
        account_id=target, node_id="(new video)", method="POST", edge=VIDEO_EDGE,
        payload={"filename": video.path.name, "bytes": video.size_bytes},
    )
    try:
        start = client.transport.request("POST", path, data={
            "upload_phase": "start", "file_size": str(video.size_bytes)})
        session, video_id = start["upload_session_id"], str(start["video_id"])
        begin, end = int(start["start_offset"]), int(start["end_offset"])
        with video.path.open("rb") as handle:
            while begin < end:
                handle.seek(begin)
                answer = client.transport.request(
                    "POST", path,
                    data={"upload_phase": "transfer", "upload_session_id": session,
                          "start_offset": str(begin)},
                    files={"video_file_chunk": (video.path.name, handle.read(end - begin))},
                )
                begin, end = int(answer["start_offset"]), int(answer["end_offset"])
        finish = client.transport.request("POST", path, data={
            "upload_phase": "finish", "upload_session_id": session, "title": video.path.name})
        if finish.get("success") is not True:
            raise MutationError(f"Meta did not confirm the finished upload: {finish!r}")
    except Exception as exc:
        client._audit.outcome(correlation, ok=False, error=str(exc))
        raise MutationError(f"Uploading {video.path.name} failed.\n{exc}") from exc
    client._audit.outcome(correlation, ok=True, response={"video_id": video_id})
    return video_id


def wait_until_ready(client: MutatingMetaAdsClient, video_id: str, *, timeout: float,
                     interval: float = 5.0, sleep=None, clock=None) -> str:
    """Poll a video's ``status.video_status`` until ready, error or ``timeout``.

    Returns the last status seen: ``ready``, ``error``, or whatever it was
    still doing (``processing``, ``uploading``...) when time ran out. Bounded
    on purpose -- a tool call must not hang on Meta's encoder.
    """
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max(0.0, timeout)
    while True:
        row = client.read.node(str(video_id), ("id", "status"))
        state = (row.get("status") or {}).get("video_status") or "unknown"
        if state in ("ready", "error") or clock() >= deadline:
            return state
        sleep(min(interval, max(0.0, deadline - clock())))


