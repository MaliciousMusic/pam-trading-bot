"""WS1 — Exit Attribution Analysis (Phase 11 Diagnostic).

Parses ledger CLOSE events from a Merciless sprint run, cross-joins with
decisions.jsonl to attach regime labels at close time, and emits the
tables + conclusions required by WS1_exit_attribution.md.

No code-under-test modifications — pure measurement.

Outputs (stdout + optional --out markdown dump):
  1. Exit-reason distribution (count, %, mean/median/p25/p75 realized_bps, mean hold_seconds)
  2. Exit reason × regime label cross-tab (counts + realized_bps mean)
  3. Exit reason × hold_seconds bucket (proxy for peak_favorable_s — see note below)
  4. Per-horizon MAE and directional hit-rate from data/forecasts/YYYY-MM-DD.jsonl

Instrumentation gaps (flagged for Phase 11 to close):
  * `decisions.jsonl` persists `expected_move_bps, confidence` but NOT
    `forecast.steps_bps`, so `peak_favorable_s` cannot be recovered post-hoc.
    We substitute `held_seconds` bucketed (short <=60s / medium 60-180s /
    long >180s) as a proxy for when the position would have peaked.
  * Ledger `close` events do not carry `regime_label`. We cross-join with
    decisions.jsonl (nearest tick by ts) to attach regime at close time.
  * `SprintEngine` does not wire the ForecastAuditor, so resolved-anchor
    MAE/hit-rate comes from pre-existing live-session data (tick_seconds=5
    schema — labels "15s" + "1m" are actually 5s and 60s horizons).
"""

from __future__ import annotations

import argparse
import bisect
import io
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout on Windows so unicode box/arrow chars don't blow up.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Reason canonicalization
# ---------------------------------------------------------------------------

REASON_PREFIXES = [
    ("hard SL floor",            "hard_sl"),
    ("strong flip",              "strong_flip"),
    ("confirmed weak flip",      "weak_flip"),
    ("ratchet retrace",          "ratchet_retrace"),
    ("forecast target hit",      "forecast_target_tp"),
    ("past forecast peak",       "shape_aware_peak"),
    ("edge exhausted",           "edge_exhausted"),
    ("sprint end",               "sprint_end"),
]


def canon_reason(full: str) -> str:
    for pfx, key in REASON_PREFIXES:
        if full.startswith(pfx):
            return key
    return "other"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_ledger(path: Path) -> tuple[dict, list[dict]]:
    opens: dict[str, dict] = {}
    closes: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("event") == "open":
                opens[d["position_id"]] = d
            elif d.get("event") == "close":
                closes.append(d)
    return opens, closes


def load_decisions(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_ts(s: str) -> float:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def attach_regimes(closes: list[dict], decisions: list[dict]) -> None:
    """Cross-join: for each close, find the nearest decision by ts and set
    c['regime'] from that decision."""
    if not decisions:
        for c in closes:
            c["regime"] = "unknown"
        return
    d_ts = []
    d_reg = []
    for d in decisions:
        ts_val = d.get("ts")
        if ts_val is None:
            continue
        try:
            t = parse_ts(ts_val) if isinstance(ts_val, str) else float(ts_val)
        except Exception:
            continue
        d_ts.append(t)
        d_reg.append(d.get("regime", d.get("regime_label", "unknown")))

    pairs = sorted(zip(d_ts, d_reg), key=lambda p: p[0])
    d_ts = [p[0] for p in pairs]
    d_reg = [p[1] for p in pairs]

    for c in closes:
        try:
            t = parse_ts(c["ts"])
        except Exception:
            c["regime"] = "unknown"
            continue
        idx = bisect.bisect_left(d_ts, t)
        candidates = []
        if idx > 0:
            candidates.append((abs(t - d_ts[idx-1]), d_reg[idx-1]))
        if idx < len(d_ts):
            candidates.append((abs(t - d_ts[idx]), d_reg[idx]))
        if candidates:
            candidates.sort()
            c["regime"] = candidates[0][1] or "unknown"
        else:
            c["regime"] = "unknown"


# ---------------------------------------------------------------------------
# Stat helpers
# ---------------------------------------------------------------------------

def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k); hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _safe_mean(values: list[float]) -> float:
    return statistics.mean(values) if values else float("nan")


