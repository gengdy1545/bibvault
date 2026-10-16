# AI usage

This is the only AI workflow for BibVault. Client setup below launches the same local server; it does not define a second workflow. Root [`AGENTS.md`](../AGENTS.md) keeps repository constraints and points here.

The stdio MCP server is the `bibvault-mcp` console script. Install the optional `mcp` extra so that script can start; from a checkout, `uv sync --extra mcp` or `uv sync --extra dev` is enough. The shapes below are the contract. A client that cannot start the script should use the [CLI equivalents](#cli-equivalents).

## Workflow

1. **Search.** Call `search_references` with a DOI, a title, or both. The lookup reads the local library only.
2. **Locate.** Call `get_reference` with the citation key. Use its `status`, relative path, `sha256`, `source_url`, and `provider`.
3. **Read a page.** Call `read_document` for one PDF page, or one Markdown character window. Repeat with the next page or offset. Stay inside `max_chars`.
4. **Cite.** Attribute a quotation with the citation key, PDF page, `sha256`, and `source_url` from that `read_document` response.
5. **Download or maintain.** Use a process started with `--allow-write` for `fetch_references`, `sync_references`, `import_document`, or `upgrade_reference`. Login, proxy, and browser downloads stay in the user's terminal. Mapping cleanup is `plan_prune`, then `apply_prune`.

## Server

Console script: `bibvault-mcp`. Optional extra: `mcp`. Transport is stdio only.

```bash
uv tool install "bibvault[mcp]"
```

```text
bibvault-mcp [--config PATH] [--allow-write]
```

`--config` is the project `.bibvault/config.toml`. An omitted `--config` uses the same project discovery as the CLI, from the process working directory. Pass an absolute `--config` when the client does not start the process in the project root. GUI clients often have a short `PATH`; point `command` at the absolute executable once it is installed.

The server calls the Python library service in-process. It does not parse CLI stdout and does not contain a second acquisition implementation.

Startup flags:

| Flag | Effect |
| --- | --- |
| *(none)* | Advertise the five read-only tools. |
| `--allow-write` | Also advertise the write and prune tools. |
| `--config PATH` | Select the project config. Environment variables still override it, as they do for the CLI. |

A write tool invoked on the read-only process returns `write_not_enabled` and leaves the library unchanged. Switching mode means starting another process. Prefer two client entries, and leave the write entry disabled until a maintenance task.

The server has no TTY. It does not launch `fetch --browser`, headless page rendering, or a publisher login. When a paper needs that step, the tool result uses status `manual_action_required` and includes `cli` with one pasteable command.

## Read-only tools

These tools are available in every mode.

### `library_info`

No arguments. Returns the process roots and whether write tools are advertised.

```json
{
  "project_root": "/abs/project",
  "library_root": "/abs/project/papers",
  "config_path": "/abs/project/.bibvault/config.toml",
  "read_only": true,
  "allow_write": false,
  "reference_count": 12,
  "read_document": { "max_chars_limit": 8000 }
}
```

`read_document.max_chars_limit` is the server's hard character cap. Callers pass `max_chars` less than or equal to that value. The `8000` above only shows the field; the running server reports the real cap, and callers use that reported value.

### `search_references`

Local metadata search. It does not call OpenAlex or the network. Provide `doi`, `title`, or both. When both are set, a row must match both. DOI matching is normalized equality. Title matching is the same case-folded comparison as `bibvault find --title`. The first version does not use full-text search or embeddings. The default `limit` is 20. The server caps `limit` at 50.

| Argument | Required | Meaning |
| --- | --- | --- |
| `doi` | no | DOI stored for the reference. |
| `title` | no | Title stored for the reference. |
| `limit` | no | Maximum rows to return. The server enforces its own cap. |

```json
{
  "matches": [
    {
      "citation_key": "attention2017",
      "title": "Attention Is All You Need",
      "reference_id": "arxiv:1706.03762",
      "status": "downloaded",
      "path": "papers/attention2017.pdf"
    }
  ],
  "returned": 1,
  "truncated": false
}
```

`path` is relative to the project root when the preferred PDF or Markdown file is on disk, and `null` when the key is known but the file is absent. An empty `matches` array means nothing matched. That is a successful search.

### `get_reference`

| Argument | Required | Meaning |
| --- | --- | --- |
| `citation_key` | yes | BibTeX citation key. |

A known key returns the same status and provenance fields as `bibvault locate --format json`: `citation_key`, `reference_id`, `type`, `title`, `status`, `version`, `files`, and `error`. Each file object carries `path`, `sha256`, `source_url`, `provider`, `retrieved_at`, `version`, and `byte_size`. `path` is project-relative, or `null` when that role is not on disk.

An unknown key is the error `unknown_key`. A known key whose preferred PDF and Markdown paths are both `null` is a successful payload with the stored status. Those two outcomes stay distinct.

### `read_document`

Reads the file SQLite maps to one citation key. The caller does not pass a filesystem path.

| Argument | Required | Meaning |
| --- | --- | --- |
| `citation_key` | yes | Key whose preferred file to read. |
| `page` | PDF | 1-based PDF page. One page per call. Omitted `page` reads page 1 only. |
| `offset` | Markdown | Character offset into the archived Markdown. Omitted `offset` starts at 0. |
| `max_chars` | yes | Maximum characters to return. Must be from 1 through `library_info.read_document.max_chars_limit`. |

The preferred role is `pdf` when that file exists, otherwise `markdown`. The resolved file must stay inside the library root after symlink resolution.

```json
{
  "citation_key": "attention2017",
  "title": "Attention Is All You Need",
  "role": "pdf",
  "page": 3,
  "offset": null,
  "text": "...",
  "truncated": false,
  "sha256": "<sha256 of the file>",
  "source_url": "https://arxiv.org/pdf/1706.03762.pdf",
  "provider": "arxiv"
}
```

`truncated: true` means `text` ends at `max_chars` inside that page or window. Request the next PDF page, or a larger offset, for the rest. A missing file is `file_missing` and includes the stored status. An unknown key is `unknown_key`.

### `verify_library`

| Argument | Required | Meaning |
| --- | --- | --- |
| `references` | no | When true, also check citation-key mappings, matching `bibvault verify --references`. |

Returns `{ "ok": true, "problems": [] }` or `{ "ok": false, "problems": ["..."] }`. The call does not rewrite files, checksums, or the manifest.

## Write tools

Advertised only when the process was started with `--allow-write`. They still refuse browser login, paths outside the roots below, and any deletion of PDF or Markdown bytes.

Acquisition results use the same status strings as `bibvault fetch --format json`:

`downloaded`, `already_exists`, `no_open_access_found`, `authentication_required`, `access_denied`, `manual_action_required`, `not_found`, `rate_limited`, `failed`, `unsupported`.

A result that needs a person at the terminal adds `cli`. The command is a single `bibvault` invocation. It repeats the caller's BibTeX path and `--key` values and, when a browser is required, adds `--browser`.

```json
{
  "citation_key": "iceberg2020",
  "title": "...",
  "reference_id": "doi:10.14778/...",
  "status": "manual_action_required",
  "path": null,
  "provider": null,
  "version": null,
  "error": "ACM download needs a visible browser",
  "cli": "bibvault fetch references.bib --key iceberg2020 --browser"
}
```

The response envelope matches the CLI JSON object: `summary` plus `results`.

### `fetch_references`

Reconcile the supplied BibTeX entries and acquire missing files for the given keys, or for every supplied entry when `keys` is omitted. HTTP acquisition only.

| Argument | Required | Meaning |
| --- | --- | --- |
| `bib` | yes | BibTeX paths inside the project root. |
| `keys` | no | Citation keys to acquire. |

### `sync_references`

Reconcile the supplied BibTeX set, including citation-key renames, then acquire missing files over HTTP. Omitted entries stay in the library. This tool does not prune mappings; use the prune pair for that.

| Argument | Required | Meaning |
| --- | --- | --- |
| `bib` | yes | BibTeX paths inside the project root. The caller treats this set as the collection to reconcile. |

### `import_document`

Register a local PDF, UTF-8 Markdown, or saved HTML file under an existing key. HTML is extracted to Markdown. The call does not discover or download anything. Validation matches `bibvault import`.

| Argument | Required | Meaning |
| --- | --- | --- |
| `citation_key` | yes | Key that should own the file. |
| `file` | yes | Source file inside the project root. |
| `bib` | no | BibTeX file used to resolve a key that is not stored yet. |
| `source_url` | no | Provenance URL. Also the base URL for relative links in imported HTML. |
| `provider` | no | Provenance label. Default `import`. |

### `upgrade_reference`

Download a candidate to a temporary file, verify it, and replace the stored file only when the candidate version ranks higher than the file on disk. Rank order is the entry's own PDF link, then published, then accepted manuscript, then preprint. Failure before the atomic replace leaves the existing file, checksum, and provenance in place. A candidate that is not higher returns `already_exists` with the existing path and provenance. A browser-only publisher returns `manual_action_required`, `cli`, and does not replace the file.

| Argument | Required | Meaning |
| --- | --- | --- |
| `citation_key` | yes | Key to upgrade. |
| `bib` | yes | BibTeX file inside the project root that contains the key. |

## Prune

Write mode only. The first version changes citation-key mappings. It does not delete PDF or Markdown files. `plan_prune` is the dry run. `apply_prune` runs only the plan named by a short-lived token. The token expires 10 minutes after `plan_prune` and lives only in that server process. A missing, expired, reused, or mismatched token changes nothing.

### `plan_prune`

| Argument | Required | Meaning |
| --- | --- | --- |
| `bib` | yes | BibTeX paths inside the project root. |
| `confirm_complete_set` | yes | Must be true. The user has confirmed this BibTeX set is the complete collection. |

```json
{
  "token": "<opaque>",
  "expires_at": "2026-10-15T12:20:00Z",
  "detach": ["oldkey2020"],
  "delete_files": []
}
```

`delete_files` is always empty. `apply_prune` rejects a plan whose `delete_files` is not empty.

### `apply_prune`

| Argument | Required | Meaning |
| --- | --- | --- |
| `token` | yes | Token from the matching `plan_prune` response. |

On success, returns the applied mapping changes and `"delete_files": []`.

## Permission modes

| Process | Tools |
| --- | --- |
| `bibvault-mcp --config PATH` | `library_info`, `search_references`, `get_reference`, `read_document`, `verify_library` |
| `bibvault-mcp --config PATH --allow-write` | Those five, plus `fetch_references`, `sync_references`, `import_document`, `upgrade_reference`, `plan_prune`, `apply_prune` |

`--allow-write` is the user's explicit opt-in at process start. A client-side tool allow-list can name the five read-only tools as a second restriction. It does not replace the flag: a write-capable process remains write-capable to any client that can see the extra tools.

Keep the OpenAlex API key in `~/.config/bibvault/credentials.toml` or `BIBVAULT_OPENALEX_API_KEY` in the user environment. Do not put the key in a committed MCP config, a project `.bibvault/config.toml`, or a chat. The server reads the same credential files as the CLI.

## Path boundaries

The server resolves every caller-supplied path and checks the real path after symlink resolution.

| Root | What may be named |
| --- | --- |
| Project root, the directory that contains `.bibvault/` | BibTeX files and import files. |
| Library root, `library.root` (default `<project>/papers`) | The PDF or Markdown file mapped to a citation key. Callers never pass this path. |

`read_document` opens that mapped file only. A symlink that resolves outside the library root is `path_not_allowed`. BibTeX and import paths outside the project root are `path_not_allowed`, including `..` escapes.

The server does not read or write these locations through its tools: `~/.config/bibvault/credentials.toml`, the browser profile under `~/.config/bibvault/browser/`, and any path outside the two roots. Library bytes under `papers/_unreferenced/` are reachable only when a citation key currently maps to them.

## Citations

Quote only text returned by `read_document`. Include the citation key, the PDF page when `role` is `pdf`, the `sha256`, and the `source_url` when it is present:

> Attention is all you need (`attention2017`, p. 3, sha256 `<sha256>`, <https://arxiv.org/pdf/1706.03762.pdf>).

When `truncated` is true, the quotation still has to fit inside the returned `text`. Do not cite a page the tool did not return. If `source_url` is null, cite the key, page, and `sha256` and say the library has no source URL for that file.

## Client configuration

Examples assume `bibvault-mcp` is installed (`bibvault[mcp]`) and on `PATH`. Use absolute paths. Enable the read-only server for ordinary reading. Add the write server only for a maintenance session, as a separate entry so the default process stays read-only.

Replace `/absolute/path/to/project` with the project that contains `.bibvault/config.toml`.

### Cursor

Project config is `.cursor/mcp.json`. User config is `~/.cursor/mcp.json`. If both define the same server name, the project entry wins. Prefer the user file for a personal library, so a shared repository does not turn on `--allow-write` for everyone.

```json
{
  "mcpServers": {
    "bibvault": {
      "type": "stdio",
      "command": "bibvault-mcp",
      "args": ["--config", "${workspaceFolder}/.bibvault/config.toml"]
    }
  }
}
```

Write session, usually in `~/.cursor/mcp.json`, disabled until needed:

```json
{
  "mcpServers": {
    "bibvault-write": {
      "type": "stdio",
      "command": "bibvault-mcp",
      "args": [
        "--config",
        "/absolute/path/to/project/.bibvault/config.toml",
        "--allow-write"
      ]
    }
  }
}
```

`${workspaceFolder}` is the folder that contains `.cursor/mcp.json`. Restart Cursor after editing the file.

### Codex

Codex reads `~/.codex/config.toml`. A trusted project may also use `.codex/config.toml`. The ChatGPT desktop app, Codex CLI, and IDE extension share this file.

```toml
[mcp_servers.bibvault]
command = "bibvault-mcp"
args = ["--config", "/absolute/path/to/project/.bibvault/config.toml"]
cwd = "/absolute/path/to/project"
enabled_tools = [
  "library_info",
  "search_references",
  "get_reference",
  "read_document",
  "verify_library",
]
```

```toml
[mcp_servers.bibvault-write]
command = "bibvault-mcp"
args = [
  "--config", "/absolute/path/to/project/.bibvault/config.toml",
  "--allow-write",
]
cwd = "/absolute/path/to/project"
enabled = false
```

Set `enabled = true` on `bibvault-write` for one maintenance session, then set it back to false. Equivalent registration:

```bash
codex mcp add bibvault -- bibvault-mcp --config /absolute/path/to/project/.bibvault/config.toml
```

Add `--allow-write` only on the write entry. Do not pass the OpenAlex key with `--env`.

### Claude Desktop

Claude Desktop starts each server as a stdio subprocess from `claude_desktop_config.json`.

| Platform | File |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

The app's `PATH` is not a login shell. Set `command` to the absolute executable.

```json
{
  "mcpServers": {
    "bibvault": {
      "command": "/absolute/path/to/bibvault-mcp",
      "args": ["--config", "/absolute/path/to/project/.bibvault/config.toml"]
    }
  }
}
```

```json
{
  "mcpServers": {
    "bibvault-write": {
      "command": "/absolute/path/to/bibvault-mcp",
      "args": [
        "--config",
        "/absolute/path/to/project/.bibvault/config.toml",
        "--allow-write"
      ]
    }
  }
}
```

Keep the write entry out of the file until a maintenance session. Quit Claude Desktop and reopen it after saving. Merge into an existing `mcpServers` object.

## Failure recovery

Tool errors use this object and do not modify the library:

```json
{
  "error": {
    "code": "unknown_key",
    "message": "unknown citation key attention2017",
    "citation_key": "attention2017",
    "cli": null
  }
}
```

| Code or status | Recovery |
| --- | --- |
| Server command missing, or the client cannot connect | Install the `mcp` extra so `bibvault-mcp` is on `PATH`, or set `command` to that executable. Until then, use [CLI equivalents](#cli-equivalents). |
| `unknown_key` | Stop. Do not invent a path. Sync or fetch that key only after the user confirms the BibTeX file. |
| `file_missing`, or a known key with null paths | Report `status`. Acquire only in a write-enabled process, or give the user the CLI command. |
| `manual_action_required` | Show `cli` and stop. The user runs that command in a terminal, completes sign-in there, then calls `get_reference` again. The MCP process does not wait on the browser. |
| `authentication_required`, `access_denied` | Same terminal `--browser` command, or `import_document` / `bibvault import` of a file the user obtained. |
| `no_open_access_found` | The user downloads a copy, then import registers it. |
| `not_found` | Check the DOI, title, and year stored for the key. |
| `rate_limited` | Wait until the reset time in the message, or the user runs `bibvault auth login openalex` in a terminal. Do not ask them to paste the key into chat. |
| `failed` | Retry the same write call later. A failed `upgrade_reference` keeps the previous file. |
| `verify_library` problem | Report it. An annotated PDF stays on disk; verification does not replace it. |
| `path_not_allowed` | Put the BibTeX or import file inside the project root and call again. |
| `write_not_enabled` | The running server is read-only. The user starts the separate `--allow-write` entry. |
| `token_expired`, `token_mismatch` | Call `plan_prune` again. `apply_prune` has not changed the library. |
| Process exit during fetch or sync | Run the same call again. Files already stored come back as `already_exists`. |

After a write process exits, the recovery copy is the library root plus the project `.bibvault/` directory. SQLite is the source of truth. `manifest.json` is regenerated from it. See the backup section in the README.

## CLI equivalents

`bibvault find` is the CLI form of `search_references`. `bibvault fetch --upgrade --key` is the CLI form of `upgrade_reference`. Mapping cleanup on the CLI is `bibvault sync --prune-keys`; the MCP form is `plan_prune` then `apply_prune`. Neither form deletes PDF or Markdown bytes. `read_document` has no CLI page reader. Do not page through a PDF by reading the file outside the library, and do not delete PDFs to force another download.

These commands work without the MCP server. `--format json` writes the payload to stdout, suppresses the progress line, and leaves warnings on stderr.

| Goal | Command |
| --- | --- |
| Locate one key | `bibvault locate KEY --format json` |
| List stored keys | `bibvault status --format json` |
| Check checksums and mappings | `bibvault verify --references` |
| Acquire | `bibvault fetch references.bib --key KEY --format json` |
| Reconcile a BibTeX set, then acquire over HTTP | `bibvault sync references.bib --format json` |
| Register a local file | `bibvault import FILE --key KEY --bib references.bib` |
| Browser or proxy sign-in | `bibvault fetch references.bib --key KEY --browser` |

`locate` exit status:

| Case | stdout | Exit |
| --- | --- | --- |
| Unknown key | `null` | 1 |
| Known key, no file on disk | JSON object, file paths null | 1 |
| File on disk | JSON object | 0 |

`fetch` and `sync` exit 2 when `--key` names a key that is not in the BibTeX file, and exit 1 when any result status is `failed`. Other problem statuses are reported in the JSON and still exit 0. `import` and `verify` exit 1 when they refuse the file or find a problem.

## Related docs

- [README](../README.md): install, command boundaries, statuses, browser proxy, backups.
- [Architecture](ARCHITECTURE.md): identity, pipeline, and product boundary.
- [Network security](SECURITY.md): HTTP(S) acquisition limits. Interactive browser mode is not an SSRF boundary.
