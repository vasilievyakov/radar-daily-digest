"""Source discovery: finding what we should be watching and are not.

Deliberately not a pipeline stage. A fast-growing repository is not a change
in a tracked product: it has no event date and no fact to extract, and pushing
it into the digest would be exactly the "AI news in general" the PRD rules out
in section 2.3.

What it is instead is a source of sources. The vendor list is currently frozen
by hand, and in six months it will be wrong — a radar that fails to notice the
next Cursor appearing is blind in the way that matters most. This process
proposes additions to the config and never writes a signal.

Growth is computed here rather than taken from the API, because GitHub has no
"trending" endpoint and sorting by stars just returns whichever projects were
already large. Snapshots are stored per run, and the delta between them is the
only honest growth number available.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SCOUT_DDL = """
CREATE TABLE IF NOT EXISTS repo_snapshots (
    full_name   TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    stars       INTEGER NOT NULL,
    pushed_at   TEXT,
    topics_json TEXT NOT NULL DEFAULT '[]',
    has_releases INTEGER,
    PRIMARY KEY (full_name, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_repo_snapshots_name ON repo_snapshots(full_name, observed_at);

CREATE TABLE IF NOT EXISTS source_candidates (
    full_name    TEXT PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    stars        INTEGER NOT NULL,
    stars_gained INTEGER NOT NULL DEFAULT 0,
    release_count INTEGER NOT NULL DEFAULT 0,
    topics_json  TEXT NOT NULL DEFAULT '[]',
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'proposed',
    note         TEXT
);
"""

DEFAULT_TOPICS = ["llm", "ai-agents", "mcp", "rag", "llmops", "agentic-ai"]


@dataclass(slots=True)
class RepoObservation:
    full_name: str
    stars: int
    pushed_at: str | None
    topics: list[str] = field(default_factory=list)
    description: str = ""
    html_url: str = ""


@dataclass(slots=True)
class Candidate:
    full_name: str
    stars: int
    stars_gained: int
    release_count: int
    description: str
    topics: list[str]
    reason: str

    def as_source_yaml(self, vendor: str = "TODO") -> str:
        """Ready to paste into the config once a human approves the vendor."""
        slug = self.full_name.replace("/", "_").replace("-", "_").lower()
        return (
            f"  - id: gh_{slug}\n"
            f"    type: github_releases\n"
            f"    url: https://github.com/{self.full_name}\n"
            f"    vendor: {vendor}\n"
            f"    priority: 3\n"
            f"    enabled: true\n"
            f"    backfill_supported: true\n"
            f"    backfill_depth_days: 360\n"
            f"    min_expected_items: 3\n"
        )


def gh_json(args: list[str], timeout: int = 60) -> Any:
    """Call the gh CLI. Returns None on any failure rather than raising."""
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def search_repos(
    topic: str,
    min_stars: int = 2000,
    pushed_since: str | None = None,
    per_page: int = 50,
) -> list[RepoObservation]:
    """One topic per call: GitHub search does not support OR across qualifiers."""
    query = f"topic:{topic} stars:>{min_stars}"
    if pushed_since:
        query += f" pushed:>{pushed_since}"
    payload = gh_json(
        [
            "api",
            "-X",
            "GET",
            "search/repositories",
            "-f",
            f"q={query}",
            "-f",
            "sort=stars",
            "-f",
            "order=desc",
            "-f",
            f"per_page={per_page}",
        ]
    )
    if not isinstance(payload, dict) or "items" not in payload:
        return []
    return [
        RepoObservation(
            full_name=item["full_name"],
            stars=item.get("stargazers_count", 0),
            pushed_at=item.get("pushed_at"),
            topics=item.get("topics", []),
            description=item.get("description") or "",
            html_url=item.get("html_url", ""),
        )
        for item in payload["items"]
    ]


def release_count(full_name: str) -> int:
    payload = gh_json(["api", f"repos/{full_name}/releases?per_page=100"])
    return len(payload) if isinstance(payload, list) else 0


class Scout:
    def __init__(
        self, conn: sqlite3.Connection, config: dict[str, Any] | None = None
    ) -> None:
        self.conn = conn
        cfg = config or {}
        self.topics: list[str] = cfg.get("topics", DEFAULT_TOPICS)
        self.min_stars: int = int(cfg.get("min_stars", 2000))
        self.min_stars_gained: int = int(cfg.get("min_stars_gained", 300))
        self.min_releases: int = int(cfg.get("min_releases", 3))
        self.conn.executescript(SCOUT_DDL)
        self.conn.commit()

    def record(
        self, observations: list[RepoObservation], now: datetime | None = None
    ) -> int:
        now = now or datetime.now(UTC)
        stamp = now.isoformat()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO repo_snapshots (full_name, observed_at, stars, pushed_at, "
                "topics_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(full_name, observed_at) DO UPDATE SET stars = excluded.stars",
                [
                    (o.full_name, stamp, o.stars, o.pushed_at, json.dumps(o.topics))
                    for o in observations
                ],
            )
        return len(observations)

    def growth(self, full_name: str, window_days: int = 30) -> int | None:
        """Stars gained since the earliest snapshot inside the window.

        None means there is only one snapshot: no growth is knowable yet, and
        reporting zero would read as "this project is not growing".
        """
        rows = self.conn.execute(
            "SELECT stars, observed_at FROM repo_snapshots WHERE full_name = ? "
            "AND observed_at >= ? ORDER BY observed_at ASC",
            (full_name, (datetime.now(UTC) - timedelta(days=window_days)).isoformat()),
        ).fetchall()
        if len(rows) < 2:
            return None
        return rows[-1]["stars"] - rows[0]["stars"]

    def propose(
        self,
        observations: list[RepoObservation],
        known_urls: set[str],
        check_releases: bool = True,
    ) -> list[Candidate]:
        """Repositories worth watching that the config does not already cover."""
        known_slugs = {
            url.rstrip("/").split("github.com/")[-1].lower()
            for url in known_urls
            if "github.com/" in url
        }
        candidates: list[Candidate] = []
        for obs in observations:
            if obs.full_name.lower() in known_slugs:
                continue
            gained = self.growth(obs.full_name)
            releases = release_count(obs.full_name) if check_releases else 0
            # A project without releases has no structured dated history, so
            # it cannot become a github_releases source however popular it is.
            if check_releases and releases < self.min_releases:
                continue
            if gained is not None and gained < self.min_stars_gained:
                continue
            reason = (
                f"прирост {gained} звёзд за месяц, релизов {releases}"
                if gained is not None
                else f"первое наблюдение, {obs.stars} звёзд, релизов {releases}"
            )
            candidates.append(
                Candidate(
                    full_name=obs.full_name,
                    stars=obs.stars,
                    stars_gained=gained or 0,
                    release_count=releases,
                    description=obs.description,
                    topics=obs.topics,
                    reason=reason,
                )
            )
        candidates.sort(key=lambda c: (-c.stars_gained, -c.stars))
        self._save(candidates)
        return candidates

    def _save(self, candidates: list[Candidate]) -> None:
        stamp = datetime.now(UTC).isoformat()
        with self.conn:
            for c in candidates:
                self.conn.execute(
                    "INSERT INTO source_candidates (full_name, first_seen, last_seen, stars, "
                    "stars_gained, release_count, topics_json, description) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(full_name) DO UPDATE SET last_seen = excluded.last_seen, "
                    "stars = excluded.stars, stars_gained = excluded.stars_gained, "
                    "release_count = excluded.release_count",
                    (
                        c.full_name,
                        stamp,
                        stamp,
                        c.stars,
                        c.stars_gained,
                        c.release_count,
                        json.dumps(c.topics),
                        c.description,
                    ),
                )

    def pending(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM source_candidates WHERE status = 'proposed' "
                "ORDER BY stars_gained DESC, stars DESC"
            ).fetchall()
        ]

    def decide(self, full_name: str, status: str, note: str | None = None) -> None:
        """Record a human decision so a rejected project stops resurfacing."""
        with self.conn:
            self.conn.execute(
                "UPDATE source_candidates SET status = ?, note = ? WHERE full_name = ?",
                (status, note, full_name),
            )

    def sweep(
        self, known_urls: set[str], check_releases: bool = True
    ) -> list[Candidate]:
        """One full pass over every configured topic."""
        seen: dict[str, RepoObservation] = {}
        for topic in self.topics:
            for obs in search_repos(topic, self.min_stars):
                seen.setdefault(obs.full_name, obs)
        self.record(list(seen.values()))
        rejected = {
            r["full_name"]
            for r in self.conn.execute(
                "SELECT full_name FROM source_candidates WHERE status = 'rejected'"
            ).fetchall()
        }
        fresh = [o for o in seen.values() if o.full_name not in rejected]
        return self.propose(fresh, known_urls, check_releases=check_releases)
