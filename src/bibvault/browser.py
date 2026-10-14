"""Downloads from sites that only answer real browsers.

dl.acm.org sits behind a Cloudflare bot check that a browser under automation
does not pass. BibVault therefore drives nothing: it starts an ordinary
Chromium-family browser on a profile of its own, whose download settings it
writes once, opens the PDF link as a user would, and picks the saved file up
from that profile's download folder. Logins and verification stay with the user.

Subscription papers on IEEE Xplore and Springer are signed in through the
library proxy in that same browser; afterwards BibVault reads the session
cookies from the profile and downloads those PDFs over HTTP without opening
each link. ACM stays on the browser path; on macOS the app the user was in
gets focus back right after each link opens.
"""

import asyncio
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bibvault import config
from bibvault.config import Settings
from bibvault.models import Reference
from bibvault.pdfcheck import first_page_text
from bibvault.storage import Library

logger = logging.getLogger(__name__)

_INSTALLED = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_COMMANDS = ("microsoft-edge", "google-chrome", "chromium", "chromium-browser", "brave-browser")
_POLL_SECONDS = 0.5
_MACOS = sys.platform == "darwin"
# The browser takes the front about 200 ms after a link is handed to it.
_FOCUS_POLLS, _FOCUS_POLL_SECONDS = 30, 0.1
_IEEE_DOCUMENT = re.compile(r"//ieeexplore\.ieee\.org/document/(\d+)")
# Proxy PDF endpoints expect a real browser User-Agent; "bibvault/…" alone is often turned away.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_SAFE_STORAGE = (
    ("Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge"),
    ("Google Chrome", "Chrome Safe Storage", "Chrome"),
    ("Chromium", "Chromium Safe Storage", "Chromium"),
    ("Brave Browser", "Brave Safe Storage", "Brave"),
)


def auth_group(provider: str) -> str:
    """Providers that share one sign-in (the library proxy) are one group."""

    if provider in {"ieee", "springer"}:
        return "library"
    return provider


def sign_in_url(group: str, settings: Settings) -> str | None:
    """A page to open before downloading, so the user can sign in or pass a check."""

    if group == "acm":
        return "https://dl.acm.org/"
    if group == "library":
        return _proxy_portal(settings.browser.proxy_hosts)
    return None


def sign_in_label(group: str) -> str:
    if group == "acm":
        return "ACM Digital Library (complete any login or bot check)"
    if group == "library":
        return "your library proxy (sign in if asked)"
    return group


def publisher_label(provider: str) -> str:
    return {"ieee": "IEEE Xplore", "springer": "SpringerLink"}.get(provider, provider)


def _proxy_portal(proxy_hosts: dict[str, str]) -> str | None:
    for host in proxy_hosts.values():
        parts = host.split(".")
        if "libproxy" in parts:
            portal = ".".join(parts[parts.index("libproxy") :])
            return f"https://{portal}/ermsClient/browse.do"
        return f"https://{host}/"
    return None


PUBLISHER_HOSTS = {"ieee": "ieeexplore.ieee.org", "springer": "link.springer.com"}


def _browser_provider(doi: str, proxies: dict[str, str]) -> str | None:
    if doi.startswith("10.1145/"):
        return "acm"
    # Any configured proxy host gives the library portal; a publisher's own host can be pasted at sign-in.
    if not proxies:
        return None
    if doi.startswith("10.1007/"):
        return "springer"
    if doi.startswith("10.1109/"):
        return "ieee"
    return None


def opens_in_browser(reference: Reference, settings: Settings) -> bool:
    """Whether ``--browser`` can try the publisher's own PDF of this paper."""

    return _browser_provider(reference.doi or "", settings.browser.proxy_hosts) is not None


async def browser_link(
    reference: Reference, settings: Settings, client: httpx.AsyncClient, library: Library
) -> tuple[str, str] | None:
    """The publisher and the link to open in the browser, for papers that need one.

    IEEE and Springer papers are opened only through the library proxy; the link
    keeps the publisher's own host when ``[browser.proxy_hosts]`` has none for it.
    """

    doi = reference.doi or ""
    proxies = settings.browser.proxy_hosts
    provider = _browser_provider(doi, proxies)
    if provider == "acm":
        url = f"https://dl.acm.org/doi/pdf/{doi}?download=true"
    elif provider == "springer":
        url = f"https://link.springer.com/content/pdf/{doi}.pdf"
    elif provider == "ieee":
        number = await _ieee_document(doi, client, library)
        if number is None:
            return None
        url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={number}"
    else:
        return None
    parts = urlparse(url)
    proxy = proxies.get(parts.hostname or "")
    return provider, (parts._replace(netloc=proxy).geturl() if proxy else url)