def _safe_median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def hold_bucket(held_s: float) -> str:
    if held_s <= 60:
        return "short ≤60s"
    if held_s <= 180:
        return "medium 60-180s"
    return "long >180s"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def table_reason_distribution(closes: list[dict]) -> str:
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for c in closes:
        by_reason[canon_reason(c["reason"])].append(c)

    total = len(closes)
    lines = []
    lines.append("| reason               |   N |    % | realized_bps mean | median |   p25 |   p75 | hold_s mean | wins | win_rate |")
    lines.append("|:---------------------|----:|-----:|------------------:|-------:|------:|------:|------------:|-----:|---------:|")
    for reason in sorted(by_reason.keys(), key=lambda k: -len(by_reason[k])):
        rows = by_reason[reason]
        rb = [r["realized_bps"] for r in rows]
        hs = [r["held_seconds"] for r in rows]
        wins = sum(1 for x in rb if x > 0)
        lines.append(
            f"| {reason:<20} | {len(rows):>3} | "
            f"{len(rows)/total*100:>4.1f} | "
            f"{_safe_mean(rb):>+17.2f} | "
            f"{_safe_median(rb):>+6.2f} | "
            f"{_pct(rb, 0.25):>+5.1f} | "
            f"{_pct(rb, 0.75):>+5.1f} | "
            f"{_safe_mean(hs):>11.1f} | "
            f"{wins:>4} | "
            f"{wins/len(rb)*100 if rb else 0:>7.1f}% |"
        )
    lines.append(f"\n**Total closes: {total}**")
    return "\n".join(lines)


def table_crosstab_reason_regime(closes: list[dict]) -> str:
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    reasons = set(); regimes = set()
    for c in closes:
        r = canon_reason(c["reason"])
        g = c.get("regime", "unknown")
        by_pair[(r, g)].append(c)
        reasons.add(r); regimes.add(g)

    regimes_sorted = sorted(regimes)
    reasons_sorted = sorted(reasons, key=lambda k: -sum(1 for c in closes if canon_reason(c["reason"]) == k))

    hdr = "| reason \\\\ regime       |" + "|".join(f" {g:>16} " for g in regimes_sorted) + "|"
    sep = "|:----------------------|" + "|".join(":---------------:" for _ in regimes_sorted) + "|"
    lines = [hdr, sep]
    for r in reasons_sorted:
        cells = []
        for g in regimes_sorted:
            rows = by_pair.get((r, g), [])
            if rows:
                n = len(rows)
                m = _safe_mean([x["realized_bps"] for x in rows])
                cells.append(f" N={n:>3}  μ={m:>+5.1f}bps ")
            else:
                cells.append(f" {'—':^16} ")
        lines.append(f"| {r:<20} |" + "|".join(cells) + "|")
    return "\n".join(lines)


def table_crosstab_reason_hold(closes: list[dict]) -> str:
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    reasons = set()
    for c in closes:
        r = canon_reason(c["reason"])
        b = hold_bucket(c["held_seconds"])
        by_pair[(r, b)].append(c)
        reasons.add(r)

    buckets = ["short ≤60s", "medium 60-180s", "long >180s"]
    reasons_sorted = sorted(reasons, key=lambda k: -sum(1 for c in closes if canon_reason(c["reason"]) == k))

    hdr = "| reason \\\\ hold bucket |" + "|".join(f" {b:>17} " for b in buckets) + "|"
    sep = "|:----------------------|" + "|".join(":-----------------:" for _ in buckets) + "|"
    lines = [hdr, sep]
    for r in reasons_sorted:
        cells = []
        for b in buckets:
            rows = by_pair.get((r, b), [])
            if rows:
                n = len(rows)
                m = _safe_mean([x["realized_bps"] for x in rows])
                cells.append(f" N={n:>3}  μ={m:>+5.1f}bps ")
            else:
                cells.append(f" {'—':^17} ")
        lines.append(f"| {r:<20} |" + "|".join(cells) + "|")
    return "\n".join(lines)


