import pytest

from bibvault.identity import (
    doi_from_url,
    extract_arxiv_id,
    internal_identity_warning,
    legacy_internal_id,
    normalize_doi,
    normalize_url,
    reference_id,
)
from bibvault.models import Reference


def ref(**fields) -> Reference:
    fields.setdefault("citation_key", "key")
    fields.setdefault("entry_type", "article")
    return Reference(**fields)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1145/ABC.123", "10.1145/abc.123"),
        ("https://doi.org/10.1145/abc", "10.1145/abc"),
        ("http://dx.doi.org/10.1145/abc", "10.1145/abc"),
        ("doi:10.1145/abc", "10.1145/abc"),
        ("DOI:10.1145/abc", "10.1145/abc"),
        ("  10.1145/abc  ", "10.1145/abc"),
        ("10.1145/abc.", "10.1145/abc"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://doi.org/10.1145/3802514.3812602", "10.1145/3802514.3812602"),
        ("http://dx.doi.org/10.1007/S002360050048", "10.1007/s002360050048"),
        ("https://doi.org/10.1002%2Fspe.3000", "10.1002/spe.3000"),
        ("https://dl.acm.org/doi/10.1145/3802514.3812602", None),
        ("https://doi.org/", None),
        ("https://arxiv.org/abs/2504.15247", None),
        (None, None),
    ],
)
def test_doi_from_url(url, expected):
    assert doi_from_url(url) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"doi": "10.48550/arxiv.2504.15247"}, "2504.15247"),
        ({"doi": "10.48550/arxiv.2504.15247v3"}, "2504.15247"),
        ({"url": "https://arxiv.org/abs/2504.15247"}, "2504.15247"),
        ({"url": "https://arxiv.org/pdf/2504.15247v2.pdf"}, "2504.15247"),
        ({"url": "https://arxiv.org/abs/1501.00001"}, "1501.00001"),
        ({"eprint": "2504.15247"}, "2504.15247"),
        ({"eprint": "arXiv:2504.15247v1"}, "2504.15247"),
        ({"eprint": "2504.15247", "archive_prefix": "arXiv"}, "2504.15247"),
        ({"eprint": "cs/0112017", "archive_prefix": "arXiv"}, "cs/0112017"),
        ({"url": "https://arxiv.org/abs/hep-th/9901001v1"}, "hep-th/9901001"),
        ({"doi": "10.48550/arxiv.cs/0112017"}, "cs/0112017"),
        ({"eprint": "2504.15247", "archive_prefix": "PubMed"}, None),
        ({"doi": "10.1145/3802514.3812602"}, None),
        ({"url": "https://example.org/abs/2504.15247"}, None),
        ({}, None),
    ],
)
def test_extract_arxiv_id(fields, expected):
    assert extract_arxiv_id(**fields) == expected


def test_doi_wins_over_other_identifiers():
    reference = ref(doi="10.1000/x", arxiv_id="2504.15247", url="https://example.org/x.pdf")
    assert reference_id(reference) == "doi:10.1000/x"


def test_arxiv_doi_and_eprint_are_the_same_publication():
    via_doi = ref(doi="10.48550/arxiv.2504.15247", arxiv_id="2504.15247")
    via_eprint = ref(arxiv_id="2504.15247")
    assert reference_id(via_doi) == reference_id(via_eprint) == "arxiv:2504.15247"


def test_url_identity_ignores_trivial_differences():
    a = ref(url="https://Docs.Example.org/paper.pdf#page=2")
    b = ref(url="https://docs.example.org/paper.pdf")
    c = ref(url="https://docs.example.org/paper.pdf?version=2")
    assert reference_id(a) == reference_id(b)
    assert reference_id(a) != reference_id(c)
    assert normalize_url("https://example.org/docs/") == "https://example.org/docs"


def test_internal_identity_is_stable_and_independent_of_the_key():
    one = ref(citation_key="a", title="A Study of Things", year="2020", authors=["Ann Lee"])
    two = ref(citation_key="b", title="A study of things!", year="2020", authors=["Ann  Lee"])
    other = ref(citation_key="a", title="A Different Study", year="2020", authors=["Ann Lee"])
    assert reference_id(one) == reference_id(two)
    assert reference_id(one).startswith("internal:")
    assert reference_id(one) != reference_id(other)


def test_internal_identity_preserves_unicode_letters_and_numbers():
    one = ref(title="湖仓一体架构", year="２０２４", authors=["张三"])
    same = ref(title="湖仓一体架构", year="2024", authors=["张三"])
    other = ref(title="数据密集型系统", year="2024", authors=["张三"])
    assert reference_id(one) == reference_id(same)
    assert reference_id(one) != reference_id(other)
    assert reference_id(one) != legacy_internal_id(one)


def test_internal_identity_normalizes_equivalent_unicode():
    composed = ref(title="Café 数据", year="2024", authors=["Zoë"])
    decomposed = ref(title="Cafe\u0301 数据", year="2024", authors=["Zoe\u0308"])
    assert reference_id(composed) == reference_id(decomposed)


def test_low_information_internal_identity_has_a_collision_warning():
    warning = internal_identity_warning(ref(year="2024"))
    assert warning is not None
    assert "may collide" in warning
    assert internal_identity_warning(ref(title="A useful title", year="2024")) is None
    assert internal_identity_warning(ref(doi="10.1000/x")) is None


def test_identity_is_never_the_citation_key():
    for reference in [
        ref(citation_key="iceberg2020", doi="10.1000/iceberg"),
        ref(citation_key="iceberg2020", arxiv_id="2504.15247"),
        ref(citation_key="iceberg2020", url="https://example.org/x"),
        ref(citation_key="iceberg2020"),
    ]:
        assert "iceberg2020" not in reference_id(reference)
