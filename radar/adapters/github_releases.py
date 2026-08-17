"""GitHub Releases adapter (FR-1.1).

A release page is the one place where a tool states, in its own words and on a
dated record, what changed. The adapter copies that record verbatim: the body
is markdown as published, never summarised here, because everything downstream
verifies evidence against the archived text.

The adapter never raises. A repository that publishes no releases is a valid
answer, not a failure, and a rate limit truncates the walk instead of losing
the pages already paid for. Whatever went wrong is recorded in `self.extra`
for the run log, since an empty list alone cannot say why it is empty.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from radar.adapters.base import Adapter, CollectedItem, SourceConfig
from radar.fetch import Fetcher
from radar.models import DatePrecision

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
GITHUB_HOSTS = {"github.com", "www.github.com", "api.github.com"}


def parse_repo(url: str) -> tuple[str, str] | None:
    """`owner/repo` out of whatever form the config carries.

    Accepts the browser URL, the API URL, an scp-style git remote and a bare
    `owner/repo`. Anything hosted elsewhere is not a GitHub source.
    """
    text = (url or "").strip()
    if not text:
        return None

    if "://" in text:
        parts = urlsplit(text)
        host = parts.netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0]
        path = parts.path
    elif "@" in text and ":" in text.split("@", 1)[1]:
        host_part, _, path = text.split("@", 1)[1].partition(":")
        host = host_part.lower()
        path = f"/{path}"
    else:
        host = ""
        path = text

    if host and host not in GITHUB_HOSTS:
        return None

    segments = [s for s in path.split("/") if s]
    # API URLs carry the repo one level deeper: /repos/{owner}/{repo}/...
    if host == "api.github.com" and segments and segments[0] == "repos":
        segments = segments[1:]
    if len(segments) < 2:
        return None

    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return owner, repo


def resolve_token() -> str | None:
    """Token from the environment, then from an authenticated `gh` (NFR-11).

    Never from the theme config: a source entry is committed to the repo and a
    token must not be.
    """
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        return token
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _header(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


class GitHubReleasesAdapter(Adapter):
    type = "github_releases"

    # One page per request is the whole cost model here: 100 releases is the
    # API maximum, and max_pages caps a walk that never meets its stop rule.
    per_page = 100
    max_pages = 50

    def __init__(self, source: SourceConfig, fetcher: Fetcher) -> None:
        super().__init__(source, fetcher)
        self.repo = parse_repo(source.url)
        self.extra: dict[str, Any] = {}
        self._token_resolved = False
        self._token_value: str | None = None

    def collect(self, since: datetime | None = None) -> list[CollectedItem]:
        return self._walk(cutoff=since, inclusive=False)

    def backfill(self, depth_days: int | None = None) -> list[CollectedItem]:
        depth = self.source.backfill_depth_days if depth_days is None else depth_days
        cutoff = (
            datetime.now(UTC) - timedelta(days=depth) if depth and depth > 0 else None
        )
        return self._walk(cutoff=cutoff, inclusive=True)

    def _token(self) -> str | None:
        if not self._token_resolved:
            self._token_resolved = True
            self._token_value = resolve_token()
            if self._token_value is None:
                # 60 requests/hour instead of 5000 turns a backfill into a
                # silent partial walk, so the absence is stated, not implied.
                log.warning(
                    "%s: no GITHUB_TOKEN and no token from `gh auth token`; "
                    "GitHub API is limited to 60 requests/hour instead of 5000",
                    self.source.id,
                )
        return self._token_value

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _is_rate_limited(status_code: int, headers: dict[str, str]) -> bool:
        if status_code not in (403, 429):
            return False
        remaining = _header(headers, "X-RateLimit-Remaining")
        # A 429 without the counter is a secondary limit; both mean stop asking.
        return remaining == "0" or (status_code == 429 and remaining is None)

    def _page_url(self, owner: str, repo: str, page: int) -> str:
        # The page number lives in the query string, and the cache keys on the
        # canonical URL, so every page is its own cache entry without
        # cache_key_extra.
        return (
            f"{API_ROOT}/repos/{owner}/{repo}/releases"
            f"?per_page={self.per_page}&page={page}"
        )

    def _walk(self, cutoff: datetime | None, inclusive: bool) -> list[CollectedItem]:
        self.extra = {"pages_fetched": 0, "releases_seen": 0}
        if self.repo is None:
            self.extra["error"] = f"not a github repository url: {self.source.url!r}"
            log.warning("%s: %s", self.source.id, self.extra["error"])
            return []

        owner, repo = self.repo
        self.extra["repo"] = f"{owner}/{repo}"
        if cutoff is not None and cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)

        items: list[CollectedItem] = []
        drafts = 0
        seen = 0

        for page in range(1, self.max_pages + 1):
            result = self.fetcher.get(
                self._page_url(owner, repo, page), headers=self._headers()
            )
            self.extra["pages_fetched"] = page

            if self._is_rate_limited(result.status_code, result.headers):
                self.extra["rate_limited"] = True
                self.extra["error"] = (
                    f"rate limited on page {page} (HTTP {result.status_code})"
                )
                log.warning(
                    "%s: %s, keeping %d releases collected so far",
                    self.source.id,
                    self.extra["error"],
                    len(items),
                )
                break

            if not result.ok:
                self.extra["http_status"] = result.status_code
                self.extra["error"] = result.error or f"HTTP {result.status_code}"
                log.warning(
                    "%s: page %d failed: %s", self.source.id, page, self.extra["error"]
                )
                break

            try:
                payload = json.loads(result.text)
            except json.JSONDecodeError as exc:
                self.extra["error"] = f"page {page} is not valid JSON: {exc}"
                log.warning("%s: %s", self.source.id, self.extra["error"])
                break
            if not isinstance(payload, list):
                message = payload.get("message") if isinstance(payload, dict) else None
                self.extra["error"] = message or f"page {page} is not a release list"
                log.warning("%s: %s", self.source.id, self.extra["error"])
                break

            seen += len(payload)
            page_has_fresh = False
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                published = _parse_dt(raw.get("published_at")) or _parse_dt(
                    raw.get("created_at")
                )
                # Age decides where paging stops, so drafts count towards it.
                if cutoff is None or published is None:
                    page_has_fresh = True
                elif published > cutoff or (inclusive and published == cutoff):
                    page_has_fresh = True
                else:
                    continue

                if raw.get("draft"):
                    drafts += 1
                    continue
                items.append(self._to_item(raw, owner, repo, result.ref, page))

            if len(payload) < self.per_page:
                break
            if cutoff is not None and not page_has_fresh:
                break
        else:
            self.extra["truncated_at_max_pages"] = True

        self.extra["releases_seen"] = seen
        self.extra["drafts_skipped"] = drafts
        if seen == 0 and "error" not in self.extra:
            # A repo that ships through tags only answers 200 with an empty
            # list. Valid, but worth seeing in the run log (FR-1.4).
            self.extra["empty"] = True
            log.warning("%s: %s/%s published no releases", self.source.id, owner, repo)
        return items

    def _to_item(
        self, raw: dict[str, Any], owner: str, repo: str, ref: str, page: int
    ) -> CollectedItem:
        tag = str(raw.get("tag_name") or "").strip()
        name = str(raw.get("name") or "").strip()
        published = _parse_dt(raw.get("published_at")) or _parse_dt(
            raw.get("created_at")
        )
        html_url = str(
            raw.get("html_url")
            or f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
        )
        extra: dict[str, Any] = {
            "repo": f"{owner}/{repo}",
            "tag": tag,
            "page": page,
            "release_id": raw.get("id"),
        }
        if raw.get("prerelease"):
            # Kept: a prerelease still announces a breaking change, and only
            # scoring downstream may decide it weighs less.
            extra["prerelease"] = True
        if raw.get("published_at") in (None, "") and published is not None:
            extra["date_from"] = "created_at"

        return CollectedItem(
            url=html_url,
            title=name or tag or f"{owner}/{repo} release",
            raw_text=str(raw.get("body") or ""),
            published_at=published,
            event_date=published.date() if published else None,
            date_precision=DatePrecision.DAY,
            raw_material_ref=ref,
            vendor_hint=owner,
            version_hint=tag or None,
            extra=extra,
        )
