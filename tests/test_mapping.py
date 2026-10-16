"""Citation-key mapping: duplicates, renames, collisions, and the manifest."""

import hashlib
import json
from pathlib import Path

import pytest

from bibvault.storage import load_manifest
from tests.helpers import (
    cli,
    fetch,
    locate,
    located_bytes,
    make_pdf,
    make_settings,
    no_openalex,
    open_library,
    reference_problems,
    refs,
    write_config,
)

DOI_X = "10.1000/x"
DOI_Y = "10.1000/y"
URL_X = "https://oa.example/x.pdf"
URL_Y = "https://oa.example/y.pdf"


def article(key: str, *, doi: str | None = None, url: str | None = None, title: str = "Shared Paper") -> str:
    fields = [f"title = {{{title}}}", "year = {2020}", "author = {Ada Lovelace}"]
    if doi:
        fields.append(f"doi = {{{doi}}}")
    if url:
        fields.append(f"url = {{{url}}}")
    return f"@article{{{key}, {', '.join(fields)}}}\n"


@pytest.fixture
def offline(project):
    return make_settings(project, resolution=no_openalex())


@pytest.fixture
def pdfs(respx_mock):
    x, y = make_pdf("paper x"), make_pdf("paper y")
    routes = {
        "x": respx_mock.get(URL_X).respond(200, content=x),
        "y": respx_mock.get(URL_Y).respond(200, content=y),
    }
    return {"x": x, "y": y, "routes": routes}


def names(settings) -> list[str]:
    return sorted(path.name for path in settings.library_root.iterdir())


def unreferenced(settings) -> list[Path]:
    folder = settings.library_root / "_unreferenced"
    return sorted(folder.iterdir()) if folder.exists() else []


@pytest.mark.respx(assert_all_called=False)
def test_locate_describes_the_file(offline, pdfs):
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    payload = locate(offline, "paper")
    assert payload["citation_key"] == "paper"
    assert payload["reference_id"] == f"doi:{DOI_X}"
    assert payload["type"] == "article"
    assert payload["title"] == "Shared Paper"
    assert payload["status"] == "downloaded"
    assert payload["version"] == "provided"
    assert payload["error"] is None
    pdf = payload["files"]["pdf"]
    assert pdf["path"] == "papers/paper.pdf"
    assert pdf["sha256"] == hashlib.sha256(pdfs["x"]).hexdigest()
    assert pdf["source_url"] == URL_X
    assert pdf["provider"] == "direct"
    assert pdf["retrieved_at"]
    assert pdf["byte_size"] == len(pdfs["x"])


def test_unknown_key_is_not_located(offline):
    assert locate(offline, "nobody") is None


@pytest.mark.respx(assert_all_called=False)
def test_manifest_lists_every_key(offline, pdfs, respx_mock):
    respx_mock.get("https://oa.example/gone.pdf").respond(404)
    text = article("paper", doi=DOI_X, url=URL_X) + article(
        "gone", doi=DOI_Y, url="https://oa.example/gone.pdf", title="Gone"
    )
    fetch(offline, refs(text))
    raw = json.loads(offline.manifest_path.read_text(encoding="utf-8"))
    assert raw["manifest_version"] == 1
    manifest = load_manifest(offline.manifest_path)
    assert set(manifest) == {"paper", "gone"}
    assert manifest["paper"] == {
        "reference_id": f"doi:{DOI_X}",
        "citation_key": "paper",
        "type": "article",
        "filename_stem": "paper",
        "files": {"pdf": "papers/paper.pdf"},
        "version": "provided",
        "status": "downloaded",
        "provenance": {
            "pdf": {
                "sha256": hashlib.sha256(pdfs["x"]).hexdigest(),
                "source_url": URL_X,
                "provider": "direct",
                "retrieved_at": manifest["paper"]["provenance"]["pdf"]["retrieved_at"],
                "version": "provided",
            }
        },
    }
    assert manifest["gone"]["files"] == {}
    assert manifest["gone"]["status"] == "not_found"


@pytest.mark.respx(assert_all_called=False)
def test_manifest_is_regenerated_from_sqlite(offline, pdfs):
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    offline.manifest_path.write_text("{ hand edited and broken", encoding="utf-8")
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    manifest = load_manifest(offline.manifest_path)
    assert manifest["paper"]["files"]["pdf"] == "papers/paper.pdf"
    assert not offline.manifest_path.with_suffix(".json.partial").exists()


