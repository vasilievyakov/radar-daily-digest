"""Adapter for documentation pages scraped as HTML (`html_scrape`).

The sources in config come in two shapes, both stated in `parser_hint`:

* `dated_sections` - one heading per event ("August 11, 2026",
  "2026-06-11: GPT-5 and o3 model deprecations"), body until the next heading.
* `dated_table` - a table whose rows carry dates ("Retirement date",
  "Deprecated"), one row per event.

Two properties the rest of the pipeline leans on:

* `raw_text` is a literal slice of the page text, never a re-joined one. A
  fact's evidence is verified by substring against the material it came from
  (FR-4.3), so any rewriting here turns into an unverifiable fact later.
* the adapter never supplies a year. A heading that reads "Mar 14" either
  borrows its year from an enclosing heading or stays undated and marked
  INFERRED. Filling in the current year is the failure that silently corrupts
  a backfill crossing a year boundary, and it is invisible afterwards.

HTTP 200 with zero sections is not success: the adapter returns an empty list
and the caller compares it against `min_expected_items` (FR-1.4). Network and
parse failures come out the same way, never as an exception.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype
from bs4 import NavigableString, ProcessingInstruction, Tag

from radar.adapters.base import Adapter, CollectedItem, SourceConfig
from radar.fetch import Fetcher
from radar.models import DatePrecision

PARSER_SECTIONS = "dated_sections"
PARSER_TABLE = "dated_table"

# A grouping heading ("2026") owns no event of its own; only its children do.
MIN_SECTION_BODY_CHARS = 16
# One release-notes page is 1.6 MB. Sections keep it splittable; the cap keeps
# a single runaway section from becoming the whole page again.
MAX_SECTION_CHARS = 20_000

_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "path",
        "canvas",
        "nav",
        "header",
        "footer",
        "form",
        "input",
        "select",
        "option",
        "textarea",
        "iframe",
        "video",
        "audio",
        "source",
        "picture",
        "link",
        "meta",
        "head",
        "img",
    }
)
# `button` is deliberately not skipped: docs.claude.com renders model ids as
# copy-to-clipboard buttons, and dropping them empties the first table column.
# Icon-only buttons carry aria-hidden glyphs and drop out on their own.
_SKIP_ROLES = frozenset({"navigation", "banner", "contentinfo", "search"})
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "details",
        "div",
        "dd",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_CONTENT_SELECTORS = (
    "main",
    "article",
    "[role=main]",
    "#content",
    "#main-content",
    "#mainContent",
    ".markdown-body",
    ".prose",
)
_NON_TEXT = (Comment, Doctype, Declaration, ProcessingInstruction, CData)
# React and Radix hand out ids like "_R_17dkl..." that are not stable anchors.
_ANCHOR_RE = re.compile(r"^[\w][\w.:-]*$")
_ANCHOR_JUNK_RE = re.compile(r"(^|-)_[Rr]_|^radix-")
_PUA_RE = re.compile("[\ue000-\uf8ff]")


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MON = (
    r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
_ORD = r"(?:st|nd|rd|th)?"

# Order is the whole point: the most specific pattern that matches wins, so
# "May 2026" never degrades into a year and "May 1, 2026" never loses its day.
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"), "ymd"),
    (
        re.compile(rf"{_MON}\.?\s+(?P<d>\d{{1,2}}){_ORD},?\s+(?P<y>\d{{4}})\b", re.I),
        "mdy",
    ),
    (
        re.compile(rf"(?P<d>\d{{1,2}}){_ORD}\s+{_MON}\.?,?\s+(?P<y>\d{{4}})\b", re.I),
        "dmy",
    ),
    (re.compile(rf"{_MON}\.?\s+(?P<y>\d{{4}})\b", re.I), "my"),
    (re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})(?!\d|-)"), "ym"),
    (re.compile(rf"{_MON}\.?\s+(?P<d>\d{{1,2}}){_ORD}(?!\d)", re.I), "md"),
    (re.compile(rf"(?P<d>\d{{1,2}}){_ORD}\s+{_MON}\.?(?!\w)", re.I), "dm"),
    (re.compile(r"^\W*(?P<y>20\d{2})\b"), "y"),
)


@dataclass(slots=True)
class ParsedDate:
    """A date as the page states it, with the parts the page did not state."""

    value: date | None
    precision: DatePrecision
    text: str
    month: int | None = None
    day: int | None = None
    year_missing: bool = False

    def with_year(self, year: int) -> ParsedDate:
        """Rebuild a year-less date once an enclosing heading supplies one."""
        if not self.year_missing or self.month is None:
            return self
        try:
            value = date(year, self.month, self.day or 1)
        except ValueError:
            return self
        precision = DatePrecision.DAY if self.day else DatePrecision.MONTH
        return ParsedDate(
            value=value,
            precision=precision,
            text=self.text,
            month=self.month,
            day=self.day,
            year_missing=False,
        )


def parse_date_fragment(text: str) -> ParsedDate | None:
    """First date stated in `text`, or None. Never invents a missing year."""
    if not text:
        return None
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        month = _MONTHS.get((groups.get("mon") or "").lower()) or (
            int(groups["m"]) if groups.get("m") else None
        )
        day = int(groups["d"]) if groups.get("d") else None
        year = int(groups["y"]) if groups.get("y") else None
        if month is not None and not 1 <= month <= 12:
            continue
        if kind in {"md", "dm"}:
            # No year on the page: month and day travel, the year does not.
            return ParsedDate(
                value=None,
                precision=DatePrecision.INFERRED,
                text=match.group(0),
                month=month,
                day=day,
                year_missing=True,
            )
        if year is None:
            continue
        if kind == "y":
            return ParsedDate(date(year, 1, 1), DatePrecision.YEAR, match.group(0))
        try:
            value = date(year, month or 1, day or 1)
        except ValueError:
            continue
        precision = DatePrecision.DAY if day else DatePrecision.MONTH
        return ParsedDate(value, precision, match.group(0), month, day)
    return None


def period_end(value: date, precision: DatePrecision) -> date:
    """Last day the stated date can still mean, for coarse precisions."""
    if precision is DatePrecision.MONTH:
        return date(
            value.year, value.month, calendar.monthrange(value.year, value.month)[1]
        )
    if precision is DatePrecision.YEAR:
        return date(value.year, 12, 31)
    return value


# --------------------------------------------------------------------------
# html to text, with offsets
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Heading:
    level: int
    title: str
    anchor: str | None
    start: int
    end: int


@dataclass(slots=True)
class Row:
    start: int
    end: int
    cells: list[tuple[int, int]]
    is_header: bool


@dataclass(slots=True)
class Table:
    rows: list[Row] = field(default_factory=list)


@dataclass(slots=True)
class PageText:
    """The page as one text plus offsets into it. Every slice is verbatim."""

    text: str
    headings: list[Heading] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    def slice(self, start: int, end: int) -> str:
        return self.text[start:end].strip()


class _TextBuilder:
    """Flattens a subtree to text while remembering where things landed."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._tail = ""
        self.length = 0
        self.headings: list[Heading] = []
        self.tables: list[Table] = []
        self._table_stack: list[Table] = []

    def build(self, root: Tag) -> PageText:
        self._visit(root)
        return PageText("".join(self._parts), self.headings, self.tables)

    def _append(self, text: str) -> None:
        if not text:
            return
        if self._tail in ("", "\n", " ") and text.startswith(" "):
            text = text.lstrip(" ")
            if not text:
                return
        self._parts.append(text)
        self.length += len(text)
        self._tail = text[-1]

    def _newline(self) -> None:
        if self.length == 0 or self._tail == "\n":
            return
        self._parts.append("\n")
        self.length += 1
        self._tail = "\n"

    def _visit(self, node: object) -> None:
        if isinstance(node, _NON_TEXT):
            return
        if isinstance(node, NavigableString):
            self._append(re.sub(r"\s+", " ", str(node)))
            return
        if not isinstance(node, Tag):
            return
        name = (node.name or "").lower()
        if name in _SKIP_TAGS:
            return
        if node.get("aria-hidden") == "true" or node.get("hidden") is not None:
            return
        role = node.get("role")
        if isinstance(role, str) and role.lower() in _SKIP_ROLES:
            return

        if name in ("td", "th") and node.find_previous_sibling(["td", "th"]):
            self._append(" | ")
        if name == "br":
            self._newline()
            return
        if name in _BLOCK_TAGS:
            self._newline()
        if name == "table":
            self._table_stack.append(Table())
        if name == "tr" and self._table_stack:
            self._table_stack[-1].rows.append(Row(0, 0, [], False))

        start = self.length
        if name == "pre":
            self._append(node.get_text())
        else:
            for child in node.children:
                self._visit(child)

        if name in _HEADING_TAGS:
            title = _clean_title(self._parts, start, self.length)
            if title:
                self.headings.append(
                    Heading(int(name[1]), title, _anchor_of(node), start, self.length)
                )
        elif name in ("td", "th"):
            if self._table_stack and self._table_stack[-1].rows:
                self._table_stack[-1].rows[-1].cells.append((start, self.length))
        elif name == "tr":
            if self._table_stack:
                row = self._table_stack[-1].rows[-1]
                row.start, row.end = start, self.length
                row.is_header = bool(node.find("th")) and not node.find("td")
        elif name == "table":
            table = self._table_stack.pop()
            if table.rows:
                self.tables.append(table)

        if name in _BLOCK_TAGS:
            self._newline()


