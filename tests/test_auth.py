"""`bibvault auth`: the OpenAlex key is typed hidden, checked, and readable by the user only."""

import stat
import tomllib

import httpx
from typer.testing import CliRunner, Result

from bibvault import config
from bibvault.cli import app
from tests.helpers import fetch, make_settings, refs, write_config

KEY = "oa-test-key-1234abcd"
RATE_LIMIT = "https://api.openalex.org/rate-limit"
BUDGET = {"rate_limit": {"daily_budget_usd": 1, "daily_remaining_usd": 0.95}}


def auth(project, *args: str, typed: str | None = None) -> Result:
    path = write_config(project)
    return CliRunner().invoke(app, ["--config", str(path), "auth", *args], input=typed)


def login(project, respx_mock, key: str = KEY) -> Result:
    respx_mock.get(RATE_LIMIT).respond(200, json=BUDGET)
    return auth(project, "login", "openalex", typed=key + "\n")


def test_login_reads_the_key_hidden_and_stores_it_for_the_user_only(project, respx_mock):
    result = login(project, respx_mock)
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    assert "…abcd" in result.output
    assert "$0.95 of $1.00 left" in result.output
    assert respx_mock.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in str(respx_mock.calls.last.request.url)

    path = config.credentials_path()
    assert tomllib.loads(path.read_text(encoding="utf-8"))["resolution"]["openalex_api_key"] == KEY
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.load_settings(project).resolution.openalex_api_key == KEY


def test_login_leaves_config_toml_alone(project, respx_mock):
    config.USER_CONFIG.parent.mkdir(parents=True)
    config.USER_CONFIG.write_text("# mine\n[browser]\ninterval_seconds = 30\n", encoding="utf-8")
    login(project, respx_mock)
    assert config.USER_CONFIG.read_text(encoding="utf-8") == "# mine\n[browser]\ninterval_seconds = 30\n"
    assert config.load_settings(project).browser.interval_seconds == 30


def test_login_replaces_the_previous_key(project, respx_mock):
    login(project, respx_mock)
    login(project, respx_mock, "second-key-9999")
    assert config.saved_openalex_key() == "second-key-9999"


def test_rejected_key_is_not_saved(project, respx_mock):
    respx_mock.get(RATE_LIMIT).respond(401)
    result = auth(project, "login", "openalex", typed="wrong-key\n")
    assert result.exit_code == 1
    assert "rejected" in result.output
    assert not config.credentials_path().exists()


def test_key_is_saved_when_openalex_cannot_be_reached(project, respx_mock):
    respx_mock.get(RATE_LIMIT).mock(side_effect=httpx.ConnectError("offline"))
    result = auth(project, "login", "openalex", typed=KEY + "\n")
    assert result.exit_code == 0, result.output
    assert "could not check" in result.output
    assert config.saved_openalex_key() == KEY


def test_pasted_text_that_is_not_a_key_is_refused(project, respx_mock):
    result = auth(project, "login", "openalex", typed="two words\n")
    assert result.exit_code == 1
    assert not respx_mock.calls
    assert not config.credentials_path().exists()


def test_environment_variable_overrides_the_saved_key(project, respx_mock, monkeypatch):
    monkeypatch.setenv("BIBVAULT_OPENALEX_API_KEY", "env-key")
    assert "overrides the saved key" in login(project, respx_mock).output
    assert config.load_settings(project).resolution.openalex_api_key == "env-key"
    assert "from BIBVAULT_OPENALEX_API_KEY" in auth(project, "status").output


def test_status_shows_only_the_end_of_the_key(project, respx_mock):
    login(project, respx_mock)
    result = auth(project, "status")
    assert "…abcd" in result.output
    assert KEY not in result.output
    assert str(config.credentials_path()) in result.output


def test_status_without_a_key(project, respx_mock):
    assert "no API key" in auth(project, "status").output
    respx_mock.get(url__regex=r"^https://api\.openalex\.org/works/").respond(200, json={})
    fetch(make_settings(project), refs("@article{a, title = {A}, year = {2020}, doi = {10.1000/a}}"))
    assert "1 of 100 requests without a key used today" in auth(project, "status").output


def test_init_explains_how_to_add_a_key(project, respx_mock):
    result = CliRunner().invoke(app, ["--config", str(write_config(project)), "init"])
    assert "https://openalex.org/settings/api" in result.output
    assert "bibvault auth login openalex" in result.output
    login(project, respx_mock)
    result = CliRunner().invoke(app, ["--config", str(write_config(project)), "init"])
    assert "auth login" not in result.output


def test_status_reports_a_spent_budget(project, respx_mock):
    respx_mock.get(url__regex=r"^https://api\.openalex\.org/works/").respond(
        429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600"}
    )
    fetch(make_settings(project), refs("@article{a, title = {A}, year = {2020}, doi = {10.1000/a}}"))
    assert "paused until" in auth(project, "status").output


def test_logout_deletes_the_saved_key(project, respx_mock):
    login(project, respx_mock)
    assert "removed the OpenAlex key" in auth(project, "logout", "openalex").output
    assert not config.credentials_path().exists()
    assert "no OpenAlex key was saved" in auth(project, "logout", "openalex").output