def table_signal_strength(opens: dict, closes: list[dict]) -> str:
    """For each closed position, compute:
       - true mark->mark directional hit (excluding slippage)
       - slice by council expected_move_bps_raw magnitude
       - compare realized net (includes friction) with mark move
    """
    rows = []
    for c in closes:
        o = opens.get(c["position_id"])
        if not o:
            continue
        mark_entry = o["mark_price"]
        mark_close = c["mark_price"]
        side = c["side"]
        mv = (mark_close - mark_entry) / mark_entry * 10000
        if side == "sell":
            mv = -mv  # sign to "in-favor direction"
        rows.append({
            "pid": c["position_id"],
            "side": side,
            "emove_raw": abs(float(o.get("expected_move_bps_raw", 0.0))),
            "conf": float(o.get("confidence", 0.0)),
            "mark_move_in_dir_bps": mv,
            "realized_net_bps": c["realized_bps"],
            "held_s": c["held_seconds"],
        })

    if not rows:
        return "(no matched opens↔closes)"

    lines = []
    n = len(rows)
    hits = sum(1 for r in rows if r["mark_move_in_dir_bps"] > 0)
    lines.append(f"- Sample: {n} closed positions with matched opens")
    lines.append(f"- **Mark→mark directional hit (frictionless):** {hits}/{n} = {hits/n*100:.1f}%")
    lines.append(f"- Mean mark-move in predicted direction: {_safe_mean([r['mark_move_in_dir_bps'] for r in rows]):+.2f}bps  (median {_safe_median([r['mark_move_in_dir_bps'] for r in rows]):+.2f}bps)")
    lines.append(f"- Mean realized_net (post-friction): {_safe_mean([r['realized_net_bps'] for r in rows]):+.2f}bps  (median {_safe_median([r['realized_net_bps'] for r in rows]):+.2f}bps)")
    lines.append("")
    lines.append("Slice by council |expected_move_bps_raw| at entry:")
    lines.append("| bucket             |   N | mark-hit | mark μ bps | net μ bps | conf μ |")
    lines.append("|:-------------------|----:|---------:|-----------:|----------:|-------:|")

    buckets: list[tuple[str, callable]] = [
        ("strong ≥10bps",  lambda e: e >= 10),
        ("moderate 5-10",  lambda e: 5 <= e < 10),
        ("mild 1-5",       lambda e: 1 <= e < 5),
        ("trivial <1",     lambda e: e < 1),
    ]
    for label, pred in buckets:
        sub = [r for r in rows if pred(r["emove_raw"])]
        if not sub:
            continue
        nb = len(sub)
        hits = sum(1 for r in sub if r["mark_move_in_dir_bps"] > 0)
        lines.append(
            f"| {label:<18} | {nb:>3} | {hits/nb*100:>7.1f}% | "
            f"{_safe_mean([r['mark_move_in_dir_bps'] for r in sub]):>+10.2f} | "
            f"{_safe_mean([r['realized_net_bps'] for r in sub]):>+9.2f} | "
            f"{_safe_mean([r['conf'] for r in sub]):>6.2f} |"
        )

    return "\n".join(lines)


def table_edge_exhausted_remaining(closes: list[dict]) -> str:
    """Parse the 'remaining=+X.Xbps' field from edge-exhausted reasons."""
    import re
    rx = re.compile(r"remaining=([+\-]?[0-9.]+)bps")
    vals = []
    for c in closes:
        if not c["reason"].startswith("edge exhausted"):
            continue
        m = rx.search(c["reason"])
        if m:
            vals.append(float(m.group(1)))
    if not vals:
        return "(no edge-exhausted closes)"
    return (
        f"- Edge-exhausted remaining-bps distribution (N={len(vals)}):\n"
        f"  - mean={_safe_mean(vals):+.2f}bps, median={_safe_median(vals):+.2f}bps\n"
        f"  - p10={_pct(vals, 0.1):+.2f}, p90={_pct(vals, 0.9):+.2f}\n"
        f"  - % with remaining <= 0 (forecast says REVERSE): "
        f"{sum(1 for v in vals if v <= 0)/len(vals)*100:.1f}%\n"
        f"  - Context: friction threshold ~8bps under Merciless; this fires "
        f"when |remaining| < threshold."
    )


