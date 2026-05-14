"""Localized regex patterns and prompt bundles.

This module is the SINGLE PLACE where non-English vocabulary lives in
the thread-keeper codebase. Other modules import named constants from
here and don't carry locale strings of their own.

Why centralize: thread-keeper is built CLI-agnostic and aims to be
language-agnostic too. The user may write in English, Russian, Spanish,
Mandarin, … — the agent has to recognize the same intents regardless.
By keeping all multilingual vocabulary in one named bundle:
  * The rest of the codebase stays readable English-only.
  * Adding a new language = appending a new section here; no edits to
    brief.py / extract.py / shadow_review.py.
  * Audits for accidental non-English literals only have to whitelist
    this file.

Supported locales (in order of speaker count):
  English, Mandarin Chinese, Hindi, Spanish, French, Arabic, Russian,
  Portuguese, German, Japanese.

Notes on regex boundaries across scripts:
  * Latin and Cyrillic words use ASCII `\\b` cleanly.
  * Devanagari (Hindi) and Arabic also work with `\\b` because the
    Python `re` module with `re.UNICODE` treats their letters as word
    chars.
  * Mandarin has no inter-character word boundary in regex terms, so
    its tokens are matched as literals with optional `(?<!\\w)` /
    `(?!\\w)` lookaround instead of `\\b`. The patterns below use bare
    literals — false positives are practically impossible because
    Han characters don't occur inside other-language vocabulary.
"""
from __future__ import annotations

import re


# ─────────────────────────────────────────────────────────────────────
# Parallel-work cues (brief.py SPAWN_CUE)
# Three families, each cross-language:
#   (a) explicit parallel vocabulary
#   (b) count + plural-noun  — count and noun may come from different
#       languages ("2 вопроса", "三 questions"). Kept as ONE combined
#       alternation so cross-mix matches.
#   (c) second-or-later numbered list item ("2.", "3)")
# ─────────────────────────────────────────────────────────────────────

