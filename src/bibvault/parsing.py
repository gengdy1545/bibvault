"""BibTeX parsing.

``bibtexparser`` 1.4 is the V0.1 parser. It covers nested braces, ``%`` comments,
and the ``@online`` entries used in this repository. Non-standard types are kept.
"""

import re
import unicodedata
from pathlib import Path

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode

from bibvault.errors import ParseError
from bibvault.identity import doi_from_url, extract_arxiv_id, normalize_doi
from bibvault.models import Reference


def parse_bibtex(text: str, source: str | None = None) -> tuple[list[Reference], list[str]]:
    parser = BibTexParser(common_strings=True, add_missing_from_crossref=True)
    parser.ignore_nonstandard_types = False
    parser.customization = convert_to_unicode
    try:
        library = bibtexparser.loads(text, parser=parser)
    except Exception as exc:
        raise ParseError(f"failed to parse BibTeX{f' in {source}' if source else ''}: {exc}") from exc

    references: list[Reference] = []
    warnings: list[str] = []
    seen: set[str] = set()
    # bibtexparser returns keys in NFC, whatever form the file used.
    parsed_keys = {
        unicodedata.normalize("NFC", str(record.get("ID") or "").strip()) for record in library.entries
    }
    for key in _declared_keys(text):
        if unicodedata.normalize("NFC", key) not in parsed_keys:
            warnings.append(f"entry '{key}' could not be parsed and was skipped")
    for record in library.entries:
        key = str(record.get("ID") or "").strip()
        if not key:
            warnings.append("skipped an entry with an empty citation key")
            continue
        if key in seen:
            warnings.append(f"duplicate citation key '{key}' ignored after the first entry")
            continue
        seen.add(key)
        url = _field(record, "url")
        doi = normalize_doi(_field(record, "doi")) or doi_from_url(url)
        eprint = _field(record, "eprint")
        archive_prefix = _field(record, "archiveprefix")
        references.append(
            Reference(
                citation_key=key,
                entry_type=str(record.get("ENTRYTYPE") or "misc").lower(),
                title=_clean(_field(record, "title")),
                authors=_authors(record.get("author")),
                year=_field(record, "year"),
                doi=doi,
                url=url,
                eprint=eprint,
                archive_prefix=archive_prefix,
                arxiv_id=extract_arxiv_id(doi=doi, url=url, eprint=eprint, archive_prefix=archive_prefix),
                publisher=_field(record, "publisher"),
                venue=_field(record, "journal") or _field(record, "booktitle"),
                volume=_field(record, "volume"),
                number=_field(record, "number") or _field(record, "issue"),
                pages=_field(record, "pages"),
                source=source,
            )
        )
    return references, warnings


def parse_paths(paths: list[Path]) -> tuple[list[Reference], list[str]]:
    references: list[Reference] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for path in paths:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            warnings.append(f"{path} is not UTF-8; decoded it as Latin-1")
        parsed, parsed_warnings = parse_bibtex(text, source=str(path))
        warnings.extend(parsed_warnings)
        for reference in parsed:
            if reference.citation_key in seen:
                warnings.append(
                    f"duplicate citation key '{reference.citation_key}' ignored after the first file"
                )
                continue
            seen.add(reference.citation_key)
            references.append(reference)
    return references, warnings


_ENTRY_START = re.compile(r"(?m)^[ \t]*@[ \t]*(\w+)[ \t]*[{(][ \t]*([^,\s{}()]+)[ \t]*,")
_NON_ENTRIES = {"string", "comment", "preamble"}


def _declared_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _ENTRY_START.finditer(text):
        if match.group(1).lower() not in _NON_ENTRIES and match.group(2) not in keys:
            keys.append(match.group(2))
    return keys


def _field(record: dict, name: str) -> str | None:
    value = record.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def _authors(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("\n", " ")
    return [part.strip() for part in text.split(" and ") if part.strip()]
