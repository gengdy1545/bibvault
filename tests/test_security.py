"""Defensive behaviour: untrusted URLs, oversized bodies, hostile keys, and secrets."""

import logging
import os

import httpx
import pytest

from tests.helpers import (
    SAMPLE_BIB,
    cli,
    fetch,
    leftovers,
    located_bytes,
    location,
    make_pdf,
    make_settings,
    mock_openalex,
    no_openalex,
    refs,
    work,
    write_config,
)


@pytest.fixture
def offline(project):
    return make_settings(project, resolution=no_openalex())


def entry(url: str, key: str = "paper") -> str:
    return f"@misc{{{key}, title = {{Paper}}, year = {{2020}}, url = {{{url}}}}}"


def stored_files(settings) -> list[str]:
    if not settings.library_root.exists():
        return []
    return sorted(path.name for path in settings.library_root.iterdir())


def test_file_urls_are_never_read(offline, tmp_path):
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(make_pdf("local secret"))
    result = fetch(offline, refs(entry(secret.as_uri())))["paper"]
    assert result.status == "failed"
    assert "refusing" in result.error
    assert stored_files(offline) == []


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/paper.pdf",
        "http://localhost:8080/paper.pdf",
        "http://169.254.169.254/latest/meta-data/paper.pdf",
        "http://10.0.0.1/paper.pdf",
        "http://192.168.0.1/paper.pdf",
        "http://[::1]/paper.pdf",
        "http://nas.local/paper.pdf",
    ],
)
def test_local_network_urls_are_refused(offline, respx_mock, url):
    route = respx_mock.get(url).respond(200, content=make_pdf("internal"))
    result = fetch(offline, refs(entry(url)))["paper"]
    assert result.status == "failed"
    assert "refusing" in result.error
    assert not route.called
    assert stored_files(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_redirect_into_the_local_network_is_refused(offline, respx_mock):
    respx_mock.get("https://oa.example/paper.pdf").respond(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/paper.pdf"}
    )
    metadata = respx_mock.get("http://169.254.169.254/latest/meta-data/paper.pdf").respond(
        200, content=make_pdf("cloud credentials")
    )
    result = fetch(offline, refs(entry("https://oa.example/paper.pdf")))["paper"]
    assert result.status == "failed"
    assert not metadata.called
    assert stored_files(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_host_resolving_to_the_local_network_is_refused(offline, respx_mock, dns):
    dns["papers.intranet.example"] = ["10.1.2.3"]
    route = respx_mock.get("https://papers.intranet.example/paper.pdf").respond(200, content=make_pdf("internal"))
    result = fetch(offline, refs(entry("https://papers.intranet.example/paper.pdf")))["paper"]
    assert result.status == "failed"
    assert "resolves to" in result.error
    assert not route.called
    assert stored_files(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_redirect_to_a_host_resolving_locally_is_refused(offline, respx_mock, dns):
    dns["rebind.example"] = ["127.0.0.1"]
    respx_mock.get("https://oa.example/paper.pdf").respond(
        302, headers={"location": "https://rebind.example/admin.pdf"}
    )
    local = respx_mock.get("https://rebind.example/admin.pdf").respond(200, content=make_pdf("admin"))
    result = fetch(offline, refs(entry("https://oa.example/paper.pdf")))["paper"]
    assert result.status == "failed"
    assert not local.called


def test_redirect_to_a_file_url_is_refused(offline, respx_mock, tmp_path):
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(make_pdf("local secret"))
    respx_mock.get("https://oa.example/paper.pdf").respond(302, headers={"location": secret.as_uri()})
    result = fetch(offline, refs(entry("https://oa.example/paper.pdf")))["paper"]
    assert result.status == "failed"
    assert stored_files(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_unsafe_resolver_candidate_is_skipped(settings, respx_mock):
    doi = "10.1000/unsafe"
    mock_openalex(
        respx_mock,
        doi,
        work(location("http://127.0.0.1/admin.pdf"), location("https://oa.example/ok.pdf", "acceptedVersion")),
    )
    internal = respx_mock.get("http://127.0.0.1/admin.pdf").respond(200, content=make_pdf("internal"))
    respx_mock.get("https://oa.example/ok.pdf").respond(200, content=make_pdf("ok"))
    text = f"@article{{paper, title = {{Paper}}, year = {{2020}}, doi = {{{doi}}}}}"
    result = fetch(settings, refs(text))["paper"]
    assert result.status == "downloaded"
    assert not internal.called


def test_declared_oversize_body_is_refused(project, respx_mock):
    settings = make_settings(project, resolution=no_openalex(), max_bytes=1000)
    respx_mock.get("https://oa.example/big.pdf").respond(
        200, content=b"%PDF-1.7\n" + b"0" * 5000, headers={"content-type": "application/pdf"}
    )
    result = fetch(settings, refs(entry("https://oa.example/big.pdf")))["paper"]
    assert result.status == "failed"
    assert "exceeds" in result.error
    assert stored_files(settings) == []


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.sent = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.sent += 1
            yield chunk


def test_streamed_oversize_body_is_cut_off(project, respx_mock):
    settings = make_settings(project, resolution=no_openalex(), max_bytes=10_000)
    stream = Chunks([b"%PDF-1.7\n"] + [b"0" * 4096] * 100)
    respx_mock.get("https://oa.example/endless.pdf").mock(return_value=httpx.Response(200, stream=stream))
    result = fetch(settings, refs(entry("https://oa.example/endless.pdf")))["paper"]
    assert result.status == "failed"
    assert "exceeds" in result.error
    assert stream.sent < 10
    assert stored_files(settings) == []


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize("key", ["../../escape", "..", "/etc/passwd", "C:\\Windows\\evil", "a/../../b"])
def test_hostile_keys_stay_inside_the_library(offline, respx_mock, key):
    respx_mock.get("https://oa.example/paper.pdf").respond(200, content=make_pdf("paper"))
    text = f"@misc{{{key}, title = {{Paper}}, url = {{https://oa.example/paper.pdf}}}}"
    [reference] = refs(text)
    result = fetch(offline, [reference])[reference.citation_key]
    assert result.status == "downloaded"
    stored = (offline.project_root / result.path).resolve()
    assert stored.parent == offline.library_root.resolve()
    assert len(stored_files(offline)) == 1
    outside = [path for path in offline.project_root.parent.rglob("*.pdf") if offline.library_root not in path.parents]
    assert outside == []


@pytest.mark.respx(assert_all_called=False)
def test_symlink_in_the_library_is_not_written_through(offline, respx_mock, tmp_path):
    outside = tmp_path / "outside.pdf"
    original = make_pdf("not yours")
    outside.write_bytes(original)
    offline.library_root.mkdir(parents=True)
    (offline.library_root / "paper.pdf").symlink_to(outside)
    paper = make_pdf("paper")
    respx_mock.get("https://oa.example/paper.pdf").respond(200, content=paper)
    result = fetch(offline, refs(entry("https://oa.example/paper.pdf")))["paper"]
    assert result.status == "downloaded"
    assert outside.read_bytes() == original
    assert located_bytes(offline, "paper") == paper


@pytest.mark.respx(assert_all_called=False)
def test_dangling_symlink_is_replaced_not_followed(offline, respx_mock, tmp_path):
    target = tmp_path / "should-not-exist.pdf"
    offline.library_root.mkdir(parents=True)
    (offline.library_root / "paper.pdf").symlink_to(target)
    respx_mock.get("https://oa.example/paper.pdf").respond(200, content=make_pdf("paper"))
    result = fetch(offline, refs(entry("https://oa.example/paper.pdf")))["paper"]
    assert result.status == "downloaded"
    assert not target.exists()


def test_bibtex_files_are_never_modified(project, respx_mock):
    config = write_config(project, '[resolution]\nuse_openalex = false\n')
    bib = project / "sample.bib"
    bib.write_bytes(SAMPLE_BIB.read_bytes())
    before = (bib.read_bytes(), os.stat(bib).st_mtime_ns)
    respx_mock.route().respond(404)
    cli(config, "fetch", str(bib))
    cli(config, "sync", str(bib))
    cli(config, "verify", "--references")
    assert (bib.read_bytes(), os.stat(bib).st_mtime_ns) == before


@pytest.mark.parametrize(
    "body",
    [b"<html><body>Please sign in</body></html>", b"%PDF-1.4 broken", b"\x00" * 64],
)
def test_invalid_payloads_leave_no_partial_files(offline, respx_mock, body):
    respx_mock.get("https://oa.example/paper.pdf").respond(200, content=body)
    fetch(offline, refs(entry("https://oa.example/paper.pdf")))
    assert leftovers(offline) == []
    assert stored_files(offline) == []


def test_session_cookies_are_never_persisted(offline, respx_mock, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SESSION-SECRET-9f1c"
    respx_mock.get("https://oa.example/paper.pdf").respond(
        200,
        content=make_pdf("paper"),
        headers={"set-cookie": f"session={secret}; HttpOnly", "x-amz-security-token": secret},
    )
    fetch(offline, refs(entry("https://oa.example/paper.pdf")))
    state = offline.db_path.parent
    for path in state.iterdir():
        assert secret.encode() not in path.read_bytes(), path.name
    assert secret not in caplog.text