# Parallel-vocabulary alternation, split by script. The Latin/Cyrillic/
# Devanagari/Arabic family is wrapped with \b in the final regex
# (whitespace-separated word boundaries work cleanly for them). Han
# (Mandarin) is matched as bare literals because \b never triggers
# between two CJK characters in Python's re engine.
_PARALLEL_WORDS_BOUNDED = (
    # English
    r"in\s+parallel|while\s+you|simultaneously|meanwhile|"
    r"in\s+the\s+background|fork\b"
    # Spanish
    r"|en\s+paralelo|simult[áa]neamente|mientras|al\s+mismo\s+tiempo|"
    r"en\s+segundo\s+plano"
    # Portuguese
    r"|em\s+paralelo|simultaneamente|enquanto|ao\s+mesmo\s+tempo|"
    r"em\s+segundo\s+plano"
    # French
    r"|en\s+parall[èe]le|simultan[ée]ment|pendant\s+que|"
    r"en\s+m[êe]me\s+temps|en\s+arri[èe]re-plan"
    # German
    r"|parallel|gleichzeitig|w[äa]hrenddessen|"
    r"zur\s+gleichen\s+zeit|im\s+hintergrund"
    # Russian
    r"|параллельн\w*|одновременн\w*|в\s+то\s+время\s+как|пока\s+ты|"
    r"заодно|в\s+фоне|многопоточн\w*"
    # Hindi (Devanagari)
    r"|समानांतर|एक\s+साथ|साथ\s+ही|पृष्ठभूमि\s+में"
    # Arabic
    r"|بالتوازي|في\s+نفس\s+الوقت|بالخلفية|متزامن\w*"
)
# CJK family: Mandarin + Japanese share no-whitespace word boundaries
# and need to be matched as bare literals (no \b).
_PARALLEL_WORDS_CJK = (
    # Mandarin
    r"并行|同时|与此同时|在后台|后台运行"
    # Japanese
    r"|並行で|並列で|同時に|バックグラウンドで|裏で"
)
_COUNT_WORDS = (
    r"[2-9]"  # digits cross-language
    r"|two|three|four|five|several|multiple"
    r"|dos|tres|cuatro|cinco|varios|varias|m[úu]ltiples"
    r"|dois|duas|tr[êe]s|quatro|cinco|v[áa]rios|v[áa]rias|m[úu]ltiplos"
    r"|deux|trois|quatre|cinq|plusieurs|multiples"
    r"|zwei|drei|vier|f[üu]nf|mehrere|mehrfach"
    r"|две|двух|три|трёх|трех|четыре|четырёх|пять"
    r"|दो|तीन|चार|पाँच|पांच|कई|कुछ"
    r"|اثنان|اثنين|ثلاث\w*|أربع\w*|خمس\w*|عدة"
    r"|两|三|四|五|几|多个|多项"
    r"|二つ|三つ|四つ|五つ|複数の?|いくつかの?"
)
_PLURAL_NOUNS = (
    # English
    r"things?|tasks?|questions?|items?|steps?|topics?|points?|"
    r"problems?|reasons?|options?"
    # Spanish
    r"|cosas|tareas|preguntas|pasos|temas|problemas|razones|opciones"
    # Portuguese
    r"|coisas|tarefas|perguntas|passos|etapas|t[óo]picos|pontos|"
    r"problemas|raz[õo]es|op[çc][õo]es"
    # French
    r"|choses|t[âa]ches|questions|[ée]tapes|sujets|probl[èe]mes|"
    r"raisons|options"
    # German
    r"|sachen|aufgaben|fragen|schritte|themen|punkte|"
    r"probleme|gr[üu]nde|optionen"
    # Russian
    r"|вопрос\w*|задач\w*|шаг\w*|пункт\w*|штук\w*|тем\w*|причин\w*|"
    r"варианта?|вариант\w*"
    # Hindi
    r"|काम|चीज़ें|सवाल|मुद्दे|कारण|विकल्प"
    # Arabic
    r"|أشياء|مهام|أسئلة|خطوات|نقاط|أسباب|خيارات"
    # Mandarin
    r"|件事|任务|问题|步骤|项目|方面|原因|选项"
    # Japanese
    r"|事|タスク|質問|ステップ|項目|問題|理由|選択肢"
)
SPAWN_CUE_RE = re.compile(
    rf"\b(?:{_PARALLEL_WORDS_BOUNDED})\b"
    rf"|(?:{_PARALLEL_WORDS_CJK})"  # CJK: bare, no \b
    rf"|\b(?:{_COUNT_WORDS})\s+(?:{_PLURAL_NOUNS})\b"
    rf"|(?:^|\n)\s*[2-9][\.\)\:]\s+",
    re.IGNORECASE | re.UNICODE,
)


# ─────────────────────────────────────────────────────────────────────
# Want / rule statements (extract.py WANT_RE)
# User stating a class-level want, never/always rule, normative phrasing.
# ─────────────────────────────────────────────────────────────────────

