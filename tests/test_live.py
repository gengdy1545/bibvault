"""Smoke tests against the real services. Run with BIBVAULT_LIVE_TESTS=1."""

import os

import pytest

from bibvault.pdfcheck import assert_pdf_file
from tests.helpers import fetch, locate, make_settings, refs

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("BIBVAULT_LIVE_TESTS") != "1", reason="set BIBVAULT_LIVE_TESTS=1"),
]

CIDR = """
@inproceedings{lakehouse-comparison23,
    title = {Analyzing and Comparing Lakehouse Storage Systems},
    year  = {2023},
    url   = {https://www.cidrdb.org/cidr2023/papers/p92-jain.pdf}
}
"""

ARXIV = """
@article{lance-format-preprint,
    title = {Lance: Efficient Random Access in Columnar Storage through Adaptive Structural Encodings},
    year  = {2025},
    doi   = {10.48550/arXiv.2504.15247}
}
@misc{lance-eprint,
    title = {Lance},
    year = {2025},
    eprint = {2504.15247v2},
    archivePrefix = {arXiv}
}
"""


@pytest.fixture
def live_settings(project):
    return make_settings(project, max_retries=2, min_interval_seconds=1, timeout_seconds=60)


def test_direct_conference_pdf(live_settings):
    result = fetch(live_settings, refs(CIDR))["lakehouse-comparison23"]
    assert result.status == "downloaded", result.error
    assert_pdf_file(live_settings.library_root / "lakehouse-comparison23.pdf")


def test_arxiv_preprint_under_two_keys(live_settings):
    results = fetch(live_settings, refs(ARXIV))
    assert sorted(item.status for item in results.values()) == ["already_exists", "downloaded"]
    assert locate(live_settings, "lance-eprint")["reference_id"] == "arxiv:2504.15247"
