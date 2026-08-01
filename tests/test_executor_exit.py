"""BetPlacer.exit_position must execute a real Polymarket SELL in live mode
and stay paper-safe in DRY_RUN mode (the 10-day paper phase).

Regression context: early-exit previously only booked PnL in the DB and never
actually sold the position on Polymarket, so booked "profits" were never
realized. exit_position closes that gap and must remain a no-op against the
exchange while DRY_RUN is true.
"""

from types import SimpleNamespace


def test_exit_position_paper_safe_returns_paper_order(monkeypatch):
    monkeypatch.setattr("config.settings.Config.DRY_RUN", True)
    from executor.bet_placer import BetPlacer

    placer = BetPlacer()
    market = SimpleNamespace(id="m_test_1")
    res = placer.exit_position(market, "NO", 0.62, 100.0, "take_profit")
    assert res is not None
    assert res.get("paper") is True
    assert "orderID" in res
    assert str(res["orderID"]).startswith("paper_sell_")


def test_exit_position_live_mode_calls_client(monkeypatch):
    """In live mode the sell must hit the Polymarket client, not just book DB."""
    monkeypatch.setattr("config.settings.Config.DRY_RUN", False)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    calls = {}

    class FakeClient:
        def create_and_post_order(self, payload):
            calls["payload"] = payload
            return {"orderID": "live_123"}

    from executor.bet_placer import BetPlacer
    from py_clob_client.order_builder.constants import SELL

    # Bypass __init__ (which would try to build a real CLOB client) and inject
    # a fake ready client so we isolate the exit_position live path.
    p = BetPlacer.__new__(BetPlacer)
    p.ready = True
    p.client = FakeClient()
    p._get_token_id = lambda m, s: "tok_yes"
    market = SimpleNamespace(id="m_live_1")

    res = p.exit_position(market, "YES", 0.40, 50.0, "stop_loss")
    assert res is not None
    assert res.get("orderID") == "live_123"
    assert calls["payload"]["side"] == SELL
    assert calls["payload"]["size"] == 50.0
    assert calls["payload"]["price"] == 0.40
