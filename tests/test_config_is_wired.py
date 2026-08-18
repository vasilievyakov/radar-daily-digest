"""Every key in a theme file must be read by some line of `radar/`.

Cherny's third form of the same defect. Fifteen instances were counted of code
written and never called; the count missed a whole level, because a key can be
inert too — and an inert key is worse than a missing one. `budget.max_usd_per_call`
documents a second line of defence that does not exist. `llm.max_output_tokens`
carries an incident in its comment and never reaches a client.
`delivery.channels` implies you can turn email off, and you cannot.

The worst of them read as configured during an incident, which is exactly when
somebody is deciding what to change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = sorted((ROOT / "config").glob("*.yaml"))
SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (ROOT / "radar").rglob("*.py")
    if "__pycache__" not in str(path)
)

# Keys read through a loop over a list of dicts, or spelled as a slug elsewhere.
# Each entry is a claim that the value is used, with where to look.
ACCEPTED = {
    "theme",            # ThemeConfig.name/description read via section("theme")
    "corpus",           # section("corpus"), vendors/change_types walked as lists
    "sources",          # walked as a list of dicts by SourceConfig.from_dict
    "vendors",
    "change_types",
    "id", "label", "aliases", "url", "type", "vendor", "priority", "enabled",
    "parser_hint", "backfill_supported", "backfill_depth_days",
    "min_expected_items", "live_collect", "backfill_url_template", "note",
    "channel", "weight", "critical",
}


def _leaves(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, f"{prefix}{key}.")
            yield prefix + str(key)
    elif isinstance(node, list):
        for item in node:
            yield from _leaves(item, prefix)


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_no_key_is_inert(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    inert = []
    for dotted in sorted(set(_leaves(data))):
        leaf = dotted.rstrip(".").split(".")[-1]
        if leaf in ACCEPTED:
            continue
        if f'"{leaf}"' in SOURCE or f"'{leaf}'" in SOURCE:
            continue
        inert.append(dotted.rstrip("."))

    assert not inert, (
        f"{path.name}: ключи, которые ничего не делают — "
        "их читают глазами при разборе инцидента и верят им:\n  "
        + "\n  ".join(inert)
    )