@pytest.mark.respx(assert_all_called=False)
def test_two_keys_for_one_doi_share_one_download(offline, pdfs):
    text = article("delta-lake-paper", doi=DOI_X, url=URL_X) + article("delta-lake-alias", doi=DOI_X, url=URL_X)
    results = fetch(offline, refs(text))
    assert pdfs["routes"]["x"].call_count == 1
    assert sorted(item.status for item in results.values()) == ["already_exists", "downloaded"]
    assert {item.reference_id for item in results.values()} == {f"doi:{DOI_X}"}
    assert names(offline) == ["delta-lake-alias.pdf", "delta-lake-paper.pdf"]
    assert located_bytes(offline, "delta-lake-alias") == located_bytes(offline, "delta-lake-paper") == pdfs["x"]
    library = open_library(offline)
    try:
        assert library.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        library.close()
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_doi_written_as_url_is_the_same_publication(offline, pdfs):
    text = article("delta-lake-paper", doi=DOI_X, url=URL_X) + article(
        "delta-lake-alias", url=f"https://doi.org/{DOI_X.upper()}"
    )
    results = fetch(offline, refs(text))
    assert results["delta-lake-alias"].reference_id == f"doi:{DOI_X}"
    assert results["delta-lake-alias"].status == "already_exists"


@pytest.mark.respx(assert_all_called=False)
def test_duplicate_across_bib_files(offline, pdfs):
    chapter_one = refs(article("delta2020", doi=DOI_X, url=URL_X))
    chapter_two = refs(article("armbrust-delta", doi=DOI_X))
    results = fetch(offline, chapter_one + chapter_two)
    assert pdfs["routes"]["x"].call_count == 1
    assert results["armbrust-delta"].path == "papers/armbrust-delta.pdf"


@pytest.mark.respx(assert_all_called=False)
def test_concurrent_keys_for_one_doi_download_once(project, pdfs):
    settings = make_settings(project, resolution=no_openalex(), concurrency=4)
    text = "".join(article(key, doi=DOI_X, url=URL_X) for key in ("k1", "k2", "k3"))
    results = fetch(settings, refs(text))
    assert pdfs["routes"]["x"].call_count == 1
    assert sorted(item.status for item in results.values()) == ["already_exists", "already_exists", "downloaded"]
    assert reference_problems(settings) == []


@pytest.mark.respx(assert_all_called=False)
def test_renamed_key_moves_the_file(offline, pdfs):
    fetch(offline, refs(article("delta2020", doi=DOI_X, url=URL_X)))
    result = fetch(offline, refs(article("armbrust2020delta", doi=DOI_X, url=URL_X)))["armbrust2020delta"]
    assert result.status == "already_exists"
    assert result.path == "papers/armbrust2020delta.pdf"
    assert pdfs["routes"]["x"].call_count == 1
    assert names(offline) == ["armbrust2020delta.pdf"]
    assert locate(offline, "delta2020") is None
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_dropped_alias_frees_its_filename(offline, pdfs):
    fetch(offline, refs(article("a", doi=DOI_X, url=URL_X) + article("b", doi=DOI_X, url=URL_X)))
    fetch(offline, refs(article("b", doi=DOI_X, url=URL_X)))
    assert locate(offline, "a") is None
    assert names(offline) == ["b.pdf"]
    assert located_bytes(offline, "b") == pdfs["x"]

    result = fetch(offline, refs(article("b", doi=DOI_X, url=URL_X) + article("a", doi=DOI_Y, url=URL_Y)))["a"]
    assert result.path == "papers/a.pdf"
    assert located_bytes(offline, "a") == pdfs["y"]
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_swapped_keys_swap_their_files(offline, pdfs):
    fetch(offline, refs(article("a", doi=DOI_X, url=URL_X) + article("b", doi=DOI_Y, url=URL_Y, title="Other")))
    results = fetch(
        offline, refs(article("a", doi=DOI_Y, url=URL_Y, title="Other") + article("b", doi=DOI_X, url=URL_X))
    )
    assert {key: item.path for key, item in results.items()} == {"a": "papers/a.pdf", "b": "papers/b.pdf"}
    assert names(offline) == ["a.pdf", "b.pdf"]
    assert (offline.library_root / "a.pdf").read_bytes() == pdfs["y"]
    assert (offline.library_root / "b.pdf").read_bytes() == pdfs["x"]
    assert pdfs["routes"]["x"].call_count == 1
    assert pdfs["routes"]["y"].call_count == 1
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_rotated_keys_follow_their_papers(offline, pdfs, respx_mock):
    z = make_pdf("paper z")
    respx_mock.get("https://oa.example/z.pdf").respond(200, content=z)
    papers = {
        "x": (DOI_X, URL_X),
        "y": (DOI_Y, URL_Y),
        "z": ("10.1000/z", "https://oa.example/z.pdf"),
    }

    def bib(mapping):
        return "".join(
            article(key, doi=papers[p][0], url=papers[p][1], title=f"Paper {p}") for key, p in mapping.items()
        )

    fetch(offline, refs(bib({"a": "x", "b": "y", "c": "z"})))
    fetch(offline, refs(bib({"a": "y", "b": "z", "c": "x"})))
    assert names(offline) == ["a.pdf", "b.pdf", "c.pdf"]
    assert (offline.library_root / "a.pdf").read_bytes() == pdfs["y"]
    assert (offline.library_root / "b.pdf").read_bytes() == z
    assert (offline.library_root / "c.pdf").read_bytes() == pdfs["x"]
    assert len(respx_mock.calls) == 3
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_corrected_doi_moves_the_old_paper_aside(offline, pdfs):
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    result = fetch(offline, refs(article("paper", doi=DOI_Y, url=URL_Y)))["paper"]
    assert result.status == "downloaded"
    assert result.reference_id == f"doi:{DOI_Y}"
    assert result.path == "papers/paper.pdf"
    assert (offline.library_root / "paper.pdf").read_bytes() == pdfs["y"]
    [aside] = unreferenced(offline)
    assert aside.read_bytes() == pdfs["x"]
    assert aside.name.startswith("paper--")
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_pointing_back_restores_the_paper_without_downloading(offline, pdfs):
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    fetch(offline, refs(article("paper", doi=DOI_Y, url=URL_Y)))
    result = fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))["paper"]
    assert result.status == "already_exists"
    assert result.path == "papers/paper.pdf"
    assert (offline.library_root / "paper.pdf").read_bytes() == pdfs["x"]
    assert [path.read_bytes() for path in unreferenced(offline)] == [pdfs["y"]]
    assert pdfs["routes"]["x"].call_count == 1
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_annotated_paper_is_moved_not_deleted(offline, pdfs):
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    annotated = make_pdf("paper x with my notes")
    target = offline.library_root / "paper.pdf"
    target.unlink()
    target.write_bytes(annotated)
    fetch(offline, refs(article("paper", doi=DOI_Y, url=URL_Y)))
    assert target.read_bytes() == pdfs["y"]
    assert [path.read_bytes() for path in unreferenced(offline)] == [annotated]


