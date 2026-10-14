"""Ad creatives: reading one, editing its text, and building a new one.

THE CENTRAL FACT: a Meta creative is IMMUTABLE once it is attached to an ad.
There is no "change the headline" call. Editing ad copy means building a new
creative and pointing an ad at it -- either a new ad, or the existing one.
:mod:`metaads_reporting.write.ads` does the pointing; this module does the
building.

THE SECOND FACT, and the reason this module is shaped the way it is: there is
no single creative format. An account runs several at once, and the right edit
depends on which one you are holding:

``link_data``        a single image or link. message / name / description.
``video_data``       a video plus a thumbnail. message / title / link_description.
``asset_feed_spec``  Advantage+ and multi-locale: LISTS of bodies, titles,
                     descriptions and link_urls, tied together by ``adlabels``
                     so Meta can serve the French copy to French users.
``page-only``        catalog / dynamic, where the text comes from the product
                     feed and there is no copy on the creative to edit at all.

So this does not flatten everything into one shape. It READS the shape it was
given and rebuilds the SAME shape with the text changed, which is the only way
to edit a multi-locale creative without silently collapsing it to one language
or turning a video ad into an image ad.

THE FIELD NAMES DO NOT MATCH THE UI. What Ads Manager calls "primary text" is
``message`` on a link, ``message`` on a video and ``bodies[].text`` on an asset
feed. "Headline" is ``name``, ``title`` and ``titles[].text``. That mapping is
:data:`TEXT_FIELDS`, and getting it wrong writes the headline into the body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..account_id import account_path
from .client import MutatingMetaAdsClient, MutationError

EDGE = "adcreatives"

#: Everything needed to understand an existing creative well enough to rebuild it.
FIELDS = (
    "id",
    "name",
    "object_story_spec",
    "asset_feed_spec",
    "effective_object_story_id",
    "object_story_id",
    "degrees_of_freedom_spec",
    "url_tags",
    "instagram_user_id",
)

#: The shapes this module understands.
LINK = "link_data"
VIDEO = "video_data"
ASSET_FEED = "asset_feed_spec"
PAGE_ONLY = "page_only"

#: Ads Manager's words -> the API's, per shape. The single most error-prone
#: mapping in the whole Marketing API: three different names for "headline",
#: none of which is "headline".
TEXT_FIELDS: dict[str, dict[str, str]] = {
    LINK: {
        "primary_text": "message",
        "headline": "name",
        "description": "description",
        "link": "link",
    },
    VIDEO: {
        "primary_text": "message",
        "headline": "title",
        "description": "link_description",
        "link": "link",
    },
    ASSET_FEED: {
        "primary_text": "bodies",
        "headline": "titles",
        "description": "descriptions",
        "link": "link_urls",
    },
}

#: What a caller may ask to change. Anything else is refused rather than
#: quietly ignored -- a silently dropped edit reads as "Meta rejected my copy".
EDITABLE = frozenset({"primary_text", "headline", "description", "link"})

#: Fields Meta RETURNS on a read but REFUSES on a create, because they are
#: derived from another field that is also present.
#:
#: Caught by Meta's own validate_only on the first live dry run:
#:   ObjectStorySpecRedundant: Only one of image_url and image_hash should be
#:   specified in the field video_data of object_story_spec.
#:
#: A creative read back from the API is therefore NOT a valid payload for
#: creating one, which is not obvious and is the kind of thing that only shows
#: up against the real API. The hash is the canonical form -- it identifies the
#: image in the account's library, where the URL is just a rendering of it --
#: so the derived one goes.
DERIVED_ON_READ = {
    "image_url": "image_hash",
    "picture": "image_hash",
    "thumbnail_url": "image_hash",
}

#: Keys inside ``degrees_of_freedom_spec.creative_features_spec`` that Meta
#: returns on a read and REFUSES on a create, because they are deprecated.
#:
#: Also caught by validate_only on a live dry run:
#:   Creative should not include standard enhancements: Including standard
#:   enhancements field in creative has been deprecated. Please choose to set
#:   individual features instead.
#:
#: The spec as a whole is still worth carrying -- it holds the per-feature
#: OPT_OUTs, and dropping it opts a new creative into every automatic
#: enhancement the old one had turned off. So the deprecated umbrella key is
#: removed and the individual features are kept.
DEPRECATED_CREATIVE_FEATURES = frozenset({"standard_enhancements"})


@dataclass(frozen=True)
class Creative:
    """One creative, with its shape worked out."""

    id: str
    name: str
    shape: str
    raw: dict[str, Any]

    @property
    def story_spec(self) -> dict[str, Any]:
        return dict(self.raw.get("object_story_spec") or {})

    @property
    def page_id(self) -> str | None:
        return self.story_spec.get("page_id")

    @property
    def instagram_id(self) -> str | None:
        spec = self.story_spec
        return spec.get("instagram_user_id") or spec.get("instagram_actor_id")

    @property
    def editable_text(self) -> bool:
        """False for catalog/dynamic, where the copy lives in the product feed."""
        return self.shape in TEXT_FIELDS

    def current_text(self) -> dict[str, Any]:
        """The copy as it stands, in Ads Manager's vocabulary.

        An asset-feed creative returns LISTS, because it genuinely holds
        several of each -- one per locale. Flattening those to the first entry
        is how a French ad loses its French.
        """
        if not self.editable_text:
            return {}
        mapping = TEXT_FIELDS[self.shape]
        if self.shape == ASSET_FEED:
            feed = self.raw.get("asset_feed_spec") or {}
            out: dict[str, Any] = {}
            for label, key in mapping.items():
                entries = feed.get(key) or []
                if label == "link":
                    out[label] = [e.get("website_url") for e in entries]
                else:
                    out[label] = [e.get("text") for e in entries]
            return out
        block = self.story_spec.get(self.shape) or {}
        return {label: block.get(key) for label, key in mapping.items()}

    def describe(self) -> dict[str, Any]:
        text = self.current_text()
        return {
            "creative_id": self.id,
            "name": self.name,
            "shape": self.shape,
            "page_id": self.page_id,
            "instagram_id": self.instagram_id,
            "editable_text": self.editable_text,
            "text": text,
            "locales": (
                len(text.get("headline") or []) if self.shape == ASSET_FEED else 1
            ),
        }


def classify(raw: dict[str, Any]) -> str:
    """Work out which shape a creative is.

    >>> classify({"object_story_spec": {"link_data": {}}})
    'link_data'
    >>> classify({"object_story_spec": {"video_data": {}}})
    'video_data'
    >>> classify({"asset_feed_spec": {"bodies": []}})
    'asset_feed_spec'
    >>> classify({"object_story_spec": {"page_id": "1"}})
    'page_only'
    """
    spec = raw.get("object_story_spec") or {}
    if LINK in spec:
        return LINK
    if VIDEO in spec:
        return VIDEO
    if raw.get("asset_feed_spec"):
        return ASSET_FEED
    return PAGE_ONLY


def read(client: MutatingMetaAdsClient, creative_id: str) -> Creative:
    """Fetch one creative and classify it."""
    raw = client.node(str(creative_id), FIELDS)
    return Creative(
        id=str(raw.get("id")),
        name=raw.get("name") or "",
        shape=classify(raw),
        raw=raw,
    )


def check_edits(
    creative: Creative, edits: dict[str, Any], *, allow_empty: bool = False
) -> dict[str, Any]:
    """Refuse an edit this creative cannot take, rather than dropping it.

    ``allow_empty`` is for a MEDIA-ONLY change -- swapping the image and
    leaving every word alone is a perfectly ordinary edit, and refusing it as
    "no edits given" would be wrong. The caller is responsible for checking
    that *something* changes; ads.plan_duplicate_ad does.
    """
    if not edits and not allow_empty:
        raise MutationError("No edits given.")
    unknown = sorted(set(edits) - EDITABLE)
    if unknown:
        raise MutationError(
            f"Cannot edit {', '.join(unknown)}. Editable: "
            f"{', '.join(sorted(EDITABLE))}. Media is changed separately -- see "
            f"metaads_reporting.write.media."
        )
    if not creative.editable_text:
        raise MutationError(
            f"Creative {creative.id} is a {creative.shape} creative: its copy "
            f"comes from the product catalog, not from the creative, so there "
            f"is no text on it to change. Edit the catalog instead."
        )
    return {k: v for k, v in edits.items() if v is not None}


def strip_derived(block: dict[str, Any]) -> dict[str, Any]:
    """Drop fields Meta returns on a read but refuses on a create.

    >>> strip_derived({"image_hash": "abc", "image_url": "http://x", "title": "t"})
    {'image_hash': 'abc', 'title': 't'}
    >>> strip_derived({"image_url": "http://x"})
    {'image_url': 'http://x'}
    """
    return {
        key: value
        for key, value in block.items()
        if not (key in DERIVED_ON_READ and DERIVED_ON_READ[key] in block)
    }


def build_spec(
    creative: Creative, edits: dict[str, Any], *, allow_empty: bool = False
) -> dict[str, Any]:
    """The payload for a NEW creative: this one's shape, with ``edits`` applied.

    Everything not being edited is carried across verbatim. That is the whole
    point -- a video keeps its video, an asset feed keeps its locales, its
    adlabels and its customization rules, and only the text named in ``edits``
    moves.
    """
    edits = check_edits(creative, edits, allow_empty=allow_empty)
    mapping = TEXT_FIELDS[creative.shape]

    if creative.shape == ASSET_FEED:
        return _build_asset_feed(creative, edits, mapping)

    spec = creative.story_spec
    block = dict(spec.get(creative.shape) or {})
    for label, value in edits.items():
        block[mapping[label]] = value
    rebuilt = {k: v for k, v in spec.items() if k != creative.shape}
    rebuilt[creative.shape] = strip_derived(block)
    return {"object_story_spec": rebuilt}


def _build_asset_feed(
    creative: Creative, edits: dict[str, Any], mapping: dict[str, str]
) -> dict[str, Any]:
    """Rebuild a multi-locale asset feed, keeping its adlabels intact.

    An asset feed holds a LIST per field, and ``asset_customization_rules``
    references those entries BY ADLABEL to decide who sees which language.
    Replacing a list with a bare string therefore does not just lose the other
    locale -- it breaks the rules that point at it, and Meta rejects the
    creative with a message about an invalid customization rule that names
    none of this.

    So a single string edits every entry's text and keeps every adlabel. A list
    must match the existing length, position for position.
    """
    feed = dict(creative.raw.get("asset_feed_spec") or {})
    for label, value in edits.items():
        key = mapping[label]
        entries = [dict(e) for e in (feed.get(key) or [])]
        if not entries:
            raise MutationError(
                f"This creative has no {key} to edit. Present: "
                f"{', '.join(sorted(feed))}."
            )
        text_key = "website_url" if label == "link" else "text"
        if isinstance(value, (list, tuple)):
            if len(value) != len(entries):
                raise MutationError(
                    f"{label} has {len(entries)} entries on this creative (one "
                    f"per locale) and you gave {len(value)}. Give exactly "
                    f"{len(entries)}, in the same order, or a single string to "
                    f"apply to all of them."
                )
            for entry, replacement in zip(entries, value):
                entry[text_key] = replacement
        else:
            for entry in entries:
                entry[text_key] = value
        feed[key] = entries
    for media_key in ("images", "videos"):
        if feed.get(media_key):
            feed[media_key] = [strip_derived(dict(e)) for e in feed[media_key]]

    # An asset-feed creative carries BOTH specs: asset_feed_spec holds the
    # copy, object_story_spec holds the page and Instagram account the post is
    # published as. Returning only the feed loses the page, and Meta refuses
    # with "You must select an existing post or create a post for your ad
    # creative" -- which names neither field and reads like an Ads Manager
    # instruction rather than a missing parameter.
    out: dict[str, Any] = {"asset_feed_spec": feed}
    story = creative.story_spec
    if story:
        out["object_story_spec"] = story
    return out


def plan_payload(
    creative: Creative,
    edits: dict[str, Any],
    *,
    name: str | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """The full create-a-creative payload, ready for :func:`create`."""
    payload = build_spec(creative, edits, allow_empty=allow_empty)
    payload["name"] = name or f"{creative.name} (edited)"
    # Carried across so a rebuilt creative keeps the account's tracking and its
    # Advantage+ opt-outs. Dropping degrees_of_freedom_spec silently OPTS THE
    # NEW CREATIVE INTO every automatic enhancement the old one had turned off.
    if creative.raw.get("url_tags"):
        payload["url_tags"] = creative.raw["url_tags"]
    freedom = clean_degrees_of_freedom(creative.raw.get("degrees_of_freedom_spec"))
    if freedom:
        payload["degrees_of_freedom_spec"] = freedom
    return payload


def clean_degrees_of_freedom(spec: Any) -> dict[str, Any] | None:
    """Carry the per-feature opt-outs, drop the deprecated umbrella key.

    >>> clean_degrees_of_freedom({"creative_features_spec": {
    ...     "standard_enhancements": {"enroll_status": "OPT_OUT"},
    ...     "image_touchups": {"enroll_status": "OPT_OUT"}}})
    {'creative_features_spec': {'image_touchups': {'enroll_status': 'OPT_OUT'}}}
    >>> clean_degrees_of_freedom({"creative_features_spec":
    ...     {"standard_enhancements": {"enroll_status": "OPT_OUT"}}}) is None
    True
    >>> clean_degrees_of_freedom(None) is None
    True
    """
    if not isinstance(spec, dict):
        return None
    features = spec.get("creative_features_spec")
    if not isinstance(features, dict):
        return spec or None
    kept = {
        key: value
        for key, value in features.items()
        if key not in DEPRECATED_CREATIVE_FEATURES
    }
    if not kept:
        # Nothing but the deprecated key: sending an empty spec says nothing,
        # so send none at all.
        return None
    return {**{k: v for k, v in spec.items() if k != "creative_features_spec"},
            "creative_features_spec": kept}


def create(
    client: MutatingMetaAdsClient,
    payload: dict[str, Any],
    *,
    apply: bool = False,
    account_id: str | None = None,
):
    """Send a new creative. Validates server-side unless ``apply`` is True."""
    # No JSON encoding here: MutatingMetaAdsClient.create does it for every
    # caller. Doing it twice would send a quoted string of a string.
    return client.create(
        entity_type="adcreative",
        edge=EDGE,
        payload=payload,
        apply=apply,
        account_id=account_id,
    )


def social_proof(client: MutatingMetaAdsClient, creative: Creative) -> dict[str, Any]:
    """Likes, comments and shares a repoint would discard.

    The number that makes the new-ad-versus-repoint choice concrete. Pointing a
    live ad at a new creative usually means a new underlying post, and the
    engagement accumulated on the old one does not come with it.

    Returns ``{}`` when it cannot be read, which is common and not an error --
    a brand-new ad has no post yet.
    """
    story_id = creative.raw.get("effective_object_story_id") or creative.raw.get(
        "object_story_id"
    )
    if not story_id:
        return {}
    try:
        post = client.read.node(
            str(story_id),
            ("id", "likes.summary(true)", "comments.summary(true)", "shares"),
        )
    except Exception:
        return {}
    return {
        "post_id": story_id,
        "likes": ((post.get("likes") or {}).get("summary") or {}).get("total_count"),
        "comments": ((post.get("comments") or {}).get("summary") or {}).get(
            "total_count"
        ),
        "shares": (post.get("shares") or {}).get("count"),
    }
