"""Tests for asi_engine/researcher_agent.py — verify the researcher no
longer hardcodes GFS-boost / MeteoFrance-trim.
"""

import pytest

from asi_engine.cognition_base import CognitionBase
from asi_engine.researcher_agent import ResearcherAgent


@pytest.fixture
def researcher():
    cb = CognitionBase()
    return ResearcherAgent(cb)


class TestResearcherAgentHonesty:
    def test_does_not_hardcode_gfs_boost(self):
        """Run the researcher 30 times with no Brier data available.
        Every hypothesis must NOT specifically name GFS as the boost model
        in *every* round — that would indicate the hardcoded heuristic
        is still in place.
        """
        # Patch the Brier loader to return empty so we hit the random
        # fallback path. If the hardcoded GFS-boost heuristic were still
        # present, every run would say "Boost weight of 'gfs_seamless'".
        import asi_engine.researcher_agent as ra_mod

        original = ra_mod._load_model_brier_scores
        ra_mod._load_model_brier_scores = lambda: {}
        try:
            cb = CognitionBase()
            researcher = ResearcherAgent(cb)
            hypotheses = [researcher.propose_hypothesis(r)[0] for r in range(30)]
        finally:
            ra_mod._load_model_brier_scores = original

        # Count how many times each model is named as the boost target.
        from collections import Counter

        boost_counts = Counter()
        for h in hypotheses:
            # The hypothesis string contains "Boost weight of '<model>'"
            # only in the *old* hardcoded path; in the new path it says
            # "picked '<model>' to boost" or "Brier-driven shift: '<model>'".
            # We look for any of these patterns.
            import re

            m = re.search(
                r"(?:Boost weight of|picked to boost|Brier-driven shift:.*?'|picked ')([a-z_]+)",
                h,
            )
            if m:
                boost_counts[m.group(1)] += 1

        # In a random fallback, GFS should not be the boost target in
        # more than, say, 50% of runs (with 8 models, expectation is
        # ~12.5%). 50% is a very generous ceiling that still catches
        # the deterministic hardcoded version.
        gfs_count = boost_counts.get("gfs_seamless", 0)
        assert gfs_count < 15, (
            f"gfs_seamless was boosted {gfs_count}/30 times — the hardcoded "
            "GFS heuristic appears to still be present. boost_counts="
            f"{dict(boost_counts)}"
        )

    def test_brier_driven_path_uses_real_scores(self, monkeypatch):
        """When Brier data is available, the researcher must boost the
        lowest-Brier model and trim the highest-Brier model.
        """
        # Fake Brier scores: model_a is best, model_z is worst.
        fake_briers = {
            "gfs_seamless": 0.05,
            "ecmwf_ifs025": 0.10,
            "meteofrance_seamless": 0.40,  # worst
            "icon_global": 0.20,
        }

        import asi_engine.researcher_agent as ra_mod

        monkeypatch.setattr(ra_mod, "_load_model_brier_scores", lambda: fake_briers)

        cb = CognitionBase()
        researcher = ResearcherAgent(cb)
        hypothesis, params = researcher.propose_hypothesis(1)

        # Lowest-Brier = gfs_seamless → should be boosted.
        # Highest-Brier = meteofrance_seamless → should be trimmed.
        new_weights = params["model_weights"]
        old_weights = cb.get_best_parameters()["model_weights"]

        assert new_weights["gfs_seamless"] >= old_weights["gfs_seamless"], (
            f"gfs_seamless should be boosted: was {old_weights['gfs_seamless']}, "
            f"now {new_weights['gfs_seamless']}"
        )
        assert (
            new_weights["meteofrance_seamless"] <= old_weights["meteofrance_seamless"]
        ), (
            f"meteofrance_seamless should be trimmed: was {old_weights['meteofrance_seamless']}, "
            f"now {new_weights['meteofrance_seamless']}"
        )
        assert "Brier-driven" in hypothesis

    def test_weights_remain_normalized(self, researcher):
        """After any mutation, weights must still sum to ~1.0."""
        for r in range(10):
            _, params = researcher.propose_hypothesis(r)
            total = sum(params["model_weights"].values())
            assert abs(total - 1.0) < 0.01, f"Weights sum to {total}, expected ~1.0"

    def test_min_edge_stays_in_safe_range(self, researcher):
        for r in range(20):
            _, params = researcher.propose_hypothesis(r)
            assert 0.02 <= params["min_edge"] <= 0.15

    def test_kelly_fraction_stays_in_safe_range(self, researcher):
        for r in range(20):
            _, params = researcher.propose_hypothesis(r)
            assert 0.05 <= params["kelly_fraction"] <= 0.25
