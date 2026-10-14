"""Papers that only a real browser can download: ACM's behind its bot check, and
IEEE and Springer papers through the library proxy.

No test starts a browser. ``FakeBrowser`` plays its part: when BibVault opens a
link, it saves a prepared file into BibVault's download folder, as the
configured browser profile would.
"""

import json
import stat

import httpx
import pytest

from bibvault import browser, config
from bibvault.browser import browser_home
from bibvault.config import BrowserFile, load_settings
from tests.helpers import (
    PDF,
    cli,
    fetch,
    locate,
    location,
    make_pdf,
    make_settings,
    mock_openalex,
    refs,
    work,
    write_config,
)

DOI = "10.1145/3589306"
TITLE = "Cloud Functions as Accelerator"
PUBLISHER_PDF = f"https://dl.acm.org/doi/pdf/{DOI}"
BROWSER_URL = f"{PUBLISHER_PDF}?download=true"
BOT_CHECK = {"content-type": "text/html", "cf-mitigated": "challenge"}


def acm(key: str = "cloud-functions", doi: str = DOI, title: str = TITLE) -> str:
    return f"@article{{{key}, title = {{{title}}}, year = {{2023}}, doi = {{{doi}}}}}\n"


def bot_checked(respx_mock, doi: str = DOI) -> None:
    pdf = f"https://dl.acm.org/doi/pdf/{doi}"
    mock_openalex(respx_mock, doi, work(location(pdf)))
    respx_mock.get(pdf).respond(403, content=b"<html>Just a moment...</html>", headers=BOT_CHECK)


def inbox():
    return browser_home() / "inbox"


class FakeBrowser:
    def __init__(self, files: dict[str, tuple[str, bytes]] | None = None) -> None:
        self.files = files or {}
        self.opened: list[str] = []

    def __call__(self, url: str) -> None:
        self.opened.append(url)
        if url in self.files:
            name, data = self.files[url]
            (inbox() / name).write_bytes(data)


def saves(url: str = BROWSER_URL, title: str = TITLE, name: str = "3589306.pdf") -> dict:
    return {url: (name, make_pdf(title))}


def test_bot_checked_paper_arrives_through_the_browser(settings, respx_mock):
    bot_checked(respx_mock)
    fake = FakeBrowser(saves())
    result = fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"]
    assert result.status == "downloaded"
    assert result.path == "papers/cloud-functions.pdf"
    assert result.provider == "acm"
    assert result.version == "published"
    assert fake.opened == ["https://dl.acm.org/", BROWSER_URL]
    assert list(inbox().iterdir()) == []
    assert locate(settings, "cloud-functions")["files"]["pdf"]["source_url"] == BROWSER_URL


def test_nothing_is_opened_without_the_browser_option(settings, respx_mock):
    bot_checked(respx_mock)
    fake = FakeBrowser(saves())
    result = fetch(settings, refs(acm()), open_url=fake)["cloud-functions"]
    assert result.status == "manual_action_required"
    assert fake.opened == []


def test_paper_without_an_open_copy_in_openalex_is_tried(settings, respx_mock):
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    fake = FakeBrowser(saves())
    assert fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"].status == "downloaded"


def with_preprint(respx_mock):
    """OpenAlex lists the bot-checked ACM PDF and an arXiv preprint."""

    preprint = "https://arxiv.org/pdf/2301.00001"
    mock_openalex(respx_mock, DOI, work(location(PUBLISHER_PDF), location(preprint, "submittedVersion")))
    respx_mock.get(PUBLISHER_PDF).respond(403, content=b"<html>Just a moment...</html>", headers=BOT_CHECK)
    return respx_mock.get(preprint).respond(200, content=make_pdf(f"{TITLE} preprint"), headers=PDF)


def test_browser_gets_the_publisher_pdf_before_the_preprint_is_taken(settings, respx_mock):
    preprint = with_preprint(respx_mock)
    fake = FakeBrowser(saves())
    result = fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"]
    assert result.status == "downloaded"
    assert result.provider == "acm"
    assert result.version == "published"
    assert preprint.call_count == 0


def test_preprint_is_taken_when_the_browser_gets_nothing(settings, respx_mock):
    preprint = with_preprint(respx_mock)
    fake = FakeBrowser()
    result = fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"]
    assert fake.opened == ["https://dl.acm.org/", BROWSER_URL]
    assert result.status == "downloaded"
    assert result.version == "preprint"
    assert preprint.call_count == 1