_WANT_EN = (
    r"\b(?:i\s+want\s+(?:you\s+)?to|i\s+need\s+(?:you\s+)?to|"
    r"you\s+(?:must|should|shouldn'?t|must\s+not)|"
    r"don'?t\s+(?:ever\s+)?|never\s+|always\s+|"
    r"from\s+now\s+on|going\s+forward)\b"
)
_WANT_ES = (
    r"\b(?:quiero\s+que|necesito\s+que|debes(?:\s+no)?|"
    r"no\s+debes|nunca\s+|siempre\s+|a\s+partir\s+de\s+ahora)\b"
)
_WANT_PT = (
    r"\b(?:eu\s+quero\s+que|quero\s+que|eu\s+preciso\s+que|"
    r"voc[êe]\s+(?:deve|n[ãa]o\s+deve|precisa)|"
    r"nunca\s+|sempre\s+|a\s+partir\s+de\s+agora|de\s+agora\s+em\s+diante)\b"
)
_WANT_FR = (
    r"\b(?:je\s+veux\s+que|j'?ai\s+besoin\s+(?:de|que)|"
    r"tu\s+dois(?:\s+pas)?|ne\s+pas\s+|jamais\s+|toujours\s+|"
    r"[àa]\s+partir\s+de\s+maintenant)\b"
)
_WANT_DE = (
    r"\b(?:ich\s+m[öo]chte,?\s+dass\s+du|ich\s+will,?\s+dass\s+du|"
    r"du\s+(?:sollst|darfst\s+nicht|musst)|nie(?:mals)?\s+|immer\s+|"
    r"ab\s+(?:jetzt|sofort)|von\s+jetzt\s+an)\b"
)
_WANT_RU = (
    r"\b(?:я\s+хочу\s+чтоб[ыь]?|хочу\s+чтоб[ыь]?|нужно\s+чтоб[ыь]?|"
    r"надо\s+чтоб[ыь]?|должен\s+быть|не\s+должен|должно\s+быть|"
    r"пусть\s+\S+\s+не|давай\s+чтоб[ыь]?|чтобы\s+ты\s+(?:не\s+)?)"
)
_WANT_HI = r"\b(?:मैं\s+चाहता\s+हूँ|मुझे\s+चाहिए|आपको\s+करना\s+चाहिए|मत\s+करो|हमेशा\s+|कभी\s+नहीं)"
_WANT_AR = r"\b(?:أريدك\s+أن|تحتاج\s+إلى|يجب\s+(?:أن|ألا)|لا\s+تفعل\s+أبد[اً]?|دائم[اً]?|من\s+الآن)"
# Mandarin and Japanese use literal CJK with no boundary anchor.
_WANT_ZH = r"(?:我想要|我需要你|你应该|你不应该|你必须|不要再|永远不要|总是|从现在开始)"
_WANT_JA = (
    r"(?:してほしい|してください|する必要がある|"
    r"しなければならない|してはいけない|絶対に|いつも|今後|これから)"
)

WANT_RE = re.compile(
    f"(?:{_WANT_EN})|(?:{_WANT_ES})|(?:{_WANT_PT})|(?:{_WANT_FR})|"
    f"(?:{_WANT_DE})|(?:{_WANT_RU})|(?:{_WANT_HI})|(?:{_WANT_AR})|"
    f"(?:{_WANT_ZH})|(?:{_WANT_JA})",
    re.IGNORECASE | re.UNICODE,
)


# ─────────────────────────────────────────────────────────────────────
# Conclusion / takeaway markers (extract.py INSIGHT_MARKERS_RE)
# ─────────────────────────────────────────────────────────────────────

_INSIGHT_EN = (
    r"\b(?:this\s+is\s+the|takeaway(?:\s|:)|"
    r"key\s+(?:point|insight)|conclusion(?:\s|:)|"
    r"the\s+bottom\s+line|the\s+gist\s+is)"
)
_INSIGHT_ES = (
    r"\b(?:la\s+conclusi[óo]n|el\s+punto\s+clave|en\s+resumen|"
    r"lo\s+importante\s+es|en\s+definitiva)"
)
_INSIGHT_PT = (
    r"\b(?:a\s+conclus[ãa]o|o\s+ponto\s+(?:chave|principal)|"
    r"em\s+resumo|o\s+importante\s+[ée]|no\s+fim\s+das\s+contas)"
)
_INSIGHT_FR = (
    r"\b(?:la\s+conclusion|le\s+point\s+cl[ée]|en\s+r[ée]sum[ée]|"
    r"l'?essentiel(?:\s+est)?|au\s+final)"
)
_INSIGHT_DE = (
    r"\b(?:die\s+schlussfolgerung|der\s+hauptpunkt|kurz\s+gesagt|"
    r"das\s+wichtige\s+ist|im\s+endeffekt|zusammengefasst)"
)
_INSIGHT_RU = (
    r"\b(?:это\s+и\s+есть|ключев(?:ое|ая|ой)|вывод(?:\s|:)|"
    r"главное\s+—|итог(?:\s|:)|суть\s+в\s+том)"
)
_INSIGHT_HI = r"(?:मुख्य\s+बात|निष्कर्ष|खास\s+बात|सार\s+यह)"
_INSIGHT_AR = r"(?:الخلاصة|النقطة\s+الأساسية|الأهم|باختصار)"
_INSIGHT_ZH = r"(?:关键是|结论是|重点是|总的来说|总结一下)"
_INSIGHT_JA = r"(?:結論は|要するに|重要なのは|ポイントは|要点は|まとめると)"

