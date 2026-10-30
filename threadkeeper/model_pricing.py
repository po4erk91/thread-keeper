"""Bounded official rate-card evidence for the weekly model router."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
from html.parser import HTMLParser
from http.client import HTTPException
from io import BytesIO
import json
import logging
from pathlib import Path
import re
import time
from urllib import request

from .config_io import atomic_write_json
from .model_routes import CliOptions

logger = logging.getLogger(__name__)
CACHE_TTL_S = 24 * 3600
MAX_BYTES = 1_000_000
MAX_CONTEXT_CHARS = 32_000
REQUEST_TIMEOUT_S = 8


@dataclass(frozen=True)
class Source:
    name: str
    clis: tuple[str, ...]
    url: str
    sections: tuple[tuple[str, str], ...]


SOURCES = (
    Source("OpenAI API rates (USD per 1M tokens; API reference)", ("codex",),
           "https://developers.openai.com/api/docs/pricing.md",
           (("### Standard pricing data", "### Batch pricing data"),
            ("### Fast pricing data", "### Ultrafast pricing data"))),
    Source("Codex credit rates", ("codex",),
           "https://learn.chatgpt.com/docs/pricing.md",
           (("#### Token rates", "### What counts as Code Review usage?"),)),
    Source("Anthropic API rates (USD per 1M tokens; API reference)", ("claude",),
           "https://platform.claude.com/docs/en/about-claude/pricing.md",
           (("## Model pricing", "## Cloud platform pricing"),
            ("### Fast mode pricing", "### Batch processing"),
            ("### Long context pricing", "### Tool use pricing"))),
    Source("Claude alias resolution", ("claude",),
           "https://code.claude.com/docs/en/model-config.md",
           (("The `opus`, `sonnet`, and `haiku` aliases resolve", "### Work with Fable"),)),
    Source("GitHub Copilot rates", ("copilot",),
           "https://docs.github.com/api/article/body?pathname=/en/copilot/reference/copilot-billing/models-and-pricing",
           (("## How model pricing works", "## Code completions"),)),
    Source("Antigravity quota and overages", ("antigravity",),
           "https://antigravity.google/docs/plans.md",
           (("# Plans", "## Other"),)),
)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(url: str) -> str:
    """Fetch only our fixed HTTPS sources; never follow an external redirect."""
    if url not in {source.url for source in SOURCES}:
        raise ValueError("unapproved pricing source")
    req = request.Request(url, headers={
        "User-Agent": "threadkeeper-model-router",
        "Accept": "text/markdown,text/plain", "Accept-Encoding": "identity",
    })
    with request.build_opener(_NoRedirect()).open(req, timeout=REQUEST_TIMEOUT_S) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("pricing response too large")
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=BytesIO(data)) as compressed:
            data = compressed.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("decompressed pricing response too large")
    return data.decode("utf-8")


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.parts.append("\n")
        if tag in {"th", "td"}:
            self.parts.append("| ")

    def handle_data(self, data):
        self.parts.append(re.sub(r"\s+", " ", data))

    def handle_endtag(self, tag):
        if tag == "tr":
            self.parts.append(" |\n")


def _table_text(match: re.Match) -> str:
    parser = _TableText()
    parser.feed(match.group())
    return "".join(parser.parts)


def _extract(source: Source, document: str) -> str:
    if re.search(r"<!doctype html|<html\b", document, re.I):
        raise ValueError("expected a Markdown rate card")
    sections = []
    for start, end in source.sections:
        first = document.find(start)
        last = document.find(end, first + len(start)) if first >= 0 else -1
        if first < 0 or last < 0:
            raise ValueError("official rate-card layout changed")
        sections.append(document[first:last].strip())
    # OpenAI's context threshold and promotional terms follow its speed tables.
    qualifiers = [line for line in document.splitlines()
                  if "Short context:" in line or "promotional pricing" in line.lower()]
    text = "\n\n".join(sections + qualifiers)
    text = re.sub(r"<sup\b[^>]*>.*?</sup>", "", text, flags=re.S | re.I)
    text = re.sub(r"<table\b.*?</table>", _table_text, text, flags=re.S | re.I)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_cache(path: Path) -> dict:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        return cached if isinstance(cached, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid_entry(entry: object, now: int) -> bool:
    if not isinstance(entry, dict):
        return False
    fetched_at = entry.get("fetched_at")
    text = entry.get("text")
    return (type(fetched_at) is int and 0 < fetched_at <= now
            and isinstance(text, str) and 0 < len(text) <= MAX_BYTES)


def _refresh(source: Source, cached: object, now: int) -> tuple[dict, str]:
    entry = cached if _valid_entry(cached, now) else {}
    if entry and now - entry["fetched_at"] < CACHE_TTL_S:
        return entry, "fresh"
    try:
        text = _extract(source, _download(source.url))
        return {"fetched_at": now, "text": text}, "fresh"
    except (OSError, ValueError, HTTPException) as exc:
        logger.info("model pricing refresh failed: %s (%s)", source.url, type(exc).__name__)
        return entry, "stale; refresh failed" if entry else "unavailable; refresh failed"


def _excerpt(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    marker = "\n[excerpt truncated; missing rates and terms are unknown]"
    return text[:budget - len(marker)].rsplit("\n", 1)[0] + marker


def _render(source: Source, entry: dict, status: str, budget: int) -> str:
    fetched = entry.get("fetched_at")
    stamp = datetime.fromtimestamp(fetched, timezone.utc).isoformat() if fetched else "never"
    header = f"### {source.name}\nsource: {source.url}\nstatus: {status}; fetched_at: {stamp}\n"
    return header + _excerpt(entry.get("text", "Price data unknown."), budget - len(header))


def pricing_context(options: dict[str, CliOptions], now: int | None = None,
                    cache_path: Path | None = None) -> str:
    """Refresh official evidence once per day; retain last good cards on failure."""
    from . import config

    now = int(time.time()) if now is None else now
    path = cache_path if cache_path is not None else Path(config.DB_PATH).parent / "model_pricing.json"
    sources = [source for source in SOURCES
               if any(cli in options and options[cli].models for cli in source.clis)]
    if not sources:
        return "## OFFICIAL PRICING\nNo official source configured for these CLIs; prices unknown."
    cache = _read_cache(path)
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        results = list(pool.map(lambda source: _refresh(source, cache.get(source.url), now), sources))
    updates = {source.url: entry for source, (entry, _) in zip(sources, results) if entry}
    cache.update(updates)
    if updates:
        try:
            atomic_write_json(path, cache)
        except OSError:
            pass  # The current pass still has the fetched evidence.
    header = "## OFFICIAL PRICING (published evidence; separate currencies and billing scopes)\n"
    blocks = [_render(source, entry, status, len(entry.get("text", "")) + 600)
              for source, (entry, status) in zip(sources, results)]
    complete = header + "\n\n".join(blocks)
    if len(complete) <= MAX_CONTEXT_CHARS:
        return complete
    budget = (MAX_CONTEXT_CHARS - len(header) - 2 * len(sources)) // len(sources)
    return header + "\n\n".join(_render(source, entry, status, budget)
                               for source, (entry, status) in zip(sources, results))