def test_papers_the_browser_saved_nothing_for_in_a_row_keep_waiting(settings, respx_mock):
    preprints = []
    for index, doi in enumerate(["10.1145/1", "10.1145/2"]):
        publisher = f"https://dl.acm.org/doi/pdf/{doi}"
        preprint = f"https://arxiv.org/pdf/2301.0000{index}"
        mock_openalex(respx_mock, doi, work(location(publisher), location(preprint, "submittedVersion")))
        respx_mock.get(publisher).respond(403, content=b"<html>Just a moment...</html>", headers=BOT_CHECK)
        preprints.append(respx_mock.get(preprint).respond(200, content=make_pdf(f"Paper {index}"), headers=PDF))
    text = acm("paper0", "10.1145/1", "Paper 0") + acm("paper1", "10.1145/2", "Paper 1")
    results = fetch(settings, refs(text), browser=True, open_url=FakeBrowser())
    assert [results[key].status for key in ("paper0", "paper1")] == ["manual_action_required"] * 2
    assert "did not save" in results["paper0"].error
    assert [route.call_count for route in preprints] == [0, 0]


def test_without_the_browser_option_the_preprint_is_taken_directly(settings, respx_mock):
    preprint = with_preprint(respx_mock)
    fake = FakeBrowser(saves())
    result = fetch(settings, refs(acm()), open_url=fake)["cloud-functions"]
    assert result.version == "preprint"
    assert preprint.call_count == 1
    assert fake.opened == []


def test_preprint_of_a_paper_the_browser_did_not_reach_waits_for_the_next_run(project, respx_mock):
    settings = make_settings(project)
    settings.browser = BrowserFile(max_per_run=0)
    preprint = with_preprint(respx_mock)
    result = fetch(settings, refs(acm()), browser=True, open_url=FakeBrowser(saves()))["cloud-functions"]
    assert result.status == "manual_action_required"
    assert "run again with --browser" in result.error
    assert preprint.call_count == 0


def test_only_missing_acm_papers_are_opened(settings, respx_mock):
    bot_checked(respx_mock)
    open_doi, ieee_doi = "10.1145/3183713.3196927", "10.1109/icde53745.2022.00276"
    mock_openalex(respx_mock, open_doi, work(location("https://repository.example.edu/dostoevsky.pdf")))
    respx_mock.get("https://repository.example.edu/dostoevsky.pdf").respond(200, content=make_pdf("Dostoevsky"), headers=PDF)
    mock_openalex(respx_mock, ieee_doi, work(is_oa=False))
    text = acm() + acm("dostoevsky18", open_doi, "Dostoevsky") + acm("ieee-paper", ieee_doi, "IEEE Paper")
    fake = FakeBrowser(saves())
    results = fetch(settings, refs(text), browser=True, open_url=fake)
    assert fake.opened == ["https://dl.acm.org/", BROWSER_URL]
    assert results["ieee-paper"].status == "no_open_access_found"


def test_two_keys_for_one_paper_open_it_once(settings, respx_mock):
    bot_checked(respx_mock)
    fake = FakeBrowser(saves())
    results = fetch(settings, refs(acm() + acm("cloud-alias")), browser=True, open_url=fake)
    assert fake.opened == ["https://dl.acm.org/", BROWSER_URL]
    assert sorted(item.status for item in results.values()) == ["already_exists", "downloaded"]
    assert (settings.library_root / "cloud-alias.pdf").read_bytes() == (settings.library_root / "cloud-functions.pdf").read_bytes()


def test_browser_that_saves_nothing_twice_stops_the_pass(settings, respx_mock):
    dois = ["10.1145/1", "10.1145/2", "10.1145/3"]
    for doi in dois:
        mock_openalex(respx_mock, doi, work(is_oa=False))
    fake = FakeBrowser()
    text = "".join(acm(f"paper{index}", doi, f"Paper {index}") for index, doi in enumerate(dois))
    results = fetch(settings, refs(text), browser=True, open_url=fake)
    assert len(fake.opened) == 3  # sign-in page, then two papers that save nothing
    assert results["paper0"].status == "manual_action_required"
    assert "did not save" in results["paper0"].error
    assert results["paper2"].status == "no_open_access_found"