def _clean_title(parts: list[str], start: int, end: int) -> str:
    text = "".join(parts)[start:end]
    return re.sub(r"\s+", " ", _PUA_RE.sub("", text)).strip()


def _anchor_of(node: Tag) -> str | None:
    """Anchor id of a heading. Mintlify puts it on a div inside the heading."""
    candidates = [node.get("id")]
    candidates.extend(el.get("id") for el in node.find_all(attrs={"id": True}))
    for value in candidates:
        if not isinstance(value, str):
            continue
        if _ANCHOR_RE.match(value) and not _ANCHOR_JUNK_RE.search(value):
            return value
    return None


def _content_root(soup: BeautifulSoup) -> Tag:
    """Largest plausible content container; body when nothing stands out."""
    best: Tag | None = None
    best_size = 0
    for selector in _CONTENT_SELECTORS:
        try:
            found = soup.select(selector)
        except Exception:  # malformed selector support differs across versions
            continue
        for element in found:
            size = len(element.get_text(" ", strip=True))
            if size > best_size:
                best, best_size = element, size
    body = soup.body or soup
    if best is None or best_size < 200:
        return body  # type: ignore[return-value]
    return best


def extract_page_text(html: str) -> PageText:
    """Page text plus heading and table offsets into it."""
    soup = BeautifulSoup(html, "lxml")
    builder = _TextBuilder()
    return builder.build(_content_root(soup))


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