def table_counterfactual_hold(opens: dict, closes: list[dict], decisions: list[dict]) -> str:
    """For each closed position, compute what mark-move would have been if
    held 60s, 120s, 250s instead of the 20s the exit layer enforced.

    Answers the key WS1 question: is the exit truncating real edge?
    """
    if not decisions:
        return "(no decisions — cannot run counterfactual)"

    price_ts: list[float] = []
    prices: list[float] = []
    for d in decisions:
        ts_val = d.get("ts")
        price = d.get("price")
        if ts_val is None or price is None:
            continue
        try:
            t = parse_ts(ts_val) if isinstance(ts_val, str) else float(ts_val)
        except Exception:
            continue
        price_ts.append(t)
        prices.append(float(price))

    def px_at(t_target: float) -> float | None:
        idx = bisect.bisect_left(price_ts, t_target)
        if idx >= len(price_ts):
            return None  # past end of data
        return prices[idx]

    horizons = [20, 60, 120, 180, 250]
    summary: dict[int, list[float]] = {h: [] for h in horizons}
    hits: dict[int, int] = {h: 0 for h in horizons}
    counts: dict[int, int] = {h: 0 for h in horizons}

    for c in closes:
        pid = c["position_id"]
        o = opens.get(pid)
        if not o:
            continue
        try:
            t_open = parse_ts(o["ts"])
        except Exception:
            continue
        mark_entry = o["mark_price"]
        side = c["side"]
        for h in horizons:
            px = px_at(t_open + h)
            if px is None:
                continue
            mv = (px - mark_entry) / mark_entry * 10000
            if side == "sell":
                mv = -mv
            summary[h].append(mv)
            if mv > 0:
                hits[h] += 1
            counts[h] += 1

    lines = [
        "For each closed position, compute price move in predicted direction",
        "at counterfactual hold horizons (what if the exit didn't fire at 20s?):",
        "",
        "| hold   |   N | direction-hit % | mean mark-move bps | median | p25    | p75   | net vs 8bps fees |",
        "|:-------|----:|----------------:|-------------------:|-------:|-------:|------:|-----------------:|",
    ]
    for h in horizons:
        arr = summary[h]
        if not arr:
            continue
        lines.append(
            f"| {h:>3}s   | {len(arr):>3} | "
            f"{hits[h]/len(arr)*100:>14.1f}% | "
            f"{_safe_mean(arr):>+18.2f} | "
            f"{_safe_median(arr):>+6.2f} | "
            f"{_pct(arr, 0.25):>+6.2f} | "
            f"{_pct(arr, 0.75):>+5.1f} | "
            f"{_safe_mean(arr) - 8.0:>+16.2f} |"
        )
    lines.append("")
    lines.append("**Interpretation:** if 'mean mark-move' increases with hold, exit truncates real edge (A).")
    lines.append("If it stays flat/negative, holding longer doesn't help — signal is weak (B).")
    return "\n".join(lines)


def table_counterfactual_by_regime(opens: dict, closes: list[dict], decisions: list[dict]) -> str:
    """Break down counterfactual hold by regime label at close."""
    if not decisions:
        return "(no decisions)"
    price_ts: list[float] = []
    prices: list[float] = []
    for d in decisions:
        ts_val = d.get("ts")
        price = d.get("price")
        if ts_val is None or price is None:
            continue
        try:
            t = parse_ts(ts_val) if isinstance(ts_val, str) else float(ts_val)
        except Exception:
            continue
        price_ts.append(t); prices.append(float(price))

    def px_at(t_target: float):
        idx = bisect.bisect_left(price_ts, t_target)
        return prices[idx] if idx < len(price_ts) else None

    by_regime: dict[str, list[tuple]] = defaultdict(list)
    for c in closes:
        pid = c["position_id"]
        o = opens.get(pid)
        if not o:
            continue
        try: t_open = parse_ts(o["ts"])
        except Exception: continue
        side = c["side"]; mark_entry = o["mark_price"]
        row = []
        for h in (20, 60, 250):
            px = px_at(t_open + h)
            if px is None: row.append(None); continue
            mv = (px - mark_entry) / mark_entry * 10000
            if side == "sell": mv = -mv
            row.append(mv)
        by_regime[c.get("regime", "unknown")].append(row)

    lines = [
        "| regime       |   N | mean mv @ 20s | @ 60s  | @ 250s | skill persists? |",
        "|:-------------|----:|--------------:|-------:|-------:|:----------------|",
    ]
    for reg, rows in sorted(by_regime.items(), key=lambda p: -len(p[1])):
        n = len(rows)
        m20 = [r[0] for r in rows if r[0] is not None]
        m60 = [r[1] for r in rows if r[1] is not None]
        m250 = [r[2] for r in rows if r[2] is not None]
        v20 = _safe_mean(m20) if m20 else float("nan")
        v60 = _safe_mean(m60) if m60 else float("nan")
        v250 = _safe_mean(m250) if m250 else float("nan")
        persists = "YES (rises)" if v60 > v20 and v250 > v20 else ("PARTIAL" if v60 > v20 else "NO (reverts)")
        lines.append(
            f"| {reg:<12} | {n:>3} | {v20:>+13.2f} | {v60:>+6.2f} | {v250:>+6.2f} | {persists} |"
        )
    return "\n".join(lines)