def test_saved_page_that_is_not_a_pdf_is_discarded(settings, respx_mock):
    bot_checked(respx_mock)
    fake = FakeBrowser({BROWSER_URL: ("3589306.pdf", b"<!doctype html><html><body>Error</body></html>")})
    result = fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"]
    assert result.status == "failed"
    assert list(inbox().iterdir()) == []
    assert not (settings.library_root / "cloud-functions.pdf").exists()


def test_pdf_of_another_paper_is_left_for_the_user(settings, respx_mock):
    bot_checked(respx_mock)
    fake = FakeBrowser(saves(title="Completely Unrelated Survey"))
    result = fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"]
    assert result.status == "manual_action_required"
    assert (inbox() / "3589306.pdf").exists()
    assert not (settings.library_root / "cloud-functions.pdf").exists()


def test_files_already_in_the_folder_are_not_taken(settings, respx_mock):
    bot_checked(respx_mock)
    inbox().mkdir(parents=True)
    (inbox() / "old.pdf").write_bytes(make_pdf(TITLE))
    fake = FakeBrowser()
    result = fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"]
    assert result.status == "manual_action_required"
    assert (inbox() / "old.pdf").exists()


def test_unfinished_download_is_not_taken(settings, respx_mock):
    bot_checked(respx_mock)
    fake = FakeBrowser({BROWSER_URL: ("3589306.pdf.crdownload", make_pdf(TITLE))})
    assert fetch(settings, refs(acm()), browser=True, open_url=fake)["cloud-functions"].status == "manual_action_required"


PROXY = "ifhfzfd70b582c88e4556sbqqffox9nqok6unvfaag.libproxy.ruc.edu.cn"
IEEE_DOI = "10.1109/icde.2019.00196"
PRESTO = "Presto: SQL on Everything"
PRESTO_URL = f"https://{PROXY}/stampPDF/getPDF.jsp?tp=&arnumber=8731547"


def through_proxy(settings, **hosts: str) -> None:
    settings.browser = BrowserFile(proxy_hosts={"ieeexplore.ieee.org": PROXY, **hosts})


def ieee_paper(respx_mock, location: str | None = "https://ieeexplore.ieee.org/document/8731547/"):
    mock_openalex(respx_mock, IEEE_DOI, work(is_oa=False))
    route = respx_mock.get(f"https://doi.org/{IEEE_DOI}")
    return route.respond(302, headers={"location": location}) if location else route.respond(404)


def test_ieee_paper_opens_through_the_library_proxy(settings, respx_mock):
    through_proxy(settings)
    ieee_paper(respx_mock)
    fake = FakeBrowser(saves(PRESTO_URL, PRESTO, "getPDF.pdf"))
    result = fetch(settings, refs(acm("presto-paper", IEEE_DOI, PRESTO)), browser=True, open_url=fake)["presto-paper"]
    assert result.status == "downloaded"
    assert result.provider == "ieee"
    assert fake.opened == ["https://libproxy.ruc.edu.cn/ermsClient/browse.do", PRESTO_URL]
    assert locate(settings, "presto-paper")["files"]["pdf"]["source_url"] == PRESTO_URL


def test_ieee_document_number_is_looked_up_once(settings, respx_mock):
    through_proxy(settings)
    lookup = ieee_paper(respx_mock)
    text = refs(acm("presto-paper", IEEE_DOI, PRESTO))
    first, second = FakeBrowser(), FakeBrowser()
    fetch(settings, text, browser=True, open_url=first)
    fetch(settings, text, browser=True, open_url=second)
    portal = "https://libproxy.ruc.edu.cn/ermsClient/browse.do"
    assert first.opened == second.opened == [portal, PRESTO_URL]
    assert lookup.call_count == 1


def test_ieee_paper_whose_doi_leads_elsewhere_is_not_opened(settings, respx_mock):
    through_proxy(settings)
    ieee_paper(respx_mock, location=None)
    fake = FakeBrowser()
    result = fetch(settings, refs(acm("presto-paper", IEEE_DOI, PRESTO)), browser=True, open_url=fake)["presto-paper"]
    assert result.status == "no_open_access_found"
    assert fake.opened == []


