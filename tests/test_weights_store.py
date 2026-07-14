"""SIA weight persistence: load on init, save on optimize, no-spam threshold."""

import os


def test_load_returns_none_when_file_missing(tmp_path, monkeypatch):
    """A missing file must not raise -- callers fall back to in-memory defaults."""
    # Point _WEIGHTS_PATH at a non-existent file in tmp.
    import utils.weights_store as ws

    monkeypatch.setattr(ws, "_WEIGHTS_PATH", str(tmp_path / "absent.json"))
    assert ws.load_weights() is None


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    """save_weights must produce a file that load_weights can read back."""
    import utils.weights_store as ws

    path = str(tmp_path / "model_weights.json")
    monkeypatch.setattr(ws, "_WEIGHTS_PATH", path)

    # Use real model names so ALL_ENSEMBLE_MODELS doesn't inject floors
    weights = {"gfs_seamless": 0.5, "ecmwf_ifs025": 0.3, "gem_global": 0.2}
    assert ws.save_weights(weights, apply_floor=False, min_change=0) is True
    assert os.path.exists(path)
    loaded = ws.load_weights()
    assert loaded is not None
    assert abs(loaded["gfs_seamless"] - 0.5) < 1e-9
    assert abs(loaded["ecmwf_ifs025"] - 0.3) < 1e-9
    assert abs(loaded["gem_global"] - 0.2) < 1e-9


def test_save_below_threshold_is_skipped(tmp_path, monkeypatch):
    """save_weights must not touch the file if the change is tiny."""
    import utils.weights_store as ws

    path = str(tmp_path / "model_weights.json")
    monkeypatch.setattr(ws, "_WEIGHTS_PATH", path)

    weights = {"gfs_seamless": 0.5, "ecmwf_ifs025": 0.5}
    ws.save_weights(weights, apply_floor=False, min_change=0)
    mtime_before = os.path.getmtime(path)

    # No-op save (no change at all) must not write.
    assert ws.save_weights(weights, apply_floor=False) is False
    mtime_after = os.path.getmtime(path)
    assert mtime_after == mtime_before

    # Save with delta = 0.0001 < 0.001 threshold must also not write.
    assert (
        ws.save_weights(
            {"gfs_seamless": 0.5001, "ecmwf_ifs025": 0.5}, apply_floor=False
        )
        is False
    )
    assert os.path.getmtime(path) == mtime_before

    # Save with delta = 0.01 > threshold must write.
    assert (
        ws.save_weights({"gfs_seamless": 0.51, "ecmwf_ifs025": 0.5}, apply_floor=False)
        is True
    )


def test_sialoop_loads_persisted_weights_on_init(tmp_path, monkeypatch):
    """SIALoop.__init__ must merge persisted weights over the in-memory defaults."""
    import utils.weights_store as ws

    path = str(tmp_path / "model_weights.json")
    monkeypatch.setattr(ws, "_WEIGHTS_PATH", path)

    # Pre-populate: override one model, leave others at default.
    # Save ALL models so floor doesn't shift the values we care about.
    all_models = {
        "gfs_seamless": 0.42,
        "ecmwf_ifs025": 0.99,
        "gem_global": 0.05,
        "icon_global": 0.05,
        "jma_seamless": 0.05,
        "cma_grapes_global": 0.05,
        "ukmo_seamless": 0.05,
        "meteofrance_seamless": 0.05,
    }
    ws.save_weights(all_models, apply_floor=False, min_change=0)

    # Reset the import cache so SIALoop re-evaluates load_weights().
    import importlib

    import engine.strategy as strategy_mod

    importlib.reload(strategy_mod)

    sia = strategy_mod.SIALoop(None, strategy_mod.config)
    # Overridden values picked up from disk.
    assert abs(sia.model_weights["gfs_seamless"] - 0.42) < 1e-6
    assert abs(sia.model_weights["ecmwf_ifs025"] - 0.99) < 1e-6
    # Models at floor weight should still match (or be very close to default).
    assert sia.model_weights["gem_global"] > 0


def test_corrupt_json_returns_none(tmp_path, monkeypatch):
    """A malformed file must not crash the bot -- load returns None."""
    import utils.weights_store as ws

    path = str(tmp_path / "model_weights.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    monkeypatch.setattr(ws, "_WEIGHTS_PATH", path)
    assert ws.load_weights() is None