# Which column of a table carries the date of the event. "Deprecated" beats
# "Retirement date": the announcement is the event, the retirement is a fact
# inside it. A row falls back to the next-best column when the first is "N/A".
_DATE_HEADER_RANKS = (
    (
        "announced",
        "announcement",
        "notice",
        "deprecated",
        "deprecation",
        "published",
        "released",
    ),
    (
        "retirement",
        "retired",
        "sunset",
        "shutdown",
        "end of life",
        "eol",
        "effective",
        "expires",
    ),
    ("date",),
)


def _header_rank(header: str) -> int:
    lowered = header.lower()
    for rank, keywords in enumerate(_DATE_HEADER_RANKS):
        if any(keyword in lowered for keyword in keywords):
            return rank
    return len(_DATE_HEADER_RANKS)


@dataclass(slots=True)
class Section:
    title: str
    text: str
    anchor: str | None
    event_date: date | None
    precision: DatePrecision
    date_text: str
    year_source: str | None = None


def _ancestor_year(
    headings: list[Heading], dates: list[ParsedDate | None], index: int
) -> tuple[int, str] | None:
    """Year from the nearest enclosing heading, never from a sibling.

    A sibling would be the neighbouring entry in the list, and on a page
    ordered newest first the neighbour above "Dec 14" is January of the next
    year. Only a heading of a higher level actually contains this one.
    """
    level = headings[index].level
    for j in range(index - 1, -1, -1):
        if headings[j].level >= level:
            continue
        level = headings[j].level
        parsed = dates[j]
        if parsed is not None and parsed.value is not None:
            return parsed.value.year, headings[j].title
    return None


def sections_from_headings(page: PageText) -> list[Section]:
    """One section per dated heading, body until the next boundary heading."""
    headings = page.headings
    dates = [parse_date_fragment(h.title) for h in headings]
    sections: list[Section] = []
    for i, heading in enumerate(headings):
        parsed = dates[i]
        if parsed is None:
            continue
        year_source: str | None = None
        if parsed.year_missing:
            found = _ancestor_year(headings, dates, i)
            if found is not None:
                parsed = parsed.with_year(found[0])
                year_source = found[1]
        # A section stops at the next heading that is not nested inside it, and
        # at any dated heading, so a year header never swallows its own months.
        end = len(page.text)
        for j in range(i + 1, len(headings)):
            if headings[j].level <= heading.level or dates[j] is not None:
                end = headings[j].start
                break
        body = page.text[heading.end : end].strip()
        if len(body) < MIN_SECTION_BODY_CHARS:
            continue  # grouping heading, its children carry the events
        text = page.text[heading.start : min(end, heading.start + MAX_SECTION_CHARS)]
        sections.append(
            Section(
                title=heading.title,
                text=text.strip(),
                anchor=heading.anchor,
                event_date=parsed.value,
                precision=parsed.precision,
                date_text=parsed.text,
                year_source=year_source,
            )
        )
    return sections


