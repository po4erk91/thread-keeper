"""Offline eval harness for learning-loop decision quality (issue #72).

Where ``threadkeeper/verify_ingest.py`` (issue #1) and the memory-recall
harness (issue #71) measure *plumbing* and *retrieval*, this package measures
**decision quality**: when the shadow-review / candidate-reviewer daemons make
a materialize/skip or accept/reject call, are those calls *right*?

It does so over a small, hand-labeled, **anonymized** fixture set checked into
``threadkeeper/eval/fixtures/`` and reports precision / recall / F1 for the two
binary decision loops plus a calibrated judge-vs-human agreement number for the
open-ended "is this a high-quality skill?" question. See
``harness.py`` for the design and :mod:`threadkeeper.eval.__main__` for the CLI
(``python -m threadkeeper.eval``).
"""
from .harness import (  # noqa: F401
    run_eval,
    format_report,
    main,
    binary_metrics,
    cohen_kappa,
    agreement,
    rubric_predict,
    quality_heuristic,
)
