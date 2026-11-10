"""Library search query language.

A search is free text plus optional field qualifiers, for example::

    wall mount tag:garage model:qwen size:<100 after:2026-10-01

Free text is matched against the full-text index (title, summaries, tags, prompts,
provider, model, printer and source). Qualifiers become exact filters.

| Qualifier                     | Matches                                            |
| ----------------------------- | -------------------------------------------------- |
| ``tag:garage``                | designs tagged ``garage`` (repeat for AND)         |
| ``provider:ollama``           | designs generated with that provider (substring)   |
| ``model:qwen``                | designs generated with that model (substring)      |
| ``source:mcp``                | ``studio``, ``mcp`` or ``manual``                  |
| ``printer:x1c``               | printer profile of the current version (substring) |
| ``size:<100``                 | largest dimension, in mm                           |
| ``w:<50`` ``d:>20`` ``h:<=30`` | width (X), depth (Y), height (Z) in mm             |
| ``after:2026-10-01``          | created on or after that day                       |
| ``before:2026-11``            | created before that day (a month means its 1st)    |

Size values accept ``<``, ``<=``, ``>``, ``>=``, ``=`` and ranges such as ``20..80``.
A bare number (``size:100``) means "at most". Values with spaces can be quoted:
``tag:"wall mount"``. Text in double quotes is matched as an exact phrase.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

SOURCES = ("studio", "mcp", "manual")

# axis index into stats.size_mm, None for the largest dimension
_SIZE_KEYS: dict[str, int | None] = {
    "size": None,
    "w": 0, "x": 0, "width": 0,
    "d": 1, "y": 1, "depth": 1,
    "h": 2, "z": 2, "height": 2,
}
_KEYS = {"tag", "tags", "provider", "model", "source", "printer", "after", "before", *_SIZE_KEYS}

_TOKEN = re.compile(r'(?P<key>[A-Za-z]+):(?:"(?P<qval>[^"]*)"|(?P<val>\S+))|"(?P<phrase>[^"]*)"|(?P<word>\S+)')
_SIZE = re.compile(r"^(?P<op><=|>=|<|>|=)?\s*(?P<a>\d+(?:\.\d+)?)(?:\s*(?:\.\.|-)\s*(?P<b>\d+(?:\.\d+)?))?$")
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class SizeFilter:
    axis: int | None  # 0=X, 1=Y, 2=Z, None=largest dimension
    op: str  # one of < <= > >= =
    value: float


@dataclass
class Search:
    words: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    printers: list[str] = field(default_factory=list)
    sizes: list[SizeFilter] = field(default_factory=list)
    after: float | None = None
    before: float | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.fts_query())

    def fts_query(self) -> str:
        """An FTS5 MATCH expression: every word as a prefix, every phrase exact, all ANDed."""
        parts = [f'"{w}"*' for w in self.words]
        for p in self.phrases:
            toks = _WORD.findall(p)
            if toks:
                parts.append('"' + " ".join(toks) + '"')
        return " ".join(parts)


def normalize_tag(tag: str) -> str:
    """Tags are lowercase, single-spaced, without commas, up to 40 characters."""
    return re.sub(r"\s+", " ", str(tag).replace(",", " ")).strip().lower()[:40].strip()


def parse_size(value: str, axis: int | None = None) -> list[SizeFilter]:
    m = _SIZE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid size filter: {value!r}")
    a = float(m["a"])
    if m["b"] is not None:
        lo, hi = sorted((a, float(m["b"])))
        return [SizeFilter(axis, ">=", lo), SizeFilter(axis, "<=", hi)]
    return [SizeFilter(axis, m["op"] or "<=", a)]


def parse_date(value: str) -> float:
    """YYYY-MM-DD or YYYY-MM (the first of the month) in local time, as a Unix timestamp."""
    value = value.strip()
    try:
        fmt = "%Y-%m" if re.fullmatch(r"\d{4}-\d{2}", value) else "%Y-%m-%d"
        return _dt.datetime.strptime(value, fmt).timestamp()
    except ValueError as exc:
        raise ValueError(f"Invalid date: {value!r}, use YYYY-MM-DD") from exc


def parse(q: str) -> Search:
    """Parse a search box string. Unknown ``key:value`` pairs are treated as free text,
    and malformed qualifier values raise ValueError."""
    s = Search()
    for m in _TOKEN.finditer(q or ""):
        if m["key"] is not None:
            key, val = m["key"].lower(), m["qval"] if m["qval"] is not None else m["val"]
            if key not in _KEYS:
                s.words += [w.lower() for w in _WORD.findall(m.group(0))]
                continue
            add(s, key, val)
        elif m["phrase"] is not None:
            if m["phrase"].strip():
                s.phrases.append(m["phrase"].strip().lower())
        else:
            s.words += [w.lower() for w in _WORD.findall(m["word"])]
    return s


def add(s: Search, key: str, val: str) -> None:
    """Apply one qualifier to a Search. Shared by the parser and explicit API params."""
    val = (val or "").strip()
    if not val:
        return
    if key in ("tag", "tags"):
        s.tags += [t for t in (normalize_tag(x) for x in val.split(",")) if t and t not in s.tags]
    elif key == "provider":
        s.providers.append(val.lower())
    elif key == "model":
        s.models.append(val.lower())
    elif key == "source":
        if val.lower() not in SOURCES:
            raise ValueError(f"Unknown source {val!r}, use one of: {', '.join(SOURCES)}")
        s.sources.append(val.lower())
    elif key == "printer":
        s.printers.append(val.lower())
    elif key in _SIZE_KEYS:
        s.sizes += parse_size(val, _SIZE_KEYS[key])
    elif key == "after":
        s.after = parse_date(val)
    elif key == "before":
        s.before = parse_date(val)
    else:
        raise ValueError(f"Unknown filter {key!r}")


def filters(
    tags: str = "",
    provider: str = "",
    model: str = "",
    source: str = "",
    printer: str = "",
    min_size: float | None = None,
    max_size: float | None = None,
    after: str = "",
    before: str = "",
) -> Search:
    """Build a Search from explicit filter arguments (REST query params, MCP tool args).
    Sizes are the largest dimension in mm."""
    s = Search()
    for key, val in (("tags", tags), ("provider", provider), ("model", model), ("source", source),
                     ("printer", printer), ("after", after), ("before", before)):
        add(s, key, val)
    if min_size is not None:
        s.sizes.append(SizeFilter(None, ">=", float(min_size)))
    if max_size is not None:
        s.sizes.append(SizeFilter(None, "<=", float(max_size)))
    return s