def sections_from_tables(page: PageText) -> list[Section]:
    """One section per table row that states a date."""
    sections: list[Section] = []
    for table in page.tables:
        headers: list[str] = []
        for row in table.rows:
            if row.is_header:
                headers = [page.slice(*cell) for cell in row.cells]
                break
        ranks = [_header_rank(h) for h in headers]
        for row in table.rows:
            if row.is_header or not row.cells:
                continue
            best: tuple[int, int, ParsedDate] | None = None
            for index, cell in enumerate(row.cells):
                parsed = parse_date_fragment(page.slice(*cell))
                # A table cell has no enclosing heading to borrow a year from,
                # so a year-less cell is left alone rather than guessed at.
                if parsed is None or parsed.value is None:
                    continue
                rank = ranks[index] if index < len(ranks) else len(_DATE_HEADER_RANKS)
                if best is None or (rank, index) < (best[0], best[1]):
                    best = (rank, index, parsed)
            if best is None:
                continue
            date_index, parsed = best[1], best[2]
            heading = _heading_before(page, row.start)
            label = next(
                (
                    page.slice(*cell)
                    for index, cell in enumerate(row.cells)
                    if index != date_index and page.slice(*cell)
                ),
                "",
            )
            title = " - ".join(
                part for part in (heading.title if heading else "", label) if part
            )
            sections.append(
                Section(
                    title=title or parsed.text,
                    text=page.slice(row.start, row.end),
                    anchor=heading.anchor if heading else None,
                    event_date=parsed.value,
                    precision=parsed.precision,
                    date_text=parsed.text,
                )
            )
    return sections


def _heading_before(page: PageText, offset: int) -> Heading | None:
    found = None
    for heading in page.headings:
        if heading.start >= offset:
            break
        found = heading
    return found


# --------------------------------------------------------------------------
# adapter
# --------------------------------------------------------------------------


class HtmlPageAdapter(Adapter):
    """Documentation page split into dated sections."""

    type = "html_scrape"

    def __init__(self, source: SourceConfig, fetcher: Fetcher) -> None:
        super().__init__(source, fetcher)

    def collect(self, since: datetime | None = None) -> list[CollectedItem]:
        items = self._parse()
        if since is None:
            return items
        cutoff = since.date()
        return [item for item in items if _reaches(item, cutoff)]

    def backfill(self, depth_days: int | None = None) -> list[CollectedItem]:
        items = self._parse()
        depth = (
            depth_days if depth_days is not None else self.source.backfill_depth_days
        )
        if not depth or depth <= 0:
            return items
        cutoff = date.today() - timedelta(days=depth)
        return [item for item in items if _reaches(item, cutoff)]

    def _parse(self) -> list[CollectedItem]:
        try:
            result = self.fetcher.get(self.source.url)
        except Exception:  # a broken source is data, not a crash (FR-1.5)
            return []
        if not result.ok or not result.text:
            return []
        try:
            page = extract_page_text(result.text)
            sections = self._sections(page)
        except Exception:
            return []
        base_url = result.url or self.source.url
        items = [self._to_item(section, base_url, result.ref) for section in sections]
        # Newest first; sections whose date could not be resolved go last but
        # are not dropped - they are the ones a human has to look at.
        items.sort(
            key=lambda item: (item.event_date is not None, item.event_date or date.min),
            reverse=True,
        )
        return items

    def _sections(self, page: PageText) -> list[Section]:
        hint = (self.source.parser_hint or "").strip().lower()
        order = (
            [sections_from_tables, sections_from_headings]
            if hint == PARSER_TABLE
            else [sections_from_headings, sections_from_tables]
        )
        for strategy in order:
            sections = strategy(page)
            if sections:
                return sections
        return []

    def _to_item(self, section: Section, base_url: str, ref: str) -> CollectedItem:
        url = f"{base_url}#{section.anchor}" if section.anchor else base_url
        extra: dict[str, object] = {
            "source_id": self.source.id,
            "date_text": section.date_text,
        }
        if section.year_source:
            extra["year_from_heading"] = section.year_source
        if section.event_date is None:
            extra["date_unresolved"] = True
        return CollectedItem(
            url=url,
            title=section.title,
            raw_text=section.text,
            event_date=section.event_date,
            date_precision=section.precision,
            raw_material_ref=ref,
            extra=extra,
        )


def _reaches(item: CollectedItem, cutoff: date) -> bool:
    """Undated sections always pass: silence about them hides the year bug."""
    if item.event_date is None:
        return True
    return period_end(item.event_date, item.date_precision) >= cutoff