def test_library_sign_in_happens_once_for_ieee_and_springer(settings, respx_mock):
    through_proxy(settings, **{"link.springer.com": "springer.libproxy.ruc.edu.cn"})
    ieee_paper(respx_mock)
    springer_doi = "10.1007/s002360050048"
    springer_url = f"https://springer.libproxy.ruc.edu.cn/content/pdf/{springer_doi}.pdf"
    mock_openalex(respx_mock, springer_doi, work(is_oa=False))
    files = {**saves(PRESTO_URL, PRESTO, "presto.pdf"), **saves(springer_url, "The Log-Structured Merge-Tree", "lsm.pdf")}
    fake = FakeBrowser(files)
    text = acm("presto-paper", IEEE_DOI, PRESTO) + acm("lsm-tree96", springer_doi, "The Log-Structured Merge-Tree")
    prompts: list[str] = []

    def press_enter(message: str) -> str:
        prompts.append(message)
        return "\n"

    fetch(settings, refs(text), browser=True, open_url=fake, confirm=press_enter)
    assert [prompt.split(" from ")[0] for prompt in prompts[:2]] == ["Open IEEE Xplore", "Open SpringerLink"]
    assert "Close the BibVault browser window" in prompts[2]
    assert fake.opened[0] == "https://libproxy.ruc.edu.cn/ermsClient/browse.do"
    assert fake.opened[1:] == [PRESTO_URL, springer_url]


def test_pasted_proxy_address_replaces_the_configured_host(settings, respx_mock):
    through_proxy(settings)
    ieee_paper(respx_mock)
    current = "gfhfzfd70b582c88e4556swfckpk5vnowb6wuufaag.libproxy.ruc.edu.cn"
    url = f"https://{current}/stampPDF/getPDF.jsp?tp=&arnumber=8731547"
    fake = FakeBrowser(saves(url, PRESTO, "getPDF.pdf"))
    result = fetch(
        settings,
        refs(acm("presto-paper", IEEE_DOI, PRESTO)),
        browser=True,
        open_url=fake,
        confirm=lambda _message: f"https://{current}/Xplore/home.jsp\n",
    )["presto-paper"]
    assert result.status == "downloaded"
    assert fake.opened[1:] == [url]
    assert locate(settings, "presto-paper")["files"]["pdf"]["source_url"] == url


def test_springer_paper_opens_through_the_library_proxy(settings, respx_mock):
    through_proxy(settings, **{"link.springer.com": "springer.libproxy.ruc.edu.cn"})
    doi = "10.1007/s002360050048"
    url = f"https://springer.libproxy.ruc.edu.cn/content/pdf/{doi}.pdf"
    mock_openalex(respx_mock, doi, work(is_oa=False))
    fake = FakeBrowser(saves(url, "The Log-Structured Merge-Tree", "lsm.pdf"))
    result = fetch(settings, refs(acm("lsm-tree96", doi, "The Log-Structured Merge-Tree")), browser=True, open_url=fake)["lsm-tree96"]
    assert result.status == "downloaded"
    assert result.provider == "springer"
    assert fake.opened == ["https://libproxy.ruc.edu.cn/ermsClient/browse.do", url]


SPRINGER_DOI = "10.1007/s002360050048"
LSM = "The Log-Structured Merge-Tree"
SPRINGER_PROXY = "springer.libproxy.ruc.edu.cn"
PORTAL = "https://libproxy.ruc.edu.cn/ermsClient/browse.do"


def answers(**by_publisher: str):
    """Answer each publisher's address prompt; Enter for everything else."""

    prompts: list[str] = []

    def confirm(message: str) -> str:
        prompts.append(message)
        return next((answer for label, answer in by_publisher.items() if message.startswith(f"Open {label} ")), "\n")

    confirm.prompts = prompts  # type: ignore[attr-defined]
    return confirm


def test_springer_address_can_be_pasted_when_only_ieee_is_configured(settings, respx_mock):
    through_proxy(settings)
    mock_openalex(respx_mock, SPRINGER_DOI, work(is_oa=False))
    url = f"https://{SPRINGER_PROXY}/content/pdf/{SPRINGER_DOI}.pdf"
    fake = FakeBrowser(saves(url, LSM, "lsm.pdf"))
    confirm = answers(SpringerLink=f"https://{SPRINGER_PROXY}/journal/236\n")
    result = fetch(settings, refs(acm("lsm-tree96", SPRINGER_DOI, LSM)), browser=True, open_url=fake, confirm=confirm)["lsm-tree96"]
    assert result.status == "downloaded"
    assert fake.opened == [PORTAL, url]
    assert "Enter skips SpringerLink this run" in confirm.prompts[0]


