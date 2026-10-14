"""Citation-key filenames.

``{citation_key}.pdf`` is the preferred full text. Other versions keep an
explicit suffix and are never written over that preferred file.
"""

import hashlib
import re
import unicodedata

# Unicode \w: letters and digits of any script (so CJK keys stay readable) plus "_".
_UNSAFE = re.compile(r"[^\w.-]+")
# Filesystems cap a name at 255 bytes; leave room for "--hash", role suffixes, and ".partial".
_MAX_STEM_BYTES = 120

ROLE_SUFFIX = {
    "pdf": "",
    "preprint": ".preprint",
    "accepted": ".accepted",
    "published": ".published",
    "supplement": ".supplement",
}

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}


def sanitize_citation_key(citation_key: str) -> str:
    # NFC so that composed and decomposed spellings share one filename, as macOS treats them.
    cleaned = unicodedata.normalize("NFC", citation_key).strip()
    cleaned = _UNSAFE.sub("_", cleaned)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "ref"
    head, dot, tail = cleaned.partition(".")
    if head.upper() in _WINDOWS_RESERVED:
        cleaned = f"{head}_{dot}{tail}"
    if len(cleaned.encode("utf-8")) > _MAX_STEM_BYTES:
        digest = hashlib.sha256(citation_key.encode("utf-8")).hexdigest()[:8]
        prefix = cleaned.encode("utf-8")[:100].decode("utf-8", "ignore")
        cleaned = f"{prefix}-{digest}"
    return cleaned


def filename_for(stem: str, role: str) -> str:
    if role == "markdown":
        return f"{stem}.md"
    suffix = ROLE_SUFFIX.get(role)
    if suffix is None:
        raise ValueError(f"unknown file role: {role}")
    return f"{stem}{suffix}.pdf"


def disambiguated_stem(citation_key: str, reference_id: str) -> str:
    base = sanitize_citation_key(citation_key)
    digest = hashlib.sha256(reference_id.encode("utf-8")).hexdigest()[:8]
    return f"{base}--{digest}"