async def _ieee_document(doi: str, client: httpx.AsyncClient, library: Library) -> str | None:
    """IEEE Xplore addresses a paper by its own document number, which doi.org redirects to."""

    cache_key = f"ieee:document:{doi}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    try:
        response = await client.get(f"https://doi.org/{doi}", follow_redirects=False)
    except httpx.HTTPError as exc:
        logger.warning("doi.org did not answer for %s: %s", doi, exc.__class__.__name__)
        return None
    match = _IEEE_DOCUMENT.search(response.headers.get("location", ""))
    if match is None:
        logger.warning("doi.org does not lead %s to an IEEE Xplore document", doi)
        return None
    library.cache_put(cache_key, match.group(1))
    return match.group(1)


def browser_home() -> Path:
    return config.USER_CONFIG.parent / "browser"


def find_browser(configured: str | None) -> str | None:
    if configured:
        return configured
    for path in _INSTALLED:
        if Path(path).is_file():
            return path
    for name in _COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


def matches_title(path: Path, title: str | None) -> bool:
    if not title:
        return True
    wanted = set(re.findall(r"\w+", title.lower()))
    found = set(re.findall(r"\w+", first_page_text(path).lower()))
    return len(wanted & found) >= 0.8 * len(wanted)


def application_bundle(executable: str) -> Path | None:
    """``…/App.app/Contents/MacOS/binary`` → ``…/App.app``, else None."""

    path = Path(executable)
    parts = path.parts
    if "Contents" in parts and "MacOS" in parts:
        index = parts.index("Contents")
        if index > 0 and index + 1 < len(parts) and parts[index + 1] == "MacOS":
            return Path(*parts[:index])
    return None


def _bundle_id(executable: str | None) -> str | None:
    bundle = application_bundle(executable) if executable else None
    if bundle is None:
        return None
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as handle:
            value = plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, str) else None


def _frontmost() -> tuple[str, str] | None:
    """The macOS app in front, as (launch-services id, bundle id)."""

    try:
        asn = subprocess.check_output(["lsappinfo", "front"], text=True, stderr=subprocess.DEVNULL).strip()
        info = subprocess.check_output(
            ["lsappinfo", "info", "-only", "bundleid", asn], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r'"CFBundleIdentifier"="([^"]+)"', info)
    return (asn, match.group(1)) if asn and match else None


def load_profile_cookies(profile: Path, executable: str | None = None) -> httpx.Cookies:
    """Session cookies from BibVault's Chromium profile, or empty if they cannot be read.

    The browser must not hold the cookie database open (close its window first).
    """

    db = _cookies_db(profile)
    if db is None:
        return httpx.Cookies()
    key = _os_crypt_key(executable)
    if key is None:
        logger.warning("could not unlock the browser cookie store; library PDFs will open in the browser")
        return httpx.Cookies()
    try:
        rows = _cookie_rows(db)
    except sqlite3.Error as exc:
        logger.warning("could not read browser cookies (%s); library PDFs will open in the browser", exc)
        return httpx.Cookies()
    jar = httpx.Cookies()
    for host, name, value, encrypted, path, secure in rows:
        plain = value or _decrypt_cookie(encrypted, key)
        if not plain:
            continue
        domain = host[1:] if host.startswith(".") else host
        try:
            jar.set(name, plain, domain=domain, path=path or "/")
        except Exception:
            continue
    return jar


def _cookies_db(profile: Path) -> Path | None:
    for relative in ("Default/Network/Cookies", "Default/Cookies"):
        path = profile / relative
        if path.is_file():
            return path
    return None


def _cookie_rows(db: Path) -> list[tuple[str, str, str, bytes, str, int]]:
    """Copy the DB aside so a locked or WAL-mode file still reads."""

    with tempfile.TemporaryDirectory(prefix="bibvault-cookies-") as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(db, copy)
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.is_file():
                shutil.copy2(side, Path(str(copy) + suffix))
        connection = sqlite3.connect(copy)
        try:
            return list(
                connection.execute(
                    "SELECT host_key, name, value, encrypted_value, path, is_secure FROM cookies"
                )
            )
        finally:
            connection.close()


def _os_crypt_key(executable: str | None) -> bytes | None:
    if sys.platform == "darwin":
        return _macos_key(executable)
    if sys.platform.startswith("linux"):
        return _linux_key(executable)
    return None


def _macos_key(executable: str | None) -> bytes | None:
    names = []
    if executable:
        bundle = application_bundle(executable)
        if bundle is not None:
            names.append(bundle.stem)
        names.append(Path(executable).name)
    for browser, service, account in _SAFE_STORAGE:
        if names and not any(browser in name or name in browser for name in names):
            continue
        password = _keychain_password(service, account)
        if password is not None:
            return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
    for _, service, account in _SAFE_STORAGE:
        password = _keychain_password(service, account)
        if password is not None:
            return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
    return None