def test_pasted_address_is_saved_to_the_user_config_for_next_time(project, settings, respx_mock):
    config.USER_CONFIG.parent.mkdir(parents=True)
    config.USER_CONFIG.write_text(f'# mine\n[browser.proxy_hosts]\n"ieeexplore.ieee.org" = "{PROXY}"\n', encoding="utf-8")
    through_proxy(settings)
    mock_openalex(respx_mock, SPRINGER_DOI, work(is_oa=False))
    fetch(settings, refs(acm("lsm-tree96", SPRINGER_DOI, LSM)), browser=True, open_url=FakeBrowser(), confirm=answers(SpringerLink=SPRINGER_PROXY))
    assert config.USER_CONFIG.read_text(encoding="utf-8").startswith("# mine\n")
    saved = load_settings(config_path=write_config(project)).browser.proxy_hosts
    assert saved == {"ieeexplore.ieee.org": PROXY, "link.springer.com": SPRINGER_PROXY}


def test_enter_keeps_the_config_untouched(settings, respx_mock):
    through_proxy(settings)
    ieee_paper(respx_mock)
    fetch(settings, refs(acm("presto-paper", IEEE_DOI, PRESTO)), browser=True, open_url=FakeBrowser(), confirm=answers())
    assert not config.USER_CONFIG.exists()


def test_answer_that_is_not_an_address_is_neither_used_nor_saved(settings, respx_mock):
    through_proxy(settings)
    ieee_paper(respx_mock)
    fake = FakeBrowser()
    fetch(settings, refs(acm("presto-paper", IEEE_DOI, PRESTO)), browser=True, open_url=fake, confirm=answers(**{"IEEE Xplore": "y\n"}))
    assert fake.opened == [PORTAL, PRESTO_URL]
    assert not config.USER_CONFIG.exists()


def test_springer_paper_without_an_address_is_not_opened(settings, respx_mock):
    through_proxy(settings)
    mock_openalex(respx_mock, SPRINGER_DOI, work(is_oa=False))
    fake = FakeBrowser()
    result = fetch(settings, refs(acm("lsm-tree96", SPRINGER_DOI, LSM)), browser=True, open_url=fake, confirm=answers())["lsm-tree96"]
    assert result.status == "manual_action_required"
    assert "paste SpringerLink's address" in (result.error or "")
    assert fake.opened == [PORTAL]


@pytest.mark.parametrize("doi", [SPRINGER_DOI, IEEE_DOI])
def test_without_any_library_proxy_ieee_and_springer_are_not_opened(settings, respx_mock, doi):
    mock_openalex(respx_mock, doi, work(is_oa=False))
    fake = FakeBrowser()
    result = fetch(settings, refs(acm("paper", doi, LSM)), browser=True, open_url=fake)["paper"]
    assert result.status == "no_open_access_found"
    assert fake.opened == []


def test_proxy_addresses_copied_from_the_address_bar_are_reduced_to_hosts(project):
    config = write_config(
        project, f'[browser.proxy_hosts]\n"https://IEEEXplore.ieee.org/" = "https://{PROXY}/document/8731547"\n'
    )
    assert load_settings(config_path=config).browser.proxy_hosts == {"ieeexplore.ieee.org": PROXY}


def test_browser_pass_is_paced_and_capped(project, respx_mock, sleeps, caplog):
    settings = make_settings(project)
    settings.browser = BrowserFile(interval_seconds=15, max_per_run=2)
    dois = ["10.1145/1", "10.1145/2", "10.1145/3"]
    files = {}
    for index, doi in enumerate(dois):
        mock_openalex(respx_mock, doi, work(is_oa=False))
        files.update(saves(f"https://dl.acm.org/doi/pdf/{doi}?download=true", f"Paper {index}", f"{index}.pdf"))
    fake = FakeBrowser(files)
    text = "".join(acm(f"paper{index}", doi, f"Paper {index}") for index, doi in enumerate(dois))
    results = fetch(settings, refs(text), browser=True, open_url=fake)
    assert len(fake.opened) == 3  # sign-in + two capped downloads
    assert [results[f"paper{index}"].status for index in range(3)] == ["downloaded", "downloaded", "no_open_access_found"]
    assert any(14 < delay <= 15 for delay in sleeps)
    assert "1 papers are left for the next run" in caplog.text


