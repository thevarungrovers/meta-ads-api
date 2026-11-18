"""Uploading an image, and reusing a video.

An ad does not carry a file. It carries a creative, and a creative carries an
``image_hash`` or a ``video_id`` that Meta already holds. So "change the image"
is two steps: put the file in the account's media library, then build a
creative that references it.

IMAGES ARE SUPPORTED, VIDEO UPLOAD IS NOT, and that is a deliberate line rather
than an oversight. The ``advideos`` edge is asynchronous: the POST returns an
id for a video Meta has not finished processing, and an ad built on it is
rejected until ``status.video_status`` reaches ``ready``. Doing that properly
means chunked upload for large files and a bounded polling loop, which is its
own feature. :func:`check_video_usable` lets an EXISTING video be reused --
which covers duplicating a video ad with new copy, the common case on an
account whose ads are mostly video -- and :func:`upload_video` refuses with
that explanation rather than half-doing it.

Nothing here is reachable from the MCP server except through a preview/apply
pair, and the upload happens at APPLY time because it is a write.
"""

from __future__ import annotations

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


def upload_video(*args: Any, **kwargs: Any) -> None:
    """Not implemented, on purpose. Reuse an existing video instead.

    The ``advideos`` edge is asynchronous and, above 50MB, chunked: the POST
    returns an id for a video Meta has not finished encoding, and anything
    built on it is rejected until it reports ready. Supporting that means a
    resumable upload and a bounded polling loop, which is a feature of its own
    rather than a few lines here.

    A stub that half-did it would be worse than its absence: it would create
    ads that fail review for a reason nobody could see.
    """
    raise MutationError(
        "Uploading a new video is not supported. Upload it in Ads Manager, "
        "then reuse its video_id here -- duplicating a video ad with new copy "
        "keeps the existing video and needs no upload at all."
    )
