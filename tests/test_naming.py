import re
import unicodedata

import pytest

from bibvault.naming import disambiguated_stem, filename_for, sanitize_citation_key

SAFE = re.compile(r"^[\w.-]+$")


def test_plain_keys_are_unchanged():
    for key in ["iceberg2020", "lakehouse-comparison23", "rum_conjecture", "Pixels.S3"]:
        assert sanitize_citation_key(key) == key


@pytest.mark.parametrize(
    ("key", "stem"),
    [
        ("张三2020", "张三2020"),
        ("数据湖综述", "数据湖综述"),
        ("王小明:湖仓综述2024", "王小明_湖仓综述2024"),
        ("李四 2021", "李四_2021"),
        ("論文２０２４", "論文２０２４"),
        ("田中2019", "田中2019"),
        ("김철수2022", "김철수2022"),
        ("müller2020", "müller2020"),
        ("论文🚀2024", "论文_2024"),
        ("「标题」2024", "标题_2024"),
    ],
)
def test_non_ascii_letters_are_kept(key, stem):
    assert sanitize_citation_key(key) == stem


def test_decomposed_and_composed_keys_get_the_same_stem():
    composed = "m\u00fcller2020"
    decomposed = "mu\u0308ller2020"
    assert composed != decomposed
    assert sanitize_citation_key(decomposed) == sanitize_citation_key(composed) == composed
    assert unicodedata.is_normalized("NFC", sanitize_citation_key(decomposed))


def test_long_cjk_keys_fit_filesystem_byte_limits():
    first = "数据湖" * 60 + "甲"
    second = "数据湖" * 60 + "乙"
    stem = sanitize_citation_key(first)
    assert len(stem.encode("utf-8")) <= 120
    assert len(filename_for(disambiguated_stem(first, "doi:10.1/x"), "supplement").encode("utf-8")) + len(
        ".partial"
    ) <= 255
    assert stem == sanitize_citation_key(first)
    assert stem != sanitize_citation_key(second)
    assert stem.startswith("数据湖")


@pytest.mark.parametrize(
    ("role", "name"),
    [
        ("pdf", "iceberg2020.pdf"),
        ("preprint", "iceberg2020.preprint.pdf"),
        ("accepted", "iceberg2020.accepted.pdf"),
        ("published", "iceberg2020.published.pdf"),
        ("supplement", "iceberg2020.supplement.pdf"),
        ("markdown", "iceberg2020.md"),
    ],
)
def test_role_suffixes(role, name):
    assert filename_for("iceberg2020", role) == name


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        filename_for("iceberg2020", "thesis")


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "/absolute/path",
        "a/b",
        "smith:2020",
        "key with spaces",
        "müller2020",
        "引用2024",
        "...",
        "",
        "<>|?*",
        'a"b',
        "tab\there",
        "line\nbreak",
        "nul\x00byte",
        "：全角冒号",
    ],
)
def test_unsafe_keys_become_one_safe_path_component(key):
    stem = sanitize_citation_key(key)
    assert SAFE.match(stem)
    assert not stem.startswith(".")
    assert stem not in {".", ".."}
    assert not set(stem) & set('/\\:*?"<>|')
    assert all(ch.isprintable() and not ch.isspace() for ch in stem)


@pytest.mark.parametrize("key", ["con", "NUL", "com1", "Lpt9", "aux.2020"])
def test_windows_reserved_names_are_avoided(key):
    stem = sanitize_citation_key(key)
    assert stem.split(".")[0].upper() not in {"CON", "NUL", "COM1", "LPT9", "AUX"}


def test_long_keys_are_truncated_deterministically():
    first = "x" * 200 + "a"
    second = "x" * 200 + "b"
    assert len(sanitize_citation_key(first)) <= 120
    assert sanitize_citation_key(first) == sanitize_citation_key(first)
    assert sanitize_citation_key(first) != sanitize_citation_key(second)


def test_disambiguated_stem_depends_on_the_identity():
    one = disambiguated_stem("foo", "doi:10.1000/one")
    two = disambiguated_stem("foo", "doi:10.1000/two")
    assert one.startswith("foo--")
    assert one != two
    assert one == disambiguated_stem("foo", "doi:10.1000/one")