@pytest.mark.respx(assert_all_called=False)
def test_shared_paper_stays_with_the_remaining_key(offline, pdfs):
    fetch(offline, refs(article("a", doi=DOI_X, url=URL_X) + article("c", doi=DOI_X, url=URL_X)))
    fetch(offline, refs(article("a", doi=DOI_Y, url=URL_Y, title="Other") + article("c", doi=DOI_X, url=URL_X)))
    assert names(offline) == ["a.pdf", "c.pdf"]
    assert (offline.library_root / "a.pdf").read_bytes() == pdfs["y"]
    assert located_bytes(offline, "c") == pdfs["x"]
    assert unreferenced(offline) == []
    assert reference_problems(offline) == []


def test_import_after_a_doi_correction_takes_the_filename(project, respx_mock):
    config = write_config(project, "[resolution]\nuse_openalex = false\n")
    bib = project / "refs.bib"
    old = make_pdf("wrong paper")
    respx_mock.get(URL_X).respond(200, content=old)
    bib.write_text(article("paper", doi=DOI_X, url=URL_X), encoding="utf-8")
    assert cli(config, "fetch", str(bib)).exit_code == 0

    bib.write_text(article("paper", doi=DOI_Y), encoding="utf-8")
    right = project / "right.pdf"
    right.write_bytes(make_pdf("right paper"))
    result = cli(config, "import", str(right), "--key", "paper", "--bib", str(bib))
    assert result.exit_code == 0, result.output
    assert (project / "papers" / "paper.pdf").read_bytes() == right.read_bytes()
    settings = make_settings(project)
    assert [path.read_bytes() for path in unreferenced(settings)] == [old]
    assert reference_problems(settings) == []


@pytest.mark.respx(assert_all_called=False)
def test_removed_entry_keeps_its_pdf(offline, pdfs):
    fetch(offline, refs(article("old-paper", doi=DOI_X, url=URL_X)))
    fetch(offline, refs(article("new-paper", doi=DOI_Y, url=URL_Y, title="New")))
    assert (offline.library_root / "old-paper.pdf").read_bytes() == pdfs["x"]
    assert located_bytes(offline, "old-paper") == pdfs["x"]


@pytest.mark.respx(assert_all_called=False)
def test_readded_key_reuses_its_filename(offline, pdfs):
    paper = article("paper", doi=DOI_X, url=URL_X)
    other = article("other", doi=DOI_Y, url=URL_Y, title="Other")
    fetch(offline, refs(paper))
    fetch(offline, refs(other))
    result = fetch(offline, refs(paper + other))["paper"]
    assert result.path == "papers/paper.pdf"
    assert pdfs["routes"]["x"].call_count == 1


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize(
    ("first", "second"),
    [("Smith2020", "smith2020"), ("a:b", "a_b"), ("x/y", "x_y"), ("湖仓:2024", "湖仓_2024")],
)
def test_keys_that_map_to_the_same_filename_stay_apart(offline, pdfs, first, second):
    text = article(first, doi=DOI_X, url=URL_X) + article(second, doi=DOI_Y, url=URL_Y, title="Other")
    results = fetch(offline, refs(text))
    assert results[first].path.casefold() != results[second].path.casefold()
    assert located_bytes(offline, first) == pdfs["x"]
    assert located_bytes(offline, second) == pdfs["y"]
    assert reference_problems(offline) == []


