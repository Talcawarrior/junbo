"""Layer 2: ASI-Evolve daily candidate generation loop.

This is the "daily" layer in the 3-layer LLM stack:
  Karpathy weekly (broad exploration, slow) ->
  ASI-Evolve daily (UCB1-driven exploitation + experiment DB + FAISS cognition) ->
  SIA hourly (LLM-driven harness patches, fast weight tuning).

Ported from the design of GAIR-NLP/ASI-Evolve: a 3-agent loop with
Researcher / Engineer / Analyzer roles, an experiment DB (SQLite) that
records every candidate's parent + metrics, and a FAISS cognition store
that retrieves similar past experiments to inform new proposals.

Key features:

  - **UCB1 parent selection**: instead of always mutating the current best,
    pick the parent using Upper Confidence Bound 1 (Sutton & Barto §2.7):
        UCB1(n) = mean_roi(n) + c * sqrt(2 * ln(N) / n_visits(n)
    This balances exploitation (high-ROI parents) with exploration
    (under-visited parents), producing 50-200 candidates per run as the
    ASI-Evolve paper suggests.

  - **Experiment DB**: every candidate is persisted with its parent ID,
    hypothesis text, params, and OOS metrics. Future runs query this DB
    for both UCB1 stats and FAISS retrieval.

  - **FAISS cognition store**: each candidate's hypothesis text is embedded
    (sentence-transformers if installed, else hash-based pseudo-embedding)
    and indexed. When the Researcher proposes a new candidate, the top-K
    most similar past experiments are retrieved and fed as context.

  - **Real evaluation**: each candidate is evaluated using the same
    walk-forward OOS Brier dataset as Layer 1 (no synthetic noise).

  - **No LLM hard-dependency**: the Engineer agent uses the LLM if
    ``ZAI_API_KEY`` is set (via the shared ``asi_engine.llm_client``
    helper), otherwise falls back to the Layer 1 mutation ladder. CI
    runs without network.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from asi_engine.karpathy_weekly import (
    DEFAULT_MODELS,
    Hypothesis,
    _normalise,
    _uniform_weights,
    evaluate_hypothesis_oos,
    llm_propose_hypothesis,
)
from asi_engine.karpathy_weekly import (
    generate_hypothesis as karpathy_generate,
)
from data_pipeline.unified_datastore import UnifiedDatastore

logger = logging.getLogger("ASI_EVOLVE")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "data"))
EXP_DB_PATH = os.path.join(DATA_DIR, "asi_evolve_experiments.db")
BEST_PATH = os.path.join(DATA_DIR, "asi_evolve_best.json")
RESULTS_TSV_PATH = os.path.join(DATA_DIR, "asi_evolve_results.tsv")


# ---------------------------------------------------------------------------
# Experiment DB schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    round INTEGER NOT NULL,
    candidate_idx INTEGER NOT NULL,
    description TEXT NOT NULL,
    source TEXT NOT NULL,             -- 'mutation_ladder' or 'llm' or 'crossover'
    hypothesis_json TEXT NOT NULL,    -- full Hypothesis dict
    stats_json TEXT NOT NULL,         -- OOS stats dict
    sharpe REAL NOT NULL,
    roi_pct REAL NOT NULL,
    brier_score REAL NOT NULL,
    total_trades INTEGER NOT NULL,
    accepted INTEGER NOT NULL,        -- 1 if became new best
    created_at TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES experiments(id)
);
CREATE INDEX IF NOT EXISTS idx_exp_sharpe ON experiments(sharpe DESC);
CREATE INDEX IF NOT EXISTS idx_exp_parent ON experiments(parent_id);
CREATE INDEX IF NOT EXISTS idx_exp_round ON experiments(round);
"""


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(EXP_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(EXP_DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# FAISS cognition store (or fallback)
# ---------------------------------------------------------------------------


class CognitionStore:
    """Vector store of past experiment hypotheses for retrieval.

    Uses FAISS + sentence-transformers if installed; otherwise falls back to
    a hash-based pseudo-embedding that supports the same retrieve() API
    (with worse recall but no extra deps). The fallback is sufficient for CI.
    """

    def __init__(self, dim: int = 384):
        self.dim = dim
        self._ids: list[int] = []
        self._texts: list[str] = []
        self._embeddings: list[list[float]] = []
        self._faiss_index = None
        self._embedder = None
        self._init_backend()

    def _init_backend(self):
        try:
            import faiss  # type: ignore
            import sentence_transformers  # type: ignore

            self._embedder = sentence_transformers.SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            self._faiss_index = faiss.IndexFlatIP(self.dim)
            logger.info("CognitionStore: FAISS + sentence-transformers loaded")
        except ImportError:
            logger.info(
                "CognitionStore: faiss/sentence-transformers not installed — using hash-based fallback embeddings"
            )

    def _embed(self, text: str) -> list[float]:
        if self._embedder is not None:
            vec = self._embedder.encode([text], normalize_embeddings=True)[0]
            return vec.tolist()
        # Fallback: deterministic hash-based pseudo-embedding
        rng = random.Random(hash(text) & 0xFFFFFFFF)
        return [rng.gauss(0, 1) for _ in range(self.dim)]

    def add(self, exp_id: int, text: str) -> None:
        vec = self._embed(text)
        # Normalise hash-fallback to unit length for cosine similarity
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        self._ids.append(exp_id)
        self._texts.append(text)
        self._embeddings.append(vec)
        if self._faiss_index is not None:
            import numpy as np  # type: ignore

            self._faiss_index.add(np.array([vec], dtype="float32"))

    def retrieve(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        """Return top-K similar past experiments.

        Each result is {id, text, similarity}.
        """
        if not self._embeddings:
            return []
        q = self._embed(query)
        norm = math.sqrt(sum(v * v for v in q))
        if norm > 0:
            q = [v / norm for v in q]

        if self._faiss_index is not None:
            import numpy as np  # type: ignore

            d, idx_arr = self._faiss_index.search(np.array([q], dtype="float32"), min(k, len(self._ids)))
            results = []
            for sim, idx in zip(d[0].tolist(), idx_arr[0].tolist()):
                if idx < 0:
                    continue
                results.append(
                    {
                        "id": self._ids[idx],
                        "text": self._texts[idx],
                        "similarity": float(sim),
                    }
                )
            return results

        # Fallback: cosine similarity against all stored vectors
        sims = []
        for i, v in enumerate(self._embeddings):
            s = sum(a * b for a, b in zip(q, v))
            sims.append((s, i))
        sims.sort(reverse=True)
        return [{"id": self._ids[i], "text": self._texts[i], "similarity": float(s)} for s, i in sims[:k]]


# ---------------------------------------------------------------------------
# UCB1 parent selection
# ---------------------------------------------------------------------------


def ucb1_select_parent(
    conn: sqlite3.Connection,
    exploration_c: float = 1.41,
    min_visits: int = 1,
) -> int | None:
    """Pick the parent ID for the next candidate using UCB1.

    UCB1(n) = mean_roi(n) + c * sqrt(2 * ln(N_total) / n_visits(n))

    Returns None if there are no experiments yet (caller should use the
    uniform prior as parent).
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM experiments")
    n = cur.fetchone()[0]
    if n == 0:
        return None

    # For each distinct parent_id, compute mean ROI and visit count
    cur.execute(
        """
        SELECT parent_id, COUNT(*) as visits, AVG(roi_pct) as mean_roi
        FROM experiments
        WHERE parent_id IS NOT NULL
        GROUP BY parent_id
        """
    )
    rows = cur.fetchall()
    if not rows:
        return None

    ln_n = math.log(max(n, 1))
    best_score = -1e18
    best_parent: int | None = None
    for parent_id, visits, mean_roi in rows:
        if visits < min_visits:
            # Force exploration of under-visited parents
            return parent_id
        ucb = mean_roi + exploration_c * math.sqrt(ln_n / visits)
        if ucb > best_score:
            best_score = ucb
            best_parent = parent_id

    return best_parent


def get_parent_hypothesis(conn: sqlite3.Connection, parent_id: int) -> Hypothesis | None:
    """Load a parent hypothesis from the experiment DB."""
    cur = conn.cursor()
    cur.execute(
        "SELECT hypothesis_json FROM experiments WHERE id = ?",
        (parent_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
        clean = {k: v for k, v in data.items() if k in Hypothesis.__dataclass_fields__}
        return Hypothesis(**clean)
    except Exception as e:
        logger.warning("Could not load parent %d: %s", parent_id, e)
        return None


# ---------------------------------------------------------------------------
# 3 Agents: Researcher, Engineer, Analyzer
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A proposed hypothesis + its origin metadata."""

    hypothesis: Hypothesis
    parent_id: int | None
    source: str  # 'mutation_ladder' / 'llm' / 'crossover' / 'seed'
    round_num: int
    candidate_idx: int


class ResearcherAgent:
    """Selects the parent and decides *what kind* of mutation to propose."""

    def __init__(self, conn: sqlite3.Connection, cognition: CognitionStore):
        self.conn = conn
        self.cognition = cognition

    def select_parent(self) -> tuple[Hypothesis, int | None]:
        """Use UCB1 to pick a parent. Returns (parent_hyp, parent_id)."""
        parent_id = ucb1_select_parent(self.conn)
        if parent_id is None:
            # No experiments yet — use the Layer 1 best or uniform prior
            from asi_engine.karpathy_weekly import _load_best

            parent = _load_best() or Hypothesis(
                description="Uniform prior (no parent)",
                model_weights=_uniform_weights(),
                min_edge=0.05,
                kelly_fraction=0.15,
                max_bet_pct=0.05,
            )
            return parent, None
        parent = get_parent_hypothesis(self.conn, parent_id)
        if parent is None:
            # Parent ID exists but hypothesis couldn't be loaded — fall back
            from asi_engine.karpathy_weekly import _load_best

            parent = _load_best() or Hypothesis(
                description="Fallback",
                model_weights=_uniform_weights(),
                min_edge=0.05,
                kelly_fraction=0.15,
                max_bet_pct=0.05,
            )
            return parent, None
        return parent, parent_id

    def retrieve_context(self, query: str) -> list[dict[str, Any]]:
        """Retrieve top-K similar past experiments from cognition store."""
        return self.cognition.retrieve(query, k=3)


class EngineerAgent:
    """Generates the actual candidate hypothesis (the 'code patch').

    Uses the LLM if available; otherwise falls back to the Layer 1
    mutation ladder.
    """

    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm

    def propose(
        self,
        parent: Hypothesis,
        round_num: int,
        candidate_idx: int,
        context: list[dict[str, Any]] | None = None,
    ) -> Hypothesis:
        # Try LLM first if enabled
        if self.use_llm:
            ctx_payload = {
                "parent": parent.to_dict(),
                "similar_experiments": context or [],
            }
            hyp = llm_propose_hypothesis(parent, ctx_payload)
            if hyp is not None:
                return hyp

        # Fallback: deterministic mutation ladder from Layer 1
        return karpathy_generate(round_num + candidate_idx, parent=parent)


class AnalyzerAgent:
    """Evaluates a candidate and persists results to the experiment DB."""

    def __init__(self, conn: sqlite3.Connection, cognition: CognitionStore):
        self.conn = conn
        self.cognition = cognition

    def evaluate_and_store(
        self,
        candidate: Candidate,
        brier_df: pd.DataFrame,
        splits: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Evaluate candidate on each split's test window, mean the stats,
        persist to DB, and add to cognition store.
        """
        per_split = [evaluate_hypothesis_oos(brier_df, s["test_indices"], candidate.hypothesis) for s in splits]
        mean_stats = _mean_stats(per_split)

        # Persist to experiment DB
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO experiments (
                parent_id, round, candidate_idx, description, source,
                hypothesis_json, stats_json, sharpe, roi_pct, brier_score,
                total_trades, accepted, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.parent_id,
                candidate.round_num,
                candidate.candidate_idx,
                candidate.hypothesis.description,
                candidate.source,
                json.dumps(candidate.hypothesis.to_dict(), sort_keys=True),
                json.dumps(mean_stats, sort_keys=True),
                mean_stats["sharpe"],
                mean_stats["roi_pct"],
                mean_stats["brier_score"],
                mean_stats["total_trades"],
                0,  # accepted flag set later
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()
        exp_id = cur.lastrowid

        # Add to cognition store for future retrieval
        self.cognition.add(exp_id, candidate.hypothesis.description)

        mean_stats["exp_id"] = exp_id
        return mean_stats


# ---------------------------------------------------------------------------
# Crossover operator (for diversity)
# ---------------------------------------------------------------------------


def crossover(h1: Hypothesis, h2: Hypothesis) -> Hypothesis:
    """Uniform crossover between two hypotheses' weights.

    Takes each model's weight from h1 or h2 with 50% probability, then
    re-normalises. Other params are taken from the better parent (caller's
    responsibility to choose).
    """
    new_weights = {}
    for m in DEFAULT_MODELS:
        if random.random() < 0.5:
            new_weights[m] = h1.model_weights.get(m, 0.125)
        else:
            new_weights[m] = h2.model_weights.get(m, 0.125)
    new_weights = _normalise(new_weights)

    return Hypothesis(
        description=f"Crossover({h1.description[:30]} × {h2.description[:30]})",
        model_weights=new_weights,
        min_edge=(h1.min_edge + h2.min_edge) / 2,
        kelly_fraction=(h1.kelly_fraction + h2.kelly_fraction) / 2,
        max_bet_pct=max(h1.max_bet_pct, h2.max_bet_pct),
        tail_filter_enabled=h1.tail_filter_enabled or h2.tail_filter_enabled,
        tail_filter_threshold_high=h1.tail_filter_threshold_high,
        tail_filter_threshold_low=h1.tail_filter_threshold_low,
        tail_filter_correction_high=h1.tail_filter_correction_high,
        tail_filter_correction_low=h1.tail_filter_correction_low,
        source="crossover",
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _mean_stats(per_split: list[dict[str, float]]) -> dict[str, float]:
    """Mean across splits, equal-weighted."""
    if not per_split:
        return {
            "sharpe": 0.0,
            "roi_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "brier_score": 0.25,
            "total_pnl": 0.0,
            "total_staked": 0.0,
        }
    keys = ["sharpe", "roi_pct", "win_rate", "brier_score", "total_pnl", "total_staked"]
    out = {k: sum(s.get(k, 0.0) for s in per_split) / len(per_split) for k in keys}
    out["total_trades"] = int(sum(s.get("total_trades", 0) for s in per_split))
    out["sharpe"] = round(out["sharpe"], 4)
    out["roi_pct"] = round(out["roi_pct"], 4)
    out["brier_score"] = round(out["brier_score"], 4)
    out["win_rate"] = round(out["win_rate"], 4)
    return out


def _load_best(
    conn: sqlite3.Connection,
) -> tuple[Hypothesis | None, dict[str, float] | None]:
    """Load the current best hypothesis from the experiment DB."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, hypothesis_json, stats_json FROM experiments WHERE accepted = 1 ORDER BY sharpe DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return None, None
    try:
        data = json.loads(row[1])
        clean = {k: v for k, v in data.items() if k in Hypothesis.__dataclass_fields__}
        hyp = Hypothesis(**clean)
        stats = json.loads(row[2])
        return hyp, stats
    except Exception as e:
        logger.warning("Could not load best: %s", e)
        return None, None


def _save_best_metadata(hyp: Hypothesis, stats: dict[str, float]) -> None:
    """Persist best hypothesis to JSON for cross-layer visibility."""
    os.makedirs(os.path.dirname(BEST_PATH), exist_ok=True)
    payload = {
        **hyp.to_dict(),
        "stats": stats,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    with open(BEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _append_results_tsv(
    round_num: int,
    candidate_idx: int,
    hyp: Hypothesis,
    stats: dict[str, float],
    status: str,
) -> None:
    os.makedirs(os.path.dirname(RESULTS_TSV_PATH), exist_ok=True)
    header = (
        "round\tcandidate\ttimestamp\tdescription\tsource\tmin_edge\tkelly\tsharpe\troi_pct\tbrier\ttrades\tstatus\n"
    )
    if not os.path.exists(RESULTS_TSV_PATH):
        with open(RESULTS_TSV_PATH, "w", encoding="utf-8") as f:
            f.write(header)
    with open(RESULTS_TSV_PATH, "a", encoding="utf-8") as f:
        f.write(
            f"{round_num}\t{candidate_idx}\t{datetime.now(UTC).isoformat()}\t"
            f"{hyp.description!r}\t{hyp.source}\t{hyp.min_edge}\t{hyp.kelly_fraction}\t"
            f"{stats.get('sharpe', 0.0)}\t{stats.get('roi_pct', 0.0)}\t"
            f"{stats.get('brier_score', 0.25)}\t{stats.get('total_trades', 0)}\t{status}\n"
        )


def run_asi_evolve_daily(
    n_candidates: int = 20,
    use_llm: bool = False,
    crossover_rate: float = 0.2,
    seed: int = 42,
) -> dict[str, Any]:
    """Run one ASI-Evolve daily iteration.

    Args:
        n_candidates: number of candidates to generate + evaluate (paper
            suggests 50-200; we default to 20 for CI speed).
        use_llm: if True, Engineer uses LLM proposals when available.
        crossover_rate: fraction of candidates produced via crossover
            rather than mutation.
        seed: RNG seed.

    Returns a summary dict.
    """
    random.seed(seed)
    conn = _get_db()
    cognition = CognitionStore()

    # Pre-populate cognition store with all past experiments
    cur = conn.cursor()
    cur.execute("SELECT id, description FROM experiments ORDER BY id")
    for exp_id, desc in cur.fetchall():
        cognition.add(exp_id, desc)

    # 1. Pull unified Brier dataset + splits
    ds = UnifiedDatastore()
    try:
        brier_df = ds.build_brier_dataset()
    except Exception as e:
        logger.warning(
            "build_brier_dataset() raised %s — run polymarket_ingest with the "
            "weather market parser first. Returning early.",
            e,
        )
        return {
            "error": "brier_dataset_unavailable",
            "detail": str(e),
            "candidates_run": 0,
        }

    if brier_df is None or brier_df.empty:
        logger.error("Brier dataset is empty")
        return {"error": "empty_brier_dataset", "candidates_run": 0}

    # Add per-model prob columns (tries real forecast join, falls back to synthetic)
    from asi_engine.karpathy_weekly import add_per_model_probabilities

    brier_df = add_per_model_probabilities(brier_df, ds=ds, seed=seed)

    splits = ds.build_walk_forward_splits()
    if not splits:
        splits = [
            {
                "split_n": 1,
                "test_indices": brier_df.index.tolist(),
                "train_indices": [],
            }
        ]

    # 2. Init agents
    researcher = ResearcherAgent(conn, cognition)
    engineer = EngineerAgent(use_llm=use_llm)
    analyzer = AnalyzerAgent(conn, cognition)

    # 3. Load current best
    best_hyp, best_stats = _load_best(conn)
    if best_hyp is None:
        # Seed with the Karpathy best (if any) or uniform prior
        from asi_engine.karpathy_weekly import _load_best as load_karpathy_best

        best_hyp = load_karpathy_best() or Hypothesis(
            description="Uniform prior (seed)",
            model_weights=_uniform_weights(),
            min_edge=0.05,
            kelly_fraction=0.15,
            max_bet_pct=0.05,
        )
        best_stats = {
            "sharpe": -1e9,
            "roi_pct": -1e9,
            "brier_score": 1.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "total_pnl": 0.0,
            "total_staked": 0.0,
        }
        # Seed the DB so future UCB1 has a parent to point to
        seed_candidate = Candidate(
            hypothesis=best_hyp,
            parent_id=None,
            source="seed",
            round_num=0,
            candidate_idx=0,
        )
        best_stats = analyzer.evaluate_and_store(seed_candidate, brier_df, splits)
        # Mark as accepted
        cur.execute(
            "UPDATE experiments SET accepted = 1 WHERE id = ?",
            (best_stats["exp_id"],),
        )
        conn.commit()
        _save_best_metadata(best_hyp, best_stats)

    # 4. Generate + evaluate candidates
    round_num = 1 + (cur.execute("SELECT COALESCE(MAX(round), 0) FROM experiments").fetchone()[0])
    accepted_count = 0

    for c in range(n_candidates):
        # Pick parent via UCB1
        parent_hyp, parent_id = researcher.select_parent()

        # Decide: crossover or mutation?
        if random.random() < crossover_rate and len(cognition._ids) >= 2:
            # Pick a second parent for crossover
            second_id = random.choice([i for i in cognition._ids if i != parent_id])
            second_hyp = get_parent_hypothesis(conn, second_id) or parent_hyp
            new_hyp = crossover(parent_hyp, second_hyp)
            source = "crossover"
        else:
            # Mutation
            context = researcher.retrieve_context(parent_hyp.description)
            new_hyp = engineer.propose(parent_hyp, round_num=round_num, candidate_idx=c, context=context)
            source = new_hyp.source

        candidate = Candidate(
            hypothesis=new_hyp,
            parent_id=parent_id,
            source=source,
            round_num=round_num,
            candidate_idx=c,
        )

        try:
            stats = analyzer.evaluate_and_store(candidate, brier_df, splits)
        except Exception as e:
            logger.exception("Candidate %d evaluation failed: %s", c, e)
            continue

        improved = (
            stats["sharpe"] > best_stats.get("sharpe", -1e9)
            and stats["brier_score"] <= best_stats.get("brier_score", 1.0) * 1.10
            and stats["total_trades"] >= 5
        )

        if improved:
            logger.info(
                "  [%d/%d] ✓ ACCEPTED (sharpe %.3f > %.3f): %s",
                c + 1,
                n_candidates,
                stats["sharpe"],
                best_stats.get("sharpe", 0.0),
                new_hyp.description,
            )
            best_hyp = new_hyp
            best_stats = stats
            cur.execute(
                "UPDATE experiments SET accepted = 1 WHERE id = ?",
                (stats["exp_id"],),
            )
            conn.commit()
            _save_best_metadata(new_hyp, stats)
            _append_results_tsv(round_num, c, new_hyp, stats, "keep")
            accepted_count += 1
        else:
            logger.info(
                "  [%d/%d] ✗ reject (sharpe %.3f ≤ %.3f)",
                c + 1,
                n_candidates,
                stats["sharpe"],
                best_stats.get("sharpe", 0.0),
            )
            _append_results_tsv(round_num, c, new_hyp, stats, "reject")

        # LLM rate-limit friendliness
        if use_llm and (c + 1) % 5 == 0:
            time.sleep(1.0)

    cur.execute("SELECT COUNT(*) FROM experiments")
    total_experiments = cur.fetchone()[0]
    conn.close()

    logger.info(
        "ASI-Evolve round %d done. Candidates=%d, Accepted=%d, Total DB size=%d",
        round_num,
        n_candidates,
        accepted_count,
        total_experiments,
    )

    return {
        "round": round_num,
        "candidates_run": n_candidates,
        "accepted": accepted_count,
        "total_experiments": total_experiments,
        "best_hypothesis": best_hyp.to_dict(),
        "best_stats": best_stats,
        "n_splits": len(splits),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="ASI-Evolve daily candidate generation loop")
    parser.add_argument("--candidates", type=int, default=20, help="Number of candidates per run")
    parser.add_argument("--llm", action="store_true", help="Use LLM for hypothesis generation")
    parser.add_argument("--crossover", type=float, default=0.2, help="Crossover probability")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    summary = run_asi_evolve_daily(
        n_candidates=args.candidates,
        use_llm=args.llm,
        crossover_rate=args.crossover,
        seed=args.seed,
    )
    print(json.dumps(summary, indent=2, default=str))
