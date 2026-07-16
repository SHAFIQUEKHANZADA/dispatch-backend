"""The 3D Dispatch decision engine.

Pure, deterministic, LLM-free.  Everything in this package can be unit tested
without a database, a network, or a model.

    match_score.py  the Match Score (RULE 1: deterministic algorithm, not an LLM)
    optimizer.py    "Make Smart Decision" — the shop-wide plan
    metrics.py      the six scoreboard formulas
    importer.py     DMS CSV parsing, validation, familiarity + comeback derivation
"""

from .types import ENGINE_VERSION  # noqa: F401
