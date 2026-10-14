import re
from pathlib import Path

import pytest

from bibvault.identity import reference_id
from bibvault.naming import sanitize_citation_key
from bibvault.parsing import parse_bibtex, parse_paths
from tests.helpers import SAMPLE_BIB

ROOT = Path(__file__).resolve().parents[1]
MY_BIB = ROOT / "my.bib"


def _declared_entries(text: str) -> list[str]:
    return [
        key
        for kind, key in re.findall(r"(?m)^@(\w+)\{([^,\s{}]+),", text)
        if kind.lower() not in {"string", "comment", "preamble"}
    ]


@pytest.fixture(scope="module")
def sample():
    references, warnings = parse_paths([SAMPLE_BIB])
    return {item.citation_key: item for item in references}, warnings


def test_sample_bib_parses_every_entry_without_warnings(sample):
    by_key, warnings = sample
    assert warnings == []
    assert list(by_key) == _declared_entries(SAMPLE_BIB.read_text(encoding="utf-8"))


def test_entry_types_are_kept_and_lowercased(sample):
    by_key, _ = sample
    assert by_key["hudi-index"].entry_type == "online"
    assert by_key["pixels-retina"].entry_type == "misc"
    assert by_key["cidr2021"].entry_type == "proceedings"
    references, _ = parse_bibtex("@ARTICLE{upper, title={Upper}}")
    assert references[0].entry_type == "article"


def test_latex_escapes_become_unicode(sample):
    by_key, _ = sample
    authors = by_key["delta-lake-paper"].authors
    assert "Alicja Łuszczak" in authors
    assert "Michał Świtakowski" in authors


def test_protective_braces_are_removed_from_titles(sample):
    by_key, _ = sample
    assert by_key["delta-lake-paper"].title == (
        "Delta Lake: High-Performance ACID Table Storage over Cloud Object Stores"
    )
    assert by_key["lance-format-preprint"].title.startswith("Lance: Efficient")


def test_string_macros_are_expanded(sample):
    by_key, _ = sample
    assert by_key["delta-lake-paper"].venue == "Proc. VLDB Endow."
    assert by_key["delta-lake-paper"].volume == "13"
    assert by_key["delta-lake-paper"].pages == "3411--3424"


def test_multiline_and_corporate_authors(sample):
    by_key, _ = sample
    assert by_key["lakehouse-comparison23"].authors[-1] == "Matei Zaharia"
    assert len(by_key["lakehouse-comparison23"].authors) == 6
    assert by_key["hudi-index"].authors == ["Apache Software Foundation"]


def test_identifiers_are_normalized(sample):
    by_key, _ = sample
    assert by_key["delta-lake-alias"].doi == "10.14778/3415478.3415560"
    assert by_key["pixels-s3"].doi == "10.1109/icde53745.2022.00276"
    assert by_key["lance-format-preprint"].arxiv_id == "2504.15247"
    assert by_key["lance-eprint"].arxiv_id == "2504.15247"


def test_crossref_fields_are_inherited(sample):
    by_key, _ = sample
    child = by_key["crossref-child"]
    assert child.year == "2021"
    assert child.venue == "11th Conference on Innovative Data Systems Research (CIDR)"


def test_comments_are_not_entries(sample):
    by_key, _ = sample
    assert all(not key.startswith("comment") for key in by_key)


def test_missing_metadata_is_tolerated():
    references, warnings = parse_bibtex("@article{bare, doi = {10.1000/bare}}")
    assert warnings == []
    assert references[0].title is None
    assert references[0].year is None
    assert references[0].authors == []
    assert references[0].doi == "10.1000/bare"


def test_duplicate_key_in_one_file_keeps_the_first():
    text = """
    @article{same, title = {First}, year = {2020}}
    @article{same, title = {Second}, year = {2021}}
    """
    references, warnings = parse_bibtex(text)
    assert [item.title for item in references] == ["First"]
    assert any("same" in warning for warning in warnings)


def test_chinese_keys_parse():
    references, warnings = parse_bibtex("@article{张三2020, title = {湖仓一体综述}, year = {2020}}")
    assert warnings == []
    assert [(item.citation_key, item.title) for item in references] == [("张三2020", "湖仓一体综述")]


def test_decomposed_spelling_of_a_key_is_a_duplicate_not_a_parse_error():
    text = "@article{m\u00fcller2020, title = {A}}\n@article{mu\u0308ller2020, title = {B}}\n"
    references, warnings = parse_bibtex(text)
    assert [item.title for item in references] == ["A"]
    assert warnings == ["duplicate citation key 'm\u00fcller2020' ignored after the first entry"]


def test_duplicate_key_across_files_keeps_the_first(tmp_path: Path):
    first = tmp_path / "a.bib"
    second = tmp_path / "b.bib"
    first.write_text("@article{shared, title = {From A}}\n", encoding="utf-8")
    second.write_text(
        "@article{shared, title = {From B}}\n@article{other, title = {Other}}\n",
        encoding="utf-8",
    )
    references, warnings = parse_paths([first, second])
    assert [(item.citation_key, item.title) for item in references] == [
        ("shared", "From A"),
        ("other", "Other"),
    ]
    assert any("shared" in warning for warning in warnings)


def test_malformed_entry_is_reported_and_the_rest_survive():
    text = """
    @article{good-before, title = {Before}, year = {2020}}
    @article{broken, title = {Missing a closing brace, year = {2021}
    @article{good-after, title = {After}, year = {2022}}
    """
    references, warnings = parse_bibtex(text)
    keys = [item.citation_key for item in references]
    assert "good-after" in keys
    assert "broken" not in keys
    assert any("broken" in warning for warning in warnings)


def test_entry_with_no_fields_is_reported():
    references, warnings = parse_bibtex("@article{onlykey,}\n@article{ok, title={Ok}}")
    assert [item.citation_key for item in references] == ["ok"]
    assert any("onlykey" in warning for warning in warnings)


def test_empty_input():
    assert parse_bibtex("") == ([], [])
    assert parse_bibtex("% only a comment\n") == ([], [])


def test_utf8_bom_does_not_hide_the_first_entry(tmp_path: Path):
    path = tmp_path / "bom.bib"
    path.write_bytes(b"\xef\xbb\xbf@article{first, title = {First}}\n")
    references, warnings = parse_paths([path])
    assert warnings == []
    assert [item.citation_key for item in references] == ["first"]


def test_latin1_file_is_decoded_with_a_warning(tmp_path: Path):
    path = tmp_path / "latin1.bib"
    path.write_bytes("@article{muller, author = {Jürgen Müller}, title = {Café}}\n".encode("latin-1"))
    references, warnings = parse_paths([path])
    assert references[0].authors == ["Jürgen Müller"]
    assert any("Latin-1" in warning for warning in warnings)


def test_source_file_is_recorded(sample):
    by_key, _ = sample
    assert by_key["lakehouse"].source == str(SAMPLE_BIB)


@pytest.mark.skipif(not MY_BIB.exists(), reason="my.bib is a local bibliography that is not committed")
def test_local_my_bib_parses_cleanly():
    text = MY_BIB.read_text(encoding="utf-8")
    references, warnings = parse_paths([MY_BIB])
    assert warnings == []
    assert [item.citation_key for item in references] == _declared_entries(text)
    ids = [reference_id(item) for item in references]
    assert all(value.split(":", 1)[0] in {"doi", "arxiv", "url", "internal"} for value in ids)
    stems = [sanitize_citation_key(item.citation_key).casefold() for item in references]
    assert len(set(stems)) == len(stems)
