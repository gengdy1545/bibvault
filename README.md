# BibVault

BibVault turns a BibTeX file into a local library that LaTeX and AI agents can address by citation key.

```
BibTeX citation key → reference metadata → local PDF → SQLite / manifest.json
```

V0.1 downloads open-access PDFs and archives web pages as Markdown. The preferred file is `{citation_key}.pdf` for a paper and `{citation_key}.md` for a web page. The optional local MCP server is `bibvault-mcp` from the `mcp` extra. AI clients follow [docs/AI_USAGE.md](docs/AI_USAGE.md).

BibVault does not collect publisher credentials or bypass access controls. With `fetch --browser`, it can hand publisher links to a separate Chromium-family browser where you sign in yourself, then reuse that browser's library-proxy session when possible.

## Install for use

Requires Python 3.11+.

From a source checkout, install the CLI in an isolated uv tool environment:

```bash
uv tool install . --with-executables-from crawl4ai
bibvault --help
```

This does not install test, lint, or type-checking dependencies. To include the local MCP server, use `uv tool install ".[mcp]" --with-executables-from crawl4ai`. A browser is optional:

- Direct PDF links, ordinary web pages, arXiv, and publisher sites that serve files over HTTP need no browser.
- For JavaScript-only web pages, run `crawl4ai-setup` once. It installs the headless Chromium used by Crawl4AI.
- For ACM downloads or IEEE/Springer through a library proxy, install Edge, Chrome, Chromium, or Brave. This is a separate visible browser where you complete sign-in yourself. Set `executable` under `[browser]` if BibVault cannot find it.

For development instead:

```bash
uv sync --extra dev
uv run bibvault --help
uv run pytest
```

Development commands use `uv run bibvault`; the remaining examples use the isolated `bibvault` command.

## Quick start

Initialize a project and run the local, no-network readiness checks:

```bash
bibvault init
bibvault doctor
```

The smallest acquisition path needs neither OpenAlex nor Chromium. Put a direct PDF URL in `references.bib`:

```bibtex
@article{attention2017,
  title = {Attention Is All You Need},
  year = {2017},
  url = {https://arxiv.org/pdf/1706.03762.pdf}
}
```

Then fetch and locate it:

```bash
bibvault fetch references.bib --key attention2017
bibvault locate attention2017
bibvault verify --references
```

Use `bibvault sync references.bib` when the BibTeX file is the complete set you want reconciled and fetched.

## Command boundaries

- `fetch BIB...` reconciles the supplied entries and acquires all missing files, or only the requested `--key` values. `fetch --upgrade --key KEY` downloads a replacement and atomically replaces the stored PDF only when that version ranks higher. A failed download, or a file whose bytes no longer match the stored checksum, leaves the original in place.
- `sync BIB...` reconciles the complete supplied BibTeX set, including citation-key renames, then acquires missing files. `sync --browser` uses the same browser pass as `fetch --browser`. Omitted standalone entries stay unless `--prune-keys` is set; that releases their citation keys and moves PDF bytes to `papers/_unreferenced/` without deleting them. `--prune-keys --dry-run` only lists those keys.
- `fetch --browser` first uses the normal HTTP paths. It additionally renders JavaScript-only web pages in Crawl4AI's headless Chromium and, for supported publishers, opens links in a separate visible browser. Human sign-in stays in that browser; BibVault never receives the password.
- `status` lists the library. `status BIB...` counts entries that are available locally, known but missing a file, not in the library, or left in the library.
- `find --doi DOI` and `find --title TEXT` search stored metadata and make no network requests. `sources` lists registered database sources and whether each one needs a browser.
- `import FILE --key KEY` performs no discovery or download. It validates a PDF, Markdown, or saved HTML file and registers it under an existing key; pass `--bib` when the key has not been synced yet.

## OpenAlex API key

BibVault uses OpenAlex to discover open-access copies when a direct or supported publisher source is not already known. A key is recommended, but it is not required for the direct-PDF path above, arXiv identifiers, or supported publisher sources. To enable full DOI and title discovery:

1. Create an account at [openalex.org](https://openalex.org) (about 30 seconds).
2. Copy your key from [openalex.org/settings/api](https://openalex.org/settings/api).
3. Run `bibvault auth login openalex` and paste it. The input is not shown, the key is checked with OpenAlex, and it is saved to `~/.config/bibvault/credentials.toml`, readable only by you. Nothing goes into the project.
4. Run `bibvault auth status` to confirm. It prints only the last four characters of the key.

`bibvault auth logout openalex` deletes the saved key. For scripts and CI, `BIBVAULT_OPENALEX_API_KEY` works too and overrides the saved key. The key is also your openalex.org login: never put it in a project config you commit, and if it leaks, rotate it under Settings → API key.

Without a key, OpenAlex gives your IP address a small daily budget, and your own visits to openalex.org draw on the same budget. When it runs out, the website shows a "Looks like you're a bot" page until midnight UTC. So without a key BibVault holds back:

- At most 100 OpenAlex requests a day (about a tenth of that budget), counted since midnight UTC. `bibvault auth status` shows how many are used.
- No title searches, which cost ten lookups each. Entries without a DOI end as `rate_limited` until you add a key.

With or without a key, an answer that OpenAlex's budget is spent (HTTP 429) ends OpenAlex requests for the run and until the budget refills, instead of being retried for every entry. A key that OpenAlex rejects (HTTP 401) ends them for the run. A title search, found or not, is reused for seven days. Publisher sites, arXiv, direct PDF links, and `--browser` keep working when OpenAlex is off. To not use OpenAlex at all, set `use_openalex = false` under `[resolution]`.

## Configuration and doctor

Configuration is merged in this order, from lowest to highest precedence:

1. Built-in defaults.
2. `~/.config/bibvault/config.toml`.
3. `~/.config/bibvault/credentials.toml`, written by `auth login`.
4. The nearest project `.bibvault/config.toml`, or the file selected by `--config PATH`.
5. `BIBVAULT_EMAIL`, `BIBVAULT_OPENALEX_API_KEY`, and `BIBVAULT_LIBRARY_ROOT`.

`--config` selects the project-config layer; environment variables still win. A relative `library.root` is resolved from the project root associated with that config.

`bibvault doctor` is deliberately local: it reports the Python version, effective project-config path, library-directory writability, OpenAlex key presence, and a detected visible Chromium-family browser. It neither validates the key over the network nor launches a browser or downloads a paper. A missing OpenAlex key or browser is a warning because the minimal direct-PDF path does not need either. An unsupported Python or unusable library directory makes `doctor` exit with status 1.

Some publishers serve their papers openly, so BibVault gets those from the publisher's own site and does not use the OpenAlex budget:

- PVLDB papers that have a `volume` come from vldb.org.
- USENIX ATC, FAST, NSDI, and OSDI papers come from usenix.org. BibVault follows the entry's `url` to the paper's page there, or finds the paper by title in the conference program.
- CIDR papers come from cidrdb.org. BibVault finds the paper by title in that year's program.
- VLDB conference papers up to 2007, before PVLDB, come from that year's proceedings on vldb.org. BibVault finds the paper by title, or by its first page when the files are named after it (for example 2001's `P169.pdf`). Years whose files carry session or sequence numbers, such as 1999, 2002, 2003, and 2004, are not found this way.
- EDBT and ICDT papers with an OpenProceedings DOI (`10.5441/002/…` or `10.48786/…`) come from openproceedings.org.

## Statuses

BibVault does not perform publisher login itself. When `fetch --browser` is used, you may sign in in the separate visible browser described below. Each entry ends in one status:

- `downloaded` / `already_exists`: the PDF or the archived Markdown page is in `papers/`.
- `no_open_access_found`: OpenAlex knows the paper but lists no open-access copy. It may still be free somewhere else, or need your library's access. Download it yourself, then run `bibvault import paper.pdf --key KEY`.
- `authentication_required`: the publisher answered with a login page or HTTP 401.
- `access_denied`: the publisher answered HTTP 403.
- `manual_action_required`: an open-access landing page exists but no direct PDF link, or the site only serves browsers (a bot check). A locally obtained PDF or Markdown file can be registered with `bibvault import`.
- `not_found`: no matching publication, the link returned 404, or a web entry has no URL.
- `rate_limited`: OpenAlex's daily budget, or BibVault's own limit for requests without a key, is used up, or the entry needs a title search and there is no key. The message says which; adding a key or waiting for midnight UTC fixes it.
- `failed`: try again later; `fetch` exits with code 1 when anything failed.

`bibvault fetch` shows one `Fetching` line for every publication in the run. A file already on disk counts immediately. Each remaining publication counts when its file is written or the run has finished with it. Sign-in prompts and other notices are printed under that line, and the line stays where it is. On a terminal the progress line is rewritten with ANSI cursor motion. When stderr is not a terminal, progress and warnings are plain lines on stderr, in order. The report that follows counts files already on disk, newly downloaded, and only the entries it could not acquire. `--format json` lists every entry and does not show the progress line.

`locate`, `status`, and `fetch --format json` share one record shape: `schema_version`, `citation_key`, `availability` (`available`, `missing`, or `unknown`), `status`, `error`, and `error_category`. `locate` exits 0 when a file is present, 1 when the key is known but the file is missing, and 2 when the key is unknown. `fetch` still exits 2 for a key that is not in the BibTeX file and 1 when a download fails. `status` is informational and exits 0.

`manifest.json` is rewritten from SQLite. Version 1 is `{"manifest_version": 1, "references": {citation key: record}}`. Readers also accept the older unversioned map. A newer `manifest_version` is rejected; SQLite remains the source of truth, and the next command rewrites the manifest from it.

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

## Backups

After any running BibVault command has exited, back up both the effective library root (`papers/` by default, including `_unreferenced/`) and the project's `.bibvault/` directory. The PDF/Markdown files contain the acquired or annotated bytes; `index.sqlite` is the source of truth for citation-key mappings, checksums, and provenance; `config.toml` records project settings. `manifest.json` is useful but can be regenerated and is not a database backup.

User-level files are separate. Back up `~/.config/bibvault/config.toml` if you need its proxy/browser settings. Treat `credentials.toml` as a secret and copy it only through a secure backup; it is not required to restore the library contents. The browser profile under `~/.config/bibvault/browser/` contains replaceable session state and is not part of the project backup.

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

`papers/iceberg2020.pdf` is the full text of a paper. `papers/hudi-index.md` is the archived text of a web page, with the page's own image URLs left in the Markdown. BibVault keeps one PDF per paper: the best version it can download, with a PDF link in the entry itself first, then the published version, then an accepted manuscript, then a preprint. A paper that already has its PDF makes no requests on later runs, so a preprint is never followed by a second download of the published version. To replace it with a higher-ranked version, run `bibvault fetch references.bib --upgrade --key KEY`. Two citation keys for the same DOI share one publication id and, when the filesystem allows, one set of bytes via a hard link.

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