def test_profile_download_settings_are_restored_on_every_run(settings, respx_mock):
    bot_checked(respx_mock)
    fetch(settings, refs(acm()), browser=True, open_url=FakeBrowser(saves()))
    preferences = browser_home() / "profile" / "Default" / "Preferences"
    written = json.loads(preferences.read_text(encoding="utf-8"))
    assert written["download"]["default_directory"] == str(inbox())
    assert written["download"]["prompt_for_download"] is False
    assert written["plugins"]["always_open_pdf_externally"] is True
    assert stat.S_IMODE(browser_home().stat().st_mode) == 0o700

    preferences.write_text('{"edited": true, "download": {"prompt_for_download": true}}', encoding="utf-8")
    fetch(settings, refs(acm("other", "10.1145/9")), browser=True, open_url=FakeBrowser())
    restored = json.loads(preferences.read_text(encoding="utf-8"))
    assert restored["edited"] is True
    assert restored["download"]["prompt_for_download"] is False
    assert restored["download"]["default_directory"] == str(inbox())


@pytest.fixture
def launches(monkeypatch):
    calls: list[list[str]] = []
    files = saves()

    def popen(args, **kwargs):
        calls.append(list(args))
        url = args[-1]
        if url in files:
            name, data = files[url]
            (inbox() / name).write_bytes(data)

    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    return calls


def test_browser_is_started_plainly_on_its_own_profile(settings, respx_mock, launches):
    settings.browser = BrowserFile(executable="/Applications/Edge/edge")
    bot_checked(respx_mock)
    assert fetch(settings, refs(acm()), browser=True)["cloud-functions"].status == "downloaded"
    assert [call[-1] for call in launches] == ["https://dl.acm.org/", BROWSER_URL]
    for args in launches:
        assert args[0] == "/Applications/Edge/edge"
        assert f"--user-data-dir={browser_home() / 'profile'}" in args
        assert "--disable-sync" in args
        assert not any(flag.startswith(("--remote-debugging", "--enable-automation", "--headless")) for flag in args)


def test_focus_goes_back_to_the_app_the_user_was_in(settings, respx_mock, launches, monkeypatch):
    settings.browser = BrowserFile(executable="/Applications/Edge/edge")
    bot_checked(respx_mock)
    editor, edge = ("ASN:1", "com.example.editor"), ("ASN:2", "com.microsoft.edgemac")
    fronts = iter([editor, editor, edge])
    monkeypatch.setattr(browser, "_frontmost", lambda: next(fronts))
    monkeypatch.setattr(browser, "_MACOS", True)
    ran: list[list[str]] = []
    monkeypatch.setattr(browser.subprocess, "run", lambda args, **_kwargs: ran.append(list(args)))
    assert fetch(settings, refs(acm()), browser=True)["cloud-functions"].status == "downloaded"
    assert ran == [["open", "-b", "com.example.editor"]]


def test_library_papers_download_silently_with_profile_cookies(settings, respx_mock, monkeypatch):
    through_proxy(settings)
    ieee_paper(respx_mock)
    respx_mock.get(PRESTO_URL).respond(200, content=make_pdf(PRESTO), headers=PDF)
    cookies = httpx.Cookies()
    cookies.set("session", "1", domain="libproxy.ruc.edu.cn", path="/")
    monkeypatch.setattr(browser.BrowserDownloads, "profile_cookies", lambda self: cookies)
    fake = FakeBrowser()
    result = fetch(settings, refs(acm("presto-paper", IEEE_DOI, PRESTO)), browser=True, open_url=fake)["presto-paper"]
    assert result.status == "downloaded"
    assert result.provider == "ieee"
    assert fake.opened == ["https://libproxy.ruc.edu.cn/ermsClient/browse.do"]
    assert locate(settings, "presto-paper")["files"]["pdf"]["source_url"] == PRESTO_URL


def test_without_an_installed_browser_statuses_stay(settings, respx_mock, launches):
    bot_checked(respx_mock)
    assert fetch(settings, refs(acm()), browser=True)["cloud-functions"].status == "manual_action_required"
    assert launches == []


def test_cli_browser_option(project, respx_mock, launches):
    config = write_config(project, '[browser]\nexecutable = "/Applications/Edge/edge"\n')
    bib = project / "references.bib"
    bib.write_text(acm(), encoding="utf-8")
    bot_checked(respx_mock)
    result = cli(config, "fetch", str(bib), "--browser")
    assert result.exit_code == 0, result.output
    assert "Newly downloaded:     1" in result.output
    assert len(launches) == 2  # sign-in page, then the PDF
    # CliRunner has no stdin for Enter; confirm swallows EOFError and continues.
