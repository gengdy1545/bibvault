# Quality gates

The `Quality gates` workflow runs on pushes, pull requests, and manual dispatches. It:

- runs the offline pytest suite on the minimum supported Python (3.11) and the current
  stable line (3.14);
- runs Ruff and mypy on Python 3.11; and
- builds a wheel, installs that wheel into a clean environment, and runs
  `bibvault --help`.

The default pytest selection is:

```bash
pytest -m "not live and not crawl4ai and not browser"
```

This still includes browser workflow unit tests that use fakes and never launch an
installed browser. Tests that contact real services, exercise the Crawl4AI adapter, or
launch an interactive browser must carry the corresponding marker and remain opt-in.
Run the existing opt-in suites locally with:

```bash
BIBVAULT_LIVE_TESTS=1 uv run pytest -m live
uv run pytest -m crawl4ai
```

There is no unattended real-browser suite today. Any future test that launches an
installed browser must use the `browser` marker; interactive validation remains an
explicit local action.

## Coverage baseline

CI measures line and branch coverage without a `fail_under` threshold. Each Python
version uploads `coverage.xml` and `coverage.json` for 30 days; the Python 3.11 report
from the first successful run on the default branch is the canonical baseline.

This first phase is observational: a guessed global percentage must not block releases.
After the baseline is stable, add explicit floors only for critical modules and set each
floor from its observed line and branch coverage, rounded down to avoid noise. CI should
then fail only when one of those module floors regresses. Repository-wide coverage stays
informational, and any intentional floor reduction requires a reviewed baseline update.
