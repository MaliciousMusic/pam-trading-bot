"""Unit tests for WS3 H1 Phase 1 decomposition analyzer.

Covers the script's structural contract per ``WS3_H1_SCOPE.md`` §3.2:

  - Percentile helper boundary cases (empty, single, interior).
  - Pre-I1 record detection (one None in any of the 5 I1 fields skips).
  - ``analyze()`` multiplicative cross-check exposes schema drift.
  - Horizon stratification covers every label present in the input.
  - Top-quantile subset correctness on a known input.
  - End-to-end ``main()`` smoke: writes JSON artifact, exit 0.

Run:  python -m pytest tests/test_ws3_h1_decomp_analyze.py -q
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scripts.ws3_h1_decomp_analyze import (
    DECOMP_FIELDS,
    I1_REQUIRED,
    _distribution,
    _is_post_i1,
    _percentile,
    analyze,
    format_report,
    main,
)


# ---------------------------------------------------------------- helpers

def _make_record(
    *,
    signal_quality: float | None = 0.4,
    directional_score: float | None = 0.6,
    diversity_score: float | None = 0.7,
    diversity_mult: float | None = 0.88,
    label: str = "1m",
    forecast_conf: float | None = None,
    base_conf: float | None = None,
) -> dict:
    """Build a post-I1-shaped forecast record.

    By default ``base_conf`` and ``forecast_conf`` satisfy the
    multiplicative identity (so the sanity cross-check diffs are zero).
    Pass explicit values to inject drift.
    """
    if base_conf is None and signal_quality is not None and directional_score is not None:
        base_conf = signal_quality * directional_score
    if forecast_conf is None and base_conf is not None and diversity_mult is not None:
        forecast_conf = base_conf * diversity_mult
    return {
        "tick_ts": 1_700_000_000.0,
        "target_ts": 1_700_000_060.0,
        "label": label,
        "horizon_s": 60,
        "entry_price": 50_000.0,
        "predicted_price": 50_050.0,
        "realized_price": 50_030.0,
        "expected_move_bps": 10.0,
        "realized_move_bps": 6.0,
        "directional_hit": True,
        "forecast_conf": forecast_conf,
        "net_pressure": 0.1,
        "signal_quality": signal_quality,
        "directional_score": directional_score,
        "base_conf": base_conf,
        "diversity_score": diversity_score,
        "diversity_mult": diversity_mult,
    }


# ---------------------------------------------------------------- percentile

def test_percentile_empty_returns_nan():
    assert math.isnan(_percentile([], 50.0))


def test_percentile_single_element_returns_that_element():
    assert _percentile([7.5], 10.0) == 7.5
    assert _percentile([7.5], 99.0) == 7.5


def test_percentile_interior_linear_interpolation():
    # Uniform [0..10] — 50th percentile = 5.0, 10th = 1.0, 90th = 9.0.
    xs = list(range(11))
    assert _percentile(xs, 50.0) == 5.0
    assert _percentile(xs, 10.0) == pytest.approx(1.0)
    assert _percentile(xs, 90.0) == pytest.approx(9.0)


def test_percentile_matches_numpy_linear():
    # Reference: numpy.percentile([1,2,3,4], 25) == 1.75 under 'linear' method.
    assert _percentile([1.0, 2.0, 3.0, 4.0], 25.0) == pytest.approx(1.75)
    assert _percentile([1.0, 2.0, 3.0, 4.0], 75.0) == pytest.approx(3.25)


# ---------------------------------------------------------------- _distribution

def test_distribution_empty_is_none_valued_but_structured():
    d = _distribution([])
    assert d["n"] == 0
    assert d["mean"] is None
    assert set(d["percentiles"].keys()) == {10, 25, 50, 75, 90, 95, 99}
    assert all(v is None for v in d["percentiles"].values())


def test_distribution_simple_mean_and_median():
    d = _distribution([0.0, 0.5, 1.0])
    assert d["n"] == 3
    assert d["mean"] == pytest.approx(0.5)
    assert d["percentiles"][50] == pytest.approx(0.5)


# ---------------------------------------------------------------- _is_post_i1

def test_is_post_i1_all_fields_populated():
    assert _is_post_i1(_make_record()) is True


@pytest.mark.parametrize("field", list(I1_REQUIRED))
def test_is_post_i1_any_missing_field_returns_false(field):
    rec = _make_record()
    rec[field] = None
    assert _is_post_i1(rec) is False


def test_is_post_i1_missing_key_treated_as_none():
    rec = _make_record()
    del rec["signal_quality"]
    assert _is_post_i1(rec) is False


# ---------------------------------------------------------------- analyze — happy path

def test_analyze_horizon_stratification_covers_all_labels():
    records = [
        _make_record(label="15s"),
        _make_record(label="1m"),
        _make_record(label="1m"),
        _make_record(label="5m"),
    ]
    result = analyze(records, top_conf_quantile=None)

    assert result["n_total"] == 4
    assert result["n_horizons"] == 3
    assert set(result["by_horizon"].keys()) == {"15s", "1m", "5m"}
    assert result["by_horizon"]["1m"]["signal_quality"]["n"] == 2
    assert result["by_horizon"]["15s"]["signal_quality"]["n"] == 1


def test_analyze_sanity_cross_check_is_zero_when_identity_holds():
    # 20 records all built from the identity — diffs should be exactly 0.
    records = [_make_record(signal_quality=0.3 + 0.01 * i,
                            directional_score=0.5 + 0.01 * i)
               for i in range(20)]
    result = analyze(records, top_conf_quantile=None)
    s = result["sanity"]
    assert s["n_base_pairs"] == 20
    assert s["n_final_pairs"] == 20
    assert s["mean_abs_diff_base"] == pytest.approx(0.0, abs=1e-12)
    assert s["mean_abs_diff_final"] == pytest.approx(0.0, abs=1e-12)


def test_analyze_sanity_cross_check_catches_drift():
    # Inject drift by setting base_conf != sq * dr on one record.
    records = [_make_record() for _ in range(5)]
    records[0]["base_conf"] = 0.99  # differs from sq*dr = 0.4*0.6 = 0.24
    result = analyze(records, top_conf_quantile=None)
    assert result["sanity"]["max_abs_diff_base"] > 0.5


def test_analyze_top_subset_selects_high_conf_records():
    # 10 records with forecast_conf linearly spaced 0.1 … 1.0.
    records = []
    for i in range(10):
        sq = 0.5
        dr = 0.1 + 0.1 * i  # 0.1 .. 1.0
        records.append(_make_record(signal_quality=sq,
                                    directional_score=dr))
    result = analyze(records, top_conf_quantile=0.8)
    top = result["top_subset"]
    assert top is not None
    assert top["quantile"] == 0.8
    # Cutoff at p80 of the sorted forecast_conf list — top subset
    # should contain roughly the top 20% of records (≥ 2 of 10).
    assert top["n"] >= 2
    assert top["n"] <= 5


def test_analyze_top_subset_none_when_quantile_none():
    records = [_make_record() for _ in range(3)]
    result = analyze(records, top_conf_quantile=None)
    assert result["top_subset"] is None


def test_analyze_empty_records_returns_structured_zeros():
    result = analyze([], top_conf_quantile=0.9)
    assert result["n_total"] == 0
    assert result["n_horizons"] == 0
    for f in DECOMP_FIELDS:
        assert result["overall"][f]["n"] == 0
    # Top-subset over zero records returns n=0 (reported quantile but
    # no data) — not None, since the caller asked for it.
    assert result["top_subset"]["n"] == 0


# ---------------------------------------------------------------- format_report

def test_format_report_renders_every_decomp_field():
    records = [_make_record() for _ in range(3)]
    result = analyze(records, top_conf_quantile=0.5)
    result["scan"] = {"days": ["2026-04-24"], "n_scanned": 3,
                      "n_post_i1": 3, "pre_i1_skipped": 0}
    report = format_report(result)
    for field in DECOMP_FIELDS:
        assert field in report
    assert "Per horizon (stratified)" in report
    assert "Sanity cross-check" in report


# ---------------------------------------------------------------- main() smoke

def test_main_end_to_end_writes_artifact(tmp_path: Path, capsys):
    # Two-day panel: day A has post-I1 records, day B has one pre-I1
    # record (to verify filter + scan counters).
    forecasts_root = tmp_path / "forecasts"
    forecasts_root.mkdir()

    day_a = forecasts_root / "2026-04-24.jsonl"
    with day_a.open("w") as f:
        for _ in range(5):
            f.write(json.dumps(_make_record(label="1m")) + "\n")
        for _ in range(3):
            f.write(json.dumps(_make_record(label="5m")) + "\n")

    day_b = forecasts_root / "2026-04-25.jsonl"
    with day_b.open("w") as f:
        pre = _make_record(label="15s")
        pre["signal_quality"] = None  # a pre-I1 record
        f.write(json.dumps(pre) + "\n")
        f.write(json.dumps(_make_record(label="15s")) + "\n")

    out_dir = tmp_path / "out"
    rc = main([
        "--days", "2026-04-24", "2026-04-25",
        "--forecasts-root", str(forecasts_root),
        "--out", str(out_dir),
        "--top-conf-quantile", "0.9",
    ])
    assert rc == 0

    artefacts = list(out_dir.glob("decomp_*.json"))
    assert len(artefacts) == 1
    with artefacts[0].open() as f:
        payload = json.load(f)
    # day A: 5 × 1m + 3 × 5m = 8 post-I1
    # day B: 1 pre-I1 + 1 post-I1 (15s)
    # → scanned 10, post-I1 9, pre-I1 1
    assert payload["scan"]["n_scanned"] == 10
    assert payload["scan"]["n_post_i1"] == 9
    assert payload["scan"]["pre_i1_skipped"] == 1
    # Horizons should be the 3 labels from post-I1 records only.
    assert set(payload["by_horizon"].keys()) == {"1m", "5m", "15s"}

    captured = capsys.readouterr()
    assert "Records analysed: **9**" in captured.out


def test_main_exits_2_when_no_post_i1_records(tmp_path: Path, capsys):
    forecasts_root = tmp_path / "forecasts"
    forecasts_root.mkdir()

    day = forecasts_root / "2026-04-24.jsonl"
    with day.open("w") as f:
        pre = _make_record()
        pre["directional_score"] = None
        f.write(json.dumps(pre) + "\n")

    rc = main([
        "--days", "2026-04-24",
        "--forecasts-root", str(forecasts_root),
        "--out", "",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no post-I1 records" in err


def test_main_accepts_out_empty_string_disables_artifact(
    tmp_path: Path, monkeypatch, capsys,
):
    # Run in an isolated CWD so any accidental write ends up here, not
    # in the repo root — guards the regression that leaked artifacts to
    # PROJECT_ROOT when argparse coerced "" into Path("").
    monkeypatch.chdir(tmp_path)

    forecasts_root = tmp_path / "forecasts"
    forecasts_root.mkdir()
    day = forecasts_root / "2026-04-24.jsonl"
    with day.open("w") as f:
        f.write(json.dumps(_make_record()) + "\n")

    rc = main([
        "--days", "2026-04-24",
        "--forecasts-root", str(forecasts_root),
        "--out", "",
    ])
    assert rc == 0
    # No artifact anywhere — walk the isolated CWD and assert nothing
    # matching the decomp_*.json pattern was written.
    leaked = list(tmp_path.rglob("decomp_*.json"))
    assert leaked == [], f"--out '' leaked artifacts: {leaked}"
