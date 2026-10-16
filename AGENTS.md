# BibVault agent notes

Repository constraints for coding agents. The AI client workflow (search, locate, paged read, cite, download or maintain), stdio configuration, permission modes, path limits, and failure recovery live only in [docs/AI_USAGE.md](docs/AI_USAGE.md). Do not copy that workflow into client-specific instruction files.

## Constraints

- Python 3.11+. BibVault stays a single-machine CLI with a SQLite library. `.bibvault/index.sqlite` is the source of truth. `manifest.json` is regenerated from it.
- Leave the user's BibTeX file unchanged.
- Do not collect publisher credentials, automate publisher login, or bypass an access decision. `fetch --browser` hands links to a separate browser where the user signs in. See [docs/SECURITY.md](docs/SECURITY.md). Interactive browser mode is opt-in and is not an SSRF boundary.
- The preferred file is `{citation_key}.pdf` or `{citation_key}.md` under the library root (default `papers/`). Filenames keep letters and digits from any script. A preferred file that is already on disk stays in place across later fetch runs.
- The local MCP server is the optional `bibvault-mcp` entry (`bibvault[mcp]`). [docs/AI_USAGE.md](docs/AI_USAGE.md) is the contract. The server calls the Python service in-process, speaks stdio, starts read-only, and gains write tools only with `--allow-write`. It does not run a TTY browser flow. BibTeX and import paths stay inside the project root. Document reads stay inside the library root and are addressed by citation key. `plan_prune` then `apply_prune` is the only mapping-cleanup protocol, and the first version does not delete PDF or Markdown files.
- Quality gates are in [docs/QUALITY_GATES.md](docs/QUALITY_GATES.md): offline pytest, Ruff, and mypy. Live, Crawl4AI, and real-browser tests stay opt-in.
- Scope stays the CLI, the local library, and the sources already described in the docs. A web UI, Postgres, embeddings, Unpaywall, and a publisher plugin framework are outside that scope.

## Checks

From a development checkout:

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```