INSIGHT_MARKERS_RE = re.compile(
    f"(?:{_INSIGHT_EN})|(?:{_INSIGHT_ES})|(?:{_INSIGHT_PT})|"
    f"(?:{_INSIGHT_FR})|(?:{_INSIGHT_DE})|(?:{_INSIGHT_RU})|"
    f"(?:{_INSIGHT_HI})|(?:{_INSIGHT_AR})|"
    f"(?:{_INSIGHT_ZH})|(?:{_INSIGHT_JA})",
    re.IGNORECASE | re.UNICODE,
)


# ─────────────────────────────────────────────────────────────────────
# "For example" markers (extract.py EXAMPLE_RE)
# ─────────────────────────────────────────────────────────────────────

_EXAMPLE_EN = r"\b(?:for\s+example|e\.?g\.?|such\s+as|like\s+when)\b"
_EXAMPLE_ES = r"\bpor\s+ejemplo\b"
_EXAMPLE_PT = r"\bpor\s+exemplo\b"
_EXAMPLE_FR = r"\bpar\s+exemple\b"
_EXAMPLE_DE = r"\b(?:zum\s+beispiel|z\.\s?b\.?)\b"
_EXAMPLE_RU = r"\bнаприме[р]?\b"
_EXAMPLE_HI = r"(?:उदाहरण\s+के\s+लिए|जैसे\s+कि)"
_EXAMPLE_AR = r"(?:على\s+سبيل\s+المثال|مثل)"
_EXAMPLE_ZH = r"(?:例如|比如|举例来说)"
_EXAMPLE_JA = r"(?:例えば|たとえば|例として)"

EXAMPLE_RE = re.compile(
    f"(?:{_EXAMPLE_EN})|(?:{_EXAMPLE_ES})|(?:{_EXAMPLE_PT})|"
    f"(?:{_EXAMPLE_FR})|(?:{_EXAMPLE_DE})|(?:{_EXAMPLE_RU})|"
    f"(?:{_EXAMPLE_HI})|(?:{_EXAMPLE_AR})|"
    f"(?:{_EXAMPLE_ZH})|(?:{_EXAMPLE_JA})",
    re.IGNORECASE | re.UNICODE,
)


# ─────────────────────────────────────────────────────────────────────
# Pattern / regularity framing (extract.py FRAME_RE)
# ─────────────────────────────────────────────────────────────────────

_FRAME_EN = (
    r"\b(?:pattern(?:s|:)|regularly|typically|usually|"
    r"in\s+such\s+cases|whenever\s+\S+\s+then)"
)
_FRAME_ES = (
    r"\b(?:patr[óo]n|t[íi]picamente|normalmente|generalmente|"
    r"en\s+(?:estos|tales)\s+casos|cuando\s+\S+\s+entonces)"
)
_FRAME_PT = (
    r"\b(?:padr[ãa]o|tipicamente|normalmente|geralmente|"
    r"nesses\s+casos|sempre\s+que)"
)
_FRAME_FR = (
    r"\b(?:motif|typiquement|normalement|g[ée]n[ée]ralement|"
    r"dans\s+(?:ces|tels)\s+cas|chaque\s+fois\s+que)"
)
_FRAME_DE = (
    r"\b(?:muster|typischerweise|normalerweise|[üu]blicherweise|"
    r"in\s+solchen\s+f[äa]llen|immer\s+wenn)"
)
_FRAME_RU = (
    r"\b(?:паттерн|регулярн|обычно|типичн|часто\s+бывает|"
    r"в\s+таких\s+случаях|когда\s+\S+\s+—)"
)
_FRAME_HI = r"(?:पैटर्न|आमतौर\s+पर|ऐसे\s+मामलों\s+में|जब\s+भी)"
_FRAME_AR = r"(?:عادة|في\s+مثل\s+هذه\s+الحالات|كلما)"
_FRAME_ZH = r"(?:通常|一般来说|在这种情况下|每当)"
_FRAME_JA = r"(?:パターン|通常|一般的に|通例|このような場合|〜するたびに)"