def _keychain_password(service: str, account: str) -> bytes | None:
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", service, "-a", account],
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _linux_key(executable: str | None) -> bytes | None:
    # Chromium on Linux uses one PBKDF2 iteration; the password lives in the desktop keyring.
    password = b"peanuts"
    label = "Chrome"
    if executable:
        lower = executable.lower()
        if "edge" in lower:
            label = "Microsoft Edge"
        elif "brave" in lower:
            label = "Brave"
        elif "chromium" in lower:
            label = "Chromium"
    try:
        import secretstorage  # type: ignore[import-not-found]
    except ImportError:
        secretstorage = None
    if secretstorage is not None:
        try:
            bus = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(bus)
            for item in collection.get_all_items():
                if item.get_label() == f"{label} Safe Storage":
                    password = item.get_secret()
                    break
        except Exception:
            pass
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, dklen=16)


def _decrypt_cookie(encrypted: bytes, key: bytes) -> str:
    if not encrypted:
        return ""
    payload = encrypted[3:] if encrypted[:3] in {b"v10", b"v11"} else encrypted
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(payload) + decryptor.finalize()
        pad = plain[-1]
        if 1 <= pad <= 16:
            plain = plain[:-pad]
        return plain.decode("utf-8")
    except Exception:
        return ""


class BrowserDownloads:
    def __init__(self, settings: Settings, open_url: Callable[[str], None] | None = None) -> None:
        self.settings = settings.browser
        self.home = browser_home()
        self.profile = self.home / "profile"
        self.inbox = self.home / "inbox"
        self.executable = None if open_url else find_browser(self.settings.executable)
        self._open_url = open_url
        self._last_open: float | None = None

    def available(self) -> bool:
        return self._open_url is not None or self.executable is not None

    def open(self, url: str) -> None:
        (self._open_url or self._launch)(url)

    def prepare(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.inbox.mkdir(exist_ok=True)
        preferences = self.profile / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True, exist_ok=True)
        current = json.loads(preferences.read_text(encoding="utf-8")) if preferences.exists() else {}
        inbox = str(self.inbox)
        # Rewritten before every launch, since a setting synced from the user's account can replace them.
        current.setdefault("download", {}).update(
            {"default_directory": inbox, "prompt_for_download": False, "directory_upgrade": True}
        )
        current.setdefault("savefile", {})["default_directory"] = inbox
        current.setdefault("plugins", {})["always_open_pdf_externally"] = True
        preferences.write_text(json.dumps(current), encoding="utf-8")

    def profile_cookies(self) -> httpx.Cookies:
        return load_profile_cookies(self.profile, self.executable or self.settings.executable)

    async def download(self, url: str, *, background: bool = True) -> Path | None:
        """Open ``url`` and return the PDF the browser saves, or None after the timeout."""

        if self._last_open is not None:
            wait = self.settings.interval_seconds - (time.monotonic() - self._last_open)
            if wait > 0:
                await asyncio.sleep(wait)
        before = {path.name for path in self.inbox.iterdir()}
        front = _frontmost() if background and _MACOS and self._open_url is None else None
        self.open(url)
        self._last_open = time.monotonic()
        if front is not None:
            await self._give_focus_back(front)
        for _ in range(max(1, int(self.settings.timeout_seconds / _POLL_SECONDS))):
            await asyncio.sleep(_POLL_SECONDS)
            # Chromium writes *.crdownload and renames it once the download is complete.
            for path in sorted(self.inbox.iterdir()):
                if path.name not in before and path.suffix.lower() == ".pdf":
                    return path
        return None

    async def _give_focus_back(self, front: tuple[str, str]) -> None:
        asn, bundle = front
        # Two instances of the same browser share a bundle id, so the user's own window cannot be singled out.
        if bundle == _bundle_id(self.executable):
            return
        for _ in range(_FOCUS_POLLS):
            await asyncio.sleep(_FOCUS_POLL_SECONDS)
            now = _frontmost()
            if now is not None and now[0] != asn:
                subprocess.run(["open", "-b", bundle], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                return

    def _launch(self, url: str) -> None:
        assert self.executable is not None
        # The binary itself, not ``open -a``: macOS drops ``--args`` for a running app, and a bare
        # URL goes to whichever instance of the browser macOS picks, often the user's own profile.
        subprocess.Popen(
            [
                self.executable,
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                # Edge signs this profile in to the macOS account; sync would replace the download settings.
                "--disable-sync",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
