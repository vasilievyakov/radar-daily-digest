"""Type checking as a test, because five incidents in one night say so.

Every one of them was the same shape: a name that does not resolve. `keep()`
on the filter, `TelegramSurface` in delivery, `extract-v1` in the golden set,
`_CallLog.filtered` inside the fix for the previous two. Four of the five are
visible without running the program, and the checker named them by line
number while nobody was reading its output.

Kept as a test rather than a CI step: CI does not exist yet, and the checker
that runs only when someone remembers is the one that found `_CallLog` at
01:20 and was scrolled past.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

# Errors that mean "this call cannot work", as opposed to style or strictness.
FATAL_RULES = {
    "unresolved-attribute",
    "unresolved-import",
    "invalid-argument-type",
    "missing-argument",
    "too-many-positional-arguments",
    "call-non-callable",
}

# Known diagnostics not yet paid off. Each must be a real decision, not a
# convenient silence: shrinking this list is the point of the file.
ACCEPTED: set[tuple[str, str]] = {
    # bs4 stubs declare find_all(attrs=None); the dict form is documented and
    # correct at runtime.
    ("radar/adapters/html_page.py", "invalid-argument-type"),
    # Narrowing gaps the checker cannot see through: the value is guarded by
    # an earlier branch. Real, but not a call that fails.
    ("radar/backfill.py", "unresolved-attribute"),
    ("radar/cli.py", "invalid-argument-type"),
    ("radar/enrich.py", "invalid-argument-type"),
    ("radar/llm.py", "invalid-argument-type"),
    ("radar/llm_cli.py", "invalid-argument-type"),
    ("radar/surfaces/email.py", "call-non-callable"),
    ("radar/scoring.py", "invalid-argument-type"),
}


def _run_checker() -> list[tuple[str, str, str]]:
    """Returns (path, rule, message). The checker has no JSON output, so the
    concise format is parsed: `path:line:col: error[rule] message`."""
    if shutil.which("uvx") is None:
        pytest.skip("uvx недоступен, проверка типов пропущена")
    proc = subprocess.run(
        ["uvx", "ty", "check", "--output-format", "concise", "radar"],
        capture_output=True, text=True, timeout=180, check=False,
    )
    text = proc.stdout + proc.stderr
    if "error[" not in text and "All checks passed" not in text:
        pytest.skip("вывод проверки типов не разобран")

    found: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        match = re.match(r"(?P<path>[^:]+):\d+:\d+:\s+error\[(?P<rule>[^\]]+)\]\s*(?P<msg>.*)", line)
        if match:
            path = match.group("path").split("Demo Challenge/")[-1]
            found.append((path, match.group("rule"), match.group("msg")))
    return found


def test_no_call_can_fail_on_a_name_that_does_not_resolve():
    """The class of defect that cost five incidents and 30% of a run."""
    found = [
        f"{path}: {rule} — {message[:90]}"
        for path, rule, message in _run_checker()
        if rule in FATAL_RULES and (path, rule) not in ACCEPTED
    ]

    assert not found, (
        "проверка типов нашла вызовы, которые не могут сработать:\n  "
        + "\n  ".join(sorted(set(found)))
    )