FRAME_RE = re.compile(
    f"(?:{_FRAME_EN})|(?:{_FRAME_ES})|(?:{_FRAME_PT})|"
    f"(?:{_FRAME_FR})|(?:{_FRAME_DE})|(?:{_FRAME_RU})|"
    f"(?:{_FRAME_HI})|(?:{_FRAME_AR})|"
    f"(?:{_FRAME_ZH})|(?:{_FRAME_JA})",
    re.IGNORECASE | re.UNICODE,
)


# ─────────────────────────────────────────────────────────────────────
# Prompt-embedded examples (shadow_review.py / tools/spawn.py)
# Multilingual examples shown inline to spawned children so they
# recognize class-level signals regardless of the user's language.
# ─────────────────────────────────────────────────────────────────────

SHADOW_CLASS_SIGNAL_EXAMPLES = (
    '- "in this kind of task always X" (en) / '
    '"в таких задачах X" (ru) / '
    '"en este tipo de tarea siempre X" (es) / '
    '"neste tipo de tarefa sempre X" (pt) / '
    '"dans ce genre de tâche toujours X" (fr) / '
    '"bei dieser Art von Aufgabe immer X" (de) / '
    '"इस तरह के काम में हमेशा X" (hi) / '
    '"في هذا النوع من المهام دائماً X" (ar) / '
    '"在这种任务中总是 X" (zh) / '
    '"このような作業ではいつも X" (ja)\n'
    '- "stop doing Y" / "не делай Y" / "deja de hacer Y" / '
    '"pare de fazer Y" / "arrête de faire Y" / "hör auf, Y zu tun" / '
    '"Y करना बंद करो" / "توقف عن فعل Y" / '
    '"不要再做 Y" / "Y をするのをやめて"\n'
    '- "we got burned by Z last time" / "обожглись на Z" / '
    '"nos quemamos con Z" / "nos queimamos com Z" / '
    '"on s\'est brûlés sur Z" / "wir sind mit Z auf die Nase gefallen" / '
    '"पिछली बार Z से नुकसान हुआ" / "تضررنا من Z" / '
    '"上次被 Z 坑了" / "前回 Z で痛い目にあった"\n'
)

SPAWN_TRIGGER_PHRASE_EXAMPLES = (
    '"while you do X" / "in parallel" / "this is going to take a while" / '
    '"и ещё" / "параллельно" / "пока ты" / "заодно" / '
    '"en paralelo" / "mientras" / '
    '"em paralelo" / "enquanto" / '
    '"en parallèle" / "pendant que" / '
    '"parallel" / "gleichzeitig" / '
    '"एक साथ" / "बीच में" / '
    '"بالتوازي" / "في نفس الوقت" / '
    '"同时" / "并行" / '
    '"同時に" / "並行で"'
)


# ─────────────────────────────────────────────────────────────────────
# Supported-locales registry — handy for diagnostics and tests.
# ─────────────────────────────────────────────────────────────────────

SUPPORTED_LOCALES: tuple[str, ...] = (
    "en", "zh", "hi", "es", "pt", "fr", "de", "ar", "ru", "ja",
)