@pytest.mark.respx(assert_all_called=False)
def test_unmanaged_file_with_the_same_name_is_left_alone(offline, pdfs):
    mine = make_pdf("my own notes")
    offline.library_root.mkdir(parents=True)
    (offline.library_root / "paper.pdf").write_bytes(mine)
    result = fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))["paper"]
    assert result.status == "downloaded"
    assert result.path.startswith("papers/paper--")
    assert (offline.library_root / "paper.pdf").read_bytes() == mine
    assert located_bytes(offline, "paper") == pdfs["x"]


@pytest.mark.respx(assert_all_called=False)
def test_chinese_keys_get_readable_filenames(offline, pdfs):
    text = article("张三2020", doi=DOI_X, url=URL_X) + article("李四2020", doi=DOI_Y, url=URL_Y, title="Other")
    results = fetch(offline, refs(text))
    assert results["张三2020"].path == "papers/张三2020.pdf"
    assert results["李四2020"].path == "papers/李四2020.pdf"
    assert located_bytes(offline, "张三2020") == pdfs["x"]
    manifest = load_manifest(offline.manifest_path)
    assert manifest["张三2020"]["files"]["pdf"] == "papers/张三2020.pdf"
    assert reference_problems(offline) == []


def test_cli_prints_chinese_paths(project, respx_mock):
    config = write_config(project, "[resolution]\nuse_openalex = false\n")
    bib = project / "refs.bib"
    bib.write_text(article("王小明:湖仓综述2024", doi=DOI_X, url=URL_X), encoding="utf-8")
    respx_mock.get(URL_X).respond(200, content=make_pdf("x"))
    assert cli(config, "fetch", str(bib)).exit_code == 0
    located = cli(config, "locate", "王小明:湖仓综述2024")
    assert located.exit_code == 0
    assert located.stdout.splitlines()[0] == "papers/王小明_湖仓综述2024.pdf"


@pytest.mark.respx(assert_all_called=False)
def test_original_citation_key_is_preserved(offline, pdfs):
    fetch(offline, refs(article("smith:2020", doi=DOI_X, url=URL_X)))
    payload = locate(offline, "smith:2020")
    assert payload["citation_key"] == "smith:2020"
    assert payload["files"]["pdf"]["path"] == "papers/smith_2020.pdf"
    manifest = load_manifest(offline.manifest_path)
    assert manifest["smith:2020"]["filename_stem"] == "smith_2020"


@pytest.mark.respx(assert_all_called=False)
def test_replaced_alias_never_serves_unrelated_bytes(offline, pdfs):
    text = article("a", doi=DOI_X, url=URL_X) + article("b", doi=DOI_X, url=URL_X)
    fetch(offline, refs(text))
    alias = offline.library_root / "b.pdf"
    alias.unlink()
    alias.write_bytes(make_pdf("something else entirely"))
    results = fetch(offline, refs(text))
    assert {item.status for item in results.values()} == {"already_exists"}
    assert located_bytes(offline, "b") == pdfs["x"]
    assert located_bytes(offline, "a") == pdfs["x"]


@pytest.mark.respx(assert_all_called=False)
def test_library_outside_the_project(project, tmp_path, pdfs):
    shared = tmp_path / "shared-library"
    settings = make_settings(project, library_root=shared, resolution=no_openalex())
    result = fetch(settings, refs(article("paper", doi=DOI_X, url=URL_X)))["paper"]
    assert result.status == "downloaded"
    assert result.path == (shared / "paper.pdf").as_posix()
    assert located_bytes(settings, "paper") == pdfs["x"]


@pytest.mark.respx(assert_all_called=False)
def test_index_survives_reopening(offline, pdfs):
    fetch(offline, refs(article("paper", doi=DOI_X, url=URL_X)))
    for _ in range(2):
        open_library(offline).close()
    assert located_bytes(offline, "paper") == pdfs["x"]


def test_entries_without_identifiers_get_a_stable_internal_identity(offline):
    text = "@book{knuth-a, title = {The Art of Computer Programming}, author = {Donald Knuth}, year = {1968}}\n"
    first = fetch(offline, refs(text))["knuth-a"]
    second = fetch(offline, refs(text.replace("knuth-a", "taocp")))["taocp"]
    assert first.reference_id.startswith("internal:")
    assert first.reference_id == second.reference_id
    assert first.status == "not_found"
