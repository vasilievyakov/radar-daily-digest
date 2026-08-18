"""Russian agreement, in the core rather than in one surface.

The helper existed in `radar/surfaces/email.py` and nowhere the core could
reach it, so `why_it_matters` — the one line on a card written to make somebody
get up and fix something — printed "срок наступает через 1 дней" on fourteen
cards out of thirty-four. Two lines above it, on the same card, a surface that
had the helper printed "через 241 день" correctly.

Text the core composes is the core's to get right: three surfaces rendering the
same Signal must not each re-derive the grammar of a number.
"""

from __future__ import annotations


def plural(n: int, one: str, few: str, many: str) -> str:
    """Pick the form Russian wants for `n`.

    Eleven through fourteen take the many-form regardless of the last digit,
    which is the rule a naive implementation misses.
    """
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        return many
    last = tail % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def count(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural(n, one, few, many)}"


def days(n: int) -> str:
    """«1 день», «2 дня», «11 дней»."""
    return count(n, "день", "дня", "дней")


# A model name as vendors write it: lowercase, with a version glued on. In this
# domain a string differing by case is a different model, so it is never
# touched — `o4-mini` capitalised to `O4-mini` names nothing.
_re = __import__("re")
_IDENTIFIER = _re.compile(r"^[a-z][\w.@/-]*$")


def _looks_like_an_identifier(word: str) -> bool:
    """Lowercase, carries a digit, and joined by a hyphen, dot or slash.

    Covers o4-mini, gpt-4.1-nano, claude-3-opus, veo-3.1, text-embedding-004
    and openai/openai-python, and leaves ordinary Russian and English words
    alone because they have no digit in them.
    """
    return bool(
        _IDENTIFIER.match(word)
        and any(ch.isdigit() for ch in word)
        and any(ch in "-./" for ch in word)
    )


def sentence(text: str) -> str:
    """Capitalise the first letter, unless the first word is an identifier.

    `str.capitalize()` lowercases everything after it, which turns
    "затронуто: CLAUDE-3-OPUS" into "Затронуто: claude-3-opus". And raising the
    first letter unconditionally turned `gpt-4.1-nano` into `Gpt-4.1-nano` at
    the head of a clause — the page renaming the thing it reports on.
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    first = text.split(" ", 1)[0].rstrip(":,.")
    if _looks_like_an_identifier(first):
        return text
    return text[:1].upper() + text[1:]
