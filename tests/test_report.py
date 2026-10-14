"""Slim fetch report and ASCII progress line."""

from io import StringIO

from bibvault.models import AcquireResult
from bibvault.pipeline import FetchDisplay, ascii_progress, format_report


def item(key: str, status: str, error: str | None = None) -> AcquireResult:
    return AcquireResult(
        citation_key=key,
        title=None,
        reference_id=f"id:{key}",
        status=status,
        error=error,
    )


def test_slim_report_lists_only_problems():
    text = format_report(
        [
            item("have", "already_exists"),
            item("new", "downloaded"),
            item("site", "unsupported", "web archiving is not part of V0.1"),
            item("miss", "not_found", "no matching publication or PDF found"),
        ]
    )
    assert "Already on disk:      1" in text
    assert "Newly downloaded:     1" in text
    assert "Could not acquire:    1" in text
    assert "Unsupported:          1" in text
    assert "miss (not_found): no matching publication or PDF found" in text
    assert "have" not in text.split("Could not acquire:")[-1]
    assert "new" not in text.split("Could not acquire:")[-1]
    assert "site" not in text.split("Could not acquire:")[-1]


def test_ascii_progress_fills_the_bar():
    assert ascii_progress(0, 4, "Fetching") == "Fetching [--------------------] 0/4"
    assert ascii_progress(2, 4, "Fetching") == "Fetching [##########----------] 2/4"
    assert ascii_progress(4, 4, "Fetching") == "Fetching [####################] 4/4"
    assert ascii_progress(1, 0) == ""


def test_progress_line_stays_above_later_messages():
    stream = StringIO()
    display = FetchDisplay(stream)
    display.progress(1, 4, "Fetching")
    display.status("Press Enter when sign-in (or the bot check) is done.")
    display.progress(2, 4, "Fetching")
    text = stream.getvalue()
    assert text.startswith("Fetching [#####---------------] 1/4\n")
    assert "Press Enter when sign-in (or the bot check) is done." in text
    assert f"\033[{display.lines_below + 1}A" in text
    assert "Fetching [##########----------] 2/4" in text
    assert text.index("Press Enter") < text.index("\033[")
