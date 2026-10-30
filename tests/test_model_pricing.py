"""Official rate cards remain attributable, current and bounded during routing."""
from __future__ import annotations

from io import BytesIO
import gzip
import json
import stat

import pytest

NOW = 1_800_000_000
CARD = "# Pricing\nUSD per 1M tokens.\n## Rates\n| Model | Input | Output |\n| gpt-test | $2 | $10 |\n## Other\n"


def _setup(monkeypatch, tmp_path):
    from threadkeeper import model_pricing as pricing
    from threadkeeper.model_routes import options_from_catalog

    source = pricing.Source("Official API rates", ("codex",),
                            "https://developers.openai.com/api/docs/pricing.md",
                            (("## Rates", "## Other"),))
    monkeypatch.setattr(pricing, "SOURCES", (source,))
    options = options_from_catalog({"codex": {"models": ["gpt-test"]}})
    path = tmp_path / "model_pricing.json"
    return pricing, source, options, path


def test_fetch_cache_expiry_refreshes_actual_rate_and_keeps_source_time(monkeypatch, tmp_path):
    pricing, source, options, path = _setup(monkeypatch, tmp_path)
    documents = iter([CARD, CARD.replace("$10", "$12")])
    calls = []

    def download(url):
        calls.append(url)
        return next(documents)

    monkeypatch.setattr(pricing, "_download", download)
    first = pricing.pricing_context(options, NOW, path)
    assert "$10" in first and "status: fresh" in first
    assert source.url in first and "fetched_at: 2027-" in first
    assert pricing.pricing_context(options, NOW + 1, path) == first
    refreshed = pricing.pricing_context(options, NOW + pricing.CACHE_TTL_S, path)
    assert "$12" in refreshed and "$10" not in refreshed
    assert calls == [source.url, source.url]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", [OSError("offline"), ValueError("layout changed")])
def test_refresh_failure_preserves_last_good_card_as_stale(monkeypatch, tmp_path, failure):
    pricing, source, options, path = _setup(monkeypatch, tmp_path)
    cached = {source.url: {"fetched_at": NOW - pricing.CACHE_TTL_S, "text": "last good $10"}}
    path.write_text(json.dumps(cached))

    def fail(url):
        raise failure

    monkeypatch.setattr(pricing, "_download", fail)
    result = pricing.pricing_context(options, NOW, path)
    assert "status: stale; refresh failed" in result
    assert "last good $10" in result
    assert json.loads(path.read_text()) == cached


@pytest.mark.parametrize("cached", ["not JSON", "[]", '{"wrong":true}'])
def test_unavailable_rate_never_becomes_zero_or_overwrites_cache(monkeypatch, tmp_path, cached):
    pricing, _, options, path = _setup(monkeypatch, tmp_path)
    path.write_text(cached)

    def fail(url):
        raise OSError("offline")

    monkeypatch.setattr(pricing, "_download", fail)
    result = pricing.pricing_context(options, NOW, path)
    assert "status: unavailable; refresh failed; fetched_at: never" in result
    assert "Price data unknown." in result
    assert "$0" not in result
    assert path.read_text() == cached


def test_changed_document_layout_does_not_refresh_a_stale_timestamp(monkeypatch, tmp_path):
    pricing, source, options, path = _setup(monkeypatch, tmp_path)
    cached_at = NOW - pricing.CACHE_TTL_S
    path.write_text(json.dumps({source.url: {"fetched_at": cached_at, "text": "known card"}}))
    monkeypatch.setattr(pricing, "_download", lambda url: "# Login\n## Rates\nnot a rate card")
    assert "status: stale" in pricing.pricing_context(options, NOW, path)
    assert json.loads(path.read_text())[source.url]["fetched_at"] == cached_at


def test_future_or_wrongly_typed_cache_is_not_trusted(monkeypatch, tmp_path):
    pricing, source, options, path = _setup(monkeypatch, tmp_path)
    path.write_text(json.dumps({source.url: {"fetched_at": NOW + 1, "text": "wrong $0"}}))
    monkeypatch.setattr(pricing, "_download", lambda url: CARD)
    result = pricing.pricing_context(options, NOW, path)
    assert "wrong $0" not in result
    assert "$10" in result


def test_credit_html_table_preserves_rows_columns_and_units_without_footnote_digits():
    from threadkeeper.model_pricing import Source, _extract
    source = Source("credits", ("codex",), "url", (("## Rates", "## End"),))
    document = ('## Rates\nCredits per 1M tokens\n<table><tr><th>Model</th><th>Output</th></tr>'
                '<tr><td>GPT-test</td><td>0.2<sup>2</sup> credits</td></tr></table>\n## End')
    result = _extract(source, document)
    assert "Credits per 1M tokens" in result
    assert "| Model| Output" in result
    assert "| GPT-test| 0.2 credits" in result
    assert "0.22" not in result


def test_full_cards_use_spare_budget_before_any_trimming(monkeypatch, tmp_path):
    pricing, source, options, path = _setup(monkeypatch, tmp_path)
    sources = (source, pricing.Source("Second", ("codex",), "https://example.test/other", source.sections))
    monkeypatch.setattr(pricing, "SOURCES", sources)
    monkeypatch.setattr(pricing, "MAX_CONTEXT_CHARS", 2000)
    path.write_text(json.dumps({s.url: {"fetched_at": NOW, "text": "x" * (1200 if i == 0 else 50)}
                               for i, s in enumerate(sources)}))
    result = pricing.pricing_context(options, NOW, path)
    assert "excerpt truncated" not in result
    assert len(result) <= 2000


def test_large_sources_are_bounded_and_missing_terms_explicit(monkeypatch, tmp_path):
    pricing, source, options, path = _setup(monkeypatch, tmp_path)
    path.write_text(json.dumps({source.url: {"fetched_at": NOW, "text": "price row\n" * 1000}}))
    monkeypatch.setattr(pricing, "MAX_CONTEXT_CHARS", 900)
    result = pricing.pricing_context(options, NOW, path)
    assert len(result) <= 900
    assert "excerpt truncated; missing rates and terms are unknown" in result
    assert "source: " + source.url in result


def test_unconfigured_cli_has_unknown_prices_and_makes_no_request(monkeypatch, tmp_path):
    pricing, _, _, path = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pricing, "_download", lambda url: pytest.fail("unexpected request"))
    assert "prices unknown" in pricing.pricing_context({}, NOW, path)


def test_source_allowlist_and_redirect_policy_do_not_contact_other_hosts(monkeypatch):
    from threadkeeper import model_pricing as pricing
    with pytest.raises(ValueError, match="unapproved"):
        pricing._download("https://example.test/prices")
    assert pricing._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test") is None


@pytest.mark.parametrize("compressed", [False, True])
def test_oversized_plain_or_compressed_responses_are_rejected(monkeypatch, compressed):
    from threadkeeper import model_pricing as pricing
    monkeypatch.setattr(pricing, "MAX_BYTES", 100)
    data = b"x" * 101
    body = gzip.compress(data) if compressed else data

    class Opener:
        def open(self, req, timeout):
            return BytesIO(body)

    monkeypatch.setattr(pricing.request, "build_opener", lambda handler: Opener())
    with pytest.raises(ValueError, match="too large"):
        pricing._download(pricing.SOURCES[0].url)
