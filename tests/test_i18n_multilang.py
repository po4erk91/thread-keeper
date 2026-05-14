"""Multilingual coverage for threadkeeper.i18n regex bundles.

For each supported locale (en, zh, hi, es, fr, ar, ru) we verify that
the five pattern families fire on a representative sample phrase. This
guards against regressions when adding/refactoring locales: if any
language drops out of any family, the relevant test fails loudly with
the locale tag.
"""
from __future__ import annotations

import pytest

from threadkeeper import i18n


# ─────────────────────────────────────────────────────────────────────
# Samples grouped by family. Each entry: (locale, phrase)
# ─────────────────────────────────────────────────────────────────────

SPAWN_CUE_SAMPLES = [
    ("en", "do these in parallel"),
    ("es", "hazlo en paralelo"),
    ("pt", "faça isso em paralelo"),
    ("fr", "fais-le en parallèle"),
    ("de", "mach das parallel"),
    ("ru", "сделай параллельно"),
    ("hi", "समानांतर में करो"),
    ("ar", "افعل ذلك بالتوازي"),
    ("zh", "同时做这个"),
    ("ja", "並行で進めて"),
    # Cross-language count + plural noun
    ("en-count", "3 tasks pending"),
    ("es-count", "tres tareas pendientes"),
    ("pt-count", "três tarefas pendentes"),
    ("de-count", "drei Aufgaben offen"),
    ("ru-count", "две задачи"),
    ("zh-count", "三 questions to answer"),
    ("ja-count", "三つ タスク を実行"),
]

WANT_SAMPLES = [
    ("en", "I want you to never use X"),
    ("es", "quiero que nunca uses X"),
    ("pt", "eu quero que você nunca use X"),
    ("fr", "je veux que tu ne fasses jamais X"),
    ("de", "ich möchte, dass du nie X verwendest"),
    ("ru", "я хочу чтобы ты не использовал X"),
    ("hi", "मैं चाहता हूँ कि X मत करो"),
    ("ar", "أريدك أن لا تفعل X"),
    ("zh", "我想要你不要再用 X"),
    ("ja", "X をしてほしい"),
]

INSIGHT_SAMPLES = [
    ("en", "the key point is X"),
    ("es", "la conclusión es X"),
    ("pt", "a conclusão é X"),
    ("fr", "la conclusion: X"),
    ("de", "die Schlussfolgerung ist X"),
    ("ru", "вывод: X"),
    ("hi", "मुख्य बात X है"),
    ("ar", "الخلاصة X"),
    ("zh", "关键是 X"),
    ("ja", "結論は X"),
]

EXAMPLE_SAMPLES = [
    ("en", "for example X"),
    ("es", "por ejemplo X"),
    ("pt", "por exemplo X"),
    ("fr", "par exemple X"),
    ("de", "zum Beispiel X"),
    ("de-short", "z.B. X funktioniert"),
    ("ru", "например X"),
    ("hi", "उदाहरण के लिए X"),
    ("ar", "على سبيل المثال X"),
    ("zh", "例如 X"),
    ("ja", "例えば X"),
]

FRAME_SAMPLES = [
    ("en", "this typically happens"),
    ("es", "típicamente sucede"),
    ("pt", "tipicamente acontece"),
    ("fr", "typiquement cela arrive"),
    ("de", "typischerweise passiert das"),
    ("ru", "обычно так"),
    ("hi", "आमतौर पर ऐसा होता है"),
    ("ar", "عادة يحدث هذا"),
    ("zh", "通常会发生"),
    ("ja", "通常そうなる"),
]


# ─────────────────────────────────────────────────────────────────────
# Parametrized tests — one per family
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("locale,phrase", SPAWN_CUE_SAMPLES,
                         ids=[s[0] for s in SPAWN_CUE_SAMPLES])
def test_spawn_cue_matches(locale, phrase):
    assert i18n.SPAWN_CUE_RE.search(phrase) is not None, (
        f"SPAWN_CUE_RE failed on {locale!r}: {phrase!r}"
    )


@pytest.mark.parametrize("locale,phrase", WANT_SAMPLES,
                         ids=[s[0] for s in WANT_SAMPLES])
def test_want_matches(locale, phrase):
    assert i18n.WANT_RE.search(phrase) is not None, (
        f"WANT_RE failed on {locale!r}: {phrase!r}"
    )


@pytest.mark.parametrize("locale,phrase", INSIGHT_SAMPLES,
                         ids=[s[0] for s in INSIGHT_SAMPLES])
def test_insight_matches(locale, phrase):
    assert i18n.INSIGHT_MARKERS_RE.search(phrase) is not None, (
        f"INSIGHT_MARKERS_RE failed on {locale!r}: {phrase!r}"
    )


@pytest.mark.parametrize("locale,phrase", EXAMPLE_SAMPLES,
                         ids=[s[0] for s in EXAMPLE_SAMPLES])
def test_example_matches(locale, phrase):
    assert i18n.EXAMPLE_RE.search(phrase) is not None, (
        f"EXAMPLE_RE failed on {locale!r}: {phrase!r}"
    )


@pytest.mark.parametrize("locale,phrase", FRAME_SAMPLES,
                         ids=[s[0] for s in FRAME_SAMPLES])
def test_frame_matches(locale, phrase):
    assert i18n.FRAME_RE.search(phrase) is not None, (
        f"FRAME_RE failed on {locale!r}: {phrase!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Negative checks — random plain-English text shouldn't trip the
# non-English branches; sanity guards against overly-greedy patterns.
# ─────────────────────────────────────────────────────────────────────

def test_negatives_no_false_positives_on_neutral_english():
    # A neutral sentence with NO class-level signals should match
    # nothing across all five families.
    neutral = "Let's read this file and see what's in it."
    assert i18n.WANT_RE.search(neutral) is None
    assert i18n.INSIGHT_MARKERS_RE.search(neutral) is None
    assert i18n.EXAMPLE_RE.search(neutral) is None
    assert i18n.FRAME_RE.search(neutral) is None
    assert i18n.SPAWN_CUE_RE.search(neutral) is None


def test_supported_locales_listed():
    assert set(i18n.SUPPORTED_LOCALES) == {
        "en", "zh", "hi", "es", "pt", "fr", "de", "ar", "ru", "ja",
    }
