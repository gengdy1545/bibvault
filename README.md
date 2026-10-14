# BibVault

BibVault turns a BibTeX file into a local library that LaTeX and AI agents can address by citation key.

```
BibTeX citation key → reference metadata → local PDF → SQLite / manifest.json
```

V0.1 downloads open-access PDFs and archives web pages as Markdown. The preferred file is `{citation_key}.pdf` for a paper and `{citation_key}.md` for a web page. Publisher login and MCP are later phases.

## Install

Requires Python 3.11+.

```bash
uv sync --extra dev
uv run crawl4ai-setup       # once; installs Chromium for dynamic web pages
uv run bibvault --help
```

## Quick start

```bash
uv run bibvault init
uv run bibvault auth login openalex    # once per machine; see "OpenAlex API key" below
uv run bibvault fetch references.bib --key lakehouse-comparison23
uv run bibvault locate lakehouse-comparison23
uv run bibvault locate lakehouse-comparison23 --format json
uv run bibvault verify --references
uv run bibvault sync references.bib
```

## OpenAlex API key

BibVault finds open-access copies through the OpenAlex API. Set up a free key before the first `fetch`:

1. Create an account at [openalex.org](https://openalex.org) (about 30 seconds).
2. Copy your key from [openalex.org/settings/api](https://openalex.org/settings/api).
3. Run `uv run bibvault auth login openalex` and paste it. The input is not shown, the key is checked with OpenAlex, and it is saved to `~/.config/bibvault/credentials.toml`, readable only by you. Nothing goes into the project.
4. Run `uv run bibvault auth status` to confirm. It prints only the last four characters of the key.

`uv run bibvault auth logout openalex` deletes the saved key. For scripts and CI, `BIBVAULT_OPENALEX_API_KEY` works too and overrides the saved key. The key is also your openalex.org login: never put it in a project config you commit, and if it leaks, rotate it under Settings → API key.

Without a key, OpenAlex gives your IP address a small daily budget, and your own visits to openalex.org draw on the same budget. When it runs out, the website shows a "Looks like you're a bot" page until midnight UTC. So without a key BibVault holds back:

- At most 100 OpenAlex requests a day (about a tenth of that budget), counted since midnight UTC. `bibvault auth status` shows how many are used.
- No title searches, which cost ten lookups each. Entries without a DOI end as `rate_limited` until you add a key.

With or without a key, an answer that OpenAlex's budget is spent (HTTP 429) ends OpenAlex requests for the run and until the budget refills, instead of being retried for every entry. A key that OpenAlex rejects (HTTP 401) ends them for the run. A title search, found or not, is reused for seven days. Publisher sites, arXiv, direct PDF links, and `--browser` keep working when OpenAlex is off. To not use OpenAlex at all, set `use_openalex = false` under `[resolution]`.

Some publishers serve their papers openly, so BibVault gets those from the publisher's own site and does not use the OpenAlex budget:

- PVLDB papers that have a `volume` come from vldb.org.
- USENIX ATC, FAST, NSDI, and OSDI papers come from usenix.org. BibVault follows the entry's `url` to the paper's page there, or finds the paper by title in the conference program.
- CIDR papers come from cidrdb.org. BibVault finds the paper by title in that year's program.
- VLDB conference papers up to 2007, before PVLDB, come from that year's proceedings on vldb.org. BibVault finds the paper by title, or by its first page when the files are named after it (for example 2001's `P169.pdf`). Years whose files carry session or sequence numbers, such as 1999, 2002, 2003, and 2004, are not found this way.
- EDBT and ICDT papers with an OpenProceedings DOI (`10.5441/002/…` or `10.48786/…`) come from openproceedings.org.

## Statuses

This version does not log in to publishers. Each entry ends in one status:

- `downloaded` / `already_exists`: the PDF or the archived Markdown page is in `papers/`.
- `no_open_access_found`: OpenAlex knows the paper but lists no open-access copy. It may still be free somewhere else, or need your library's access. Download it yourself, then run `bibvault import paper.pdf --key KEY`.
- `authentication_required`: the publisher answered with a login page or HTTP 401.
- `access_denied`: the publisher answered HTTP 403.
- `manual_action_required`: an open-access landing page exists but no direct PDF link, or the site only serves browsers (a bot check). A locally obtained PDF or Markdown file can be registered with `bibvault import`.
- `not_found`: no matching publication, the link returned 404, or a web entry has no URL.
- `rate_limited`: OpenAlex's daily budget, or BibVault's own limit for requests without a key, is used up, or the entry needs a title search and there is no key. The message says which; adding a key or waiting for midnight UTC fixes it.
- `failed`: try again later; `fetch` exits with code 1 when anything failed.

`bibvault fetch` shows one `Fetching` line for every publication in the run. A file already on disk counts immediately. Each remaining publication counts when its file is written or the run has finished with it. Sign-in prompts and other notices are printed under that line, and the line stays where it is. The report that follows counts files already on disk, newly downloaded, and only the entries it could not acquire. `--format json` lists every entry and does not show the progress line.

`bibvault import FILE --key KEY --bib references.bib` accepts PDF, UTF-8 Markdown, and saved HTML (`.html`, `.htm`). HTML is extracted with Trafilatura and stored as Markdown. `--source-url URL` is that page's address: relative links in the extracted Markdown resolve against it, and the URL is stored with `--provider NAME` in SQLite and `manifest.json`.

Web pages are fetched by Crawl4AI's HTTP strategy and their main content is extracted by Trafilatura. If that HTML has no main text, `bibvault fetch references.bib --browser` lets Crawl4AI render JavaScript in headless Chromium and runs Trafilatura again. Crawl4AI's cache is disabled, the HTML is not retained, and images remain remote links in the saved Markdown. BibVault does not enable Crawl4AI's stealth or undetected modes; a remaining bot check is `manual_action_required`.

dl.acm.org and library-proxied publishers need a visible browser for sign-in. The same `--browser` option starts your installed Edge or Chrome on a profile of its own (`~/.config/bibvault/browser/`). For each publisher group it first opens a sign-in page and waits until you press Enter in the terminal. ACM PDF links are then opened in that browser, 5 seconds apart and at most 50 per run; on macOS, focus goes back to the app you were in right after each link opens; BibVault takes each saved file from the profile's download folder, checks that it is a PDF of the right paper, and stores it as `{citation_key}.pdf`. It does not control these publisher pages. Set `executable` under `[browser]` if no browser is found.

With `--browser`, a paper the browser can open (ACM, and IEEE or Springer through your library proxy) does not take a preprint in the first pass. The browser tries the publisher's PDF first; only if it saves nothing usable does BibVault store the preprint instead. A paper the browser did not reach in this run (beyond `max_per_run`, or after two links in a row saved nothing, which usually means the window needs you) keeps waiting for the next `--browser` run.

## Library proxy (IEEE, Springer)

If your library gives off-campus access through a proxy that rewrites publisher addresses (for example `….libproxy.ruc.edu.cn`), `--browser` signs in once in the browser, then downloads IEEE Xplore and Springer PDFs silently over HTTP with that session:

1. Sign in to the proxy in your usual browser and open any paper of the publisher through it.
2. Copy the address from the address bar. The part before the first `/` is the publisher's host on the proxy.
3. Add it to `~/.config/bibvault/config.toml`. Pasting the whole address also works. For sign-in, BibVault opens the library portal it derives from that host (`https://libproxy.ruc.edu.cn/ermsClient/browse.do` here). One publisher is enough to start; the other's address can be pasted at the next step.

   ```toml
   [browser.proxy_hosts]
   "ieeexplore.ieee.org" = "gfhfzfd70b582c88e4556swfckpk5vnowb6wuufaag.libproxy.ruc.edu.cn"
   "link.springer.com" = "….libproxy.ruc.edu.cn"
   ```

4. Run `bibvault fetch references.bib --browser`. BibVault opens the sign-in portal in its browser window. Sign in there (BibVault never sees the password), open IEEE Xplore from the portal, and paste the address from the address bar into the terminal when asked; then the same for SpringerLink. BibVault saves a new host under `[browser.proxy_hosts]` in `~/.config/bibvault/config.toml`, keeping the rest of the file, so later runs use it. Pressing Enter keeps the configured host, or skips that publisher for the run if none is configured. Then close the BibVault browser window and press Enter again: BibVault reads the session cookies from its profile and downloads the PDFs without opening each link. If the cookies cannot be read, it falls back to opening links in the browser. If a silent download comes back as a login page, that publisher is signed out for this run: its remaining papers are not tried, and signing in again on the next run retries them. The other publisher is still downloaded.

IEEE papers need their Xplore document number. BibVault asks doi.org for it once per paper and remembers it. A publisher with neither a configured nor a pasted host is not opened, because the browser would only reach the paywall; without any entry under `[browser.proxy_hosts]` there is no portal to sign in at, so IEEE and Springer papers are not tried at all. If downloads stop after a while, sign in again and re-run. If they still stop, the proxy has probably changed its address: paste the new one at the prompt.

A PDF you annotated in place is kept; `verify` reports that its checksum changed.

Downloaded files go to `papers/` and are ignored by git. Mapping state lives in `.bibvault/index.sqlite`. `manifest.json` is regenerated from that database.

## Layout

```
papers/
├── iceberg2020.pdf
├── 张三2020.pdf
├── hudi-index.md
└── _unreferenced/
.bibvault/
├── config.toml
├── index.sqlite
└── manifest.json
```

`papers/iceberg2020.pdf` is the full text of a paper. `papers/hudi-index.md` is the archived text of a web page, with the page's own image URLs left in the Markdown. BibVault keeps one PDF per paper: the best version it can download, with a PDF link in the entry itself first, then the published version, then an accepted manuscript, then a preprint. A paper that already has its PDF makes no requests on later runs, so a preprint is never followed by a second download of the published version. To replace a file with a better version, delete it and fetch again. Two citation keys for the same DOI share one publication id and, when the filesystem allows, one set of bytes via a hard link.

Filenames keep letters and digits of any script, so `张三2020` becomes `张三2020.pdf`; characters such as `: / \ * ?` become `_`.

When a key starts pointing at a different paper (a corrected DOI, or two keys swapped), the key's filename follows the new paper. A paper no key refers to any more moves to `papers/_unreferenced/` and comes back without a download if a key points at it again. Entries you delete from the BibTeX file keep their PDFs where they are.

## Tests

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

The suite mocks every HTTP request and uses `tests/fixtures/sample.bib`. A few smoke tests hit the real CIDR and arXiv servers and are skipped unless you opt in:

```bash
BIBVAULT_LIVE_TESTS=1 uv run pytest -m live
```

If a `my.bib` exists in the repository root, `tests/test_parsing.py` also checks that every entry in it parses.