def table_conviction_slice(opens: dict, closes: list[dict]) -> str:
    """Does higher council confidence correlate with better frictionless edge?"""
    by_conf: dict[str, list[float]] = defaultdict(list)
    for c in closes:
        o = opens.get(c["position_id"])
        if not o: continue
        conf = float(o.get("confidence", 0.0))
        mark_entry = o["mark_price"]; mark_close = c["mark_price"]
        side = c["side"]
        mv = (mark_close - mark_entry) / mark_entry * 10000
        if side == "sell": mv = -mv
        bucket = ("high >=0.5" if conf >= 0.5
                  else "med 0.25-0.5" if conf >= 0.25
                  else "low 0.10-0.25" if conf >= 0.10
                  else "trivial <0.10")
        by_conf[bucket].append(mv)

    order = ["high >=0.5", "med 0.25-0.5", "low 0.10-0.25", "trivial <0.10"]
    lines = [
        "| conf bucket    |   N | mark-hit % | mean mark-move bps |",
        "|:---------------|----:|-----------:|-------------------:|",
    ]
    for b in order:
        arr = by_conf.get(b, [])
        if not arr: continue
        hits = sum(1 for v in arr if v > 0)
        lines.append(
            f"| {b:<14} | {len(arr):>3} | {hits/len(arr)*100:>9.1f}% | {_safe_mean(arr):>+18.2f} |"
        )
    return "\n".join(lines)


def table_per_horizon_mae(forecasts_path: Path) -> str:
    if not forecasts_path.exists():
        return f"(forecasts file not found at {forecasts_path})"
    by_key: dict[tuple[str, int], list[dict]] = defaultdict(list)
    with forecasts_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_key[(d.get("label", "?"), int(d.get("horizon_s", 0)))].append(d)

    lines = [
        "| label | horizon_s |      N | MAE bps | median abs err | directional hit | |expected| μ | |realized| μ | conf μ |",
        "|:------|----------:|-------:|--------:|---------------:|----------------:|--------------:|--------------:|-------:|",
    ]
    for (label, hz), rows in sorted(by_key.items()):
        abs_err = [r["abs_error_bps"] for r in rows]
        hit = [1 if r["directional_hit"] else 0 for r in rows]
        emove = [abs(r["expected_move_bps"]) for r in rows]
        rmove = [abs(r["realized_move_bps"]) for r in rows]
        conf = [r["forecast_conf"] for r in rows]
        lines.append(
            f"| {label:<5} | {hz:>9} | {len(rows):>6} | "
            f"{_safe_mean(abs_err):>7.2f} | "
            f"{_safe_median(abs_err):>14.2f} | "
            f"{sum(hit)/len(hit)*100:>14.2f}% | "
            f"{_safe_mean(emove):>13.2f} | "
            f"{_safe_mean(rmove):>13.2f} | "
            f"{_safe_mean(conf):>6.3f} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True,
                    help="e.g. data/replays/ws1_merciless_seed42/variants/full_day")
    ap.add_argument("--day", default="2026-04-20",
                    help="An-0 day (matches ledger filename + forecasts filename)")
    ap.add_argument("--forecasts-dir", default="data/forecasts",
                    help="Path to forecast auditor output dir")
    ap.add_argument("--out", default=None,
                    help="Write full markdown report to this file (also prints to stdout)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    ledger_path = run_dir / "ledger" / f"{args.day}.jsonl"
    decisions_path = run_dir / "decisions.jsonl"
    forecasts_path = Path(args.forecasts_dir) / f"{args.day}.jsonl"

    print(f"Loading ledger:    {ledger_path}")
    print(f"Loading decisions: {decisions_path}")
    print(f"Loading forecasts: {forecasts_path}")
    print()

    opens, closes = load_ledger(ledger_path)
    decisions = load_decisions(decisions_path) if decisions_path.exists() else []
    attach_regimes(closes, decisions)

    print(f"opens={len(opens)}  closes={len(closes)}  decisions={len(decisions)}")
    print()

    sections: list[tuple[str, str]] = []

    sections.append(("1. Exit-Reason Distribution", table_reason_distribution(closes)))
    sections.append(("2. Edge-Exhausted 'Remaining Edge' Distribution", table_edge_exhausted_remaining(closes)))
    sections.append(("3. Exit Reason × Regime Label", table_crosstab_reason_regime(closes)))
    sections.append(("4. Exit Reason × Hold-Seconds Bucket (peak_favorable_s proxy)", table_crosstab_reason_hold(closes)))
    sections.append(("5. Council Signal Strength (mark->mark hit by forecast magnitude)", table_signal_strength(opens, closes)))
    sections.append(("6. Counterfactual: Mark-Move at Longer Hold Horizons", table_counterfactual_hold(opens, closes, decisions)))
    sections.append(("6b. Counterfactual by Regime Label", table_counterfactual_by_regime(opens, closes, decisions)))
    sections.append(("6c. Mark-Move Skill by Council Confidence Bucket", table_conviction_slice(opens, closes)))
    sections.append(("7. Per-Horizon MAE & Hit-Rate (ForecastAuditor)", table_per_horizon_mae(forecasts_path)))

    body = []
    for title, md in sections:
        body.append(f"## {title}\n\n{md}\n")
    full = "\n".join(body)
    print(full)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(full)
        print(f"\nWrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
