"""Phase G — council-orthogonality analyzer.

Goes beyond the pairwise Pearson view of `analyze_vote_correlation.py` and
asks: *how many independent signals does the council actually carry?*

Three views, all computed from the signed-vote matrix V of shape
(n_ticks, n_tribes), where
    V[t, k] = +conviction   if tribe k voted buy on tick t
              -conviction   if tribe k voted sell on tick t
              0.0           if abstain or absent that tick.

Views:

1. **Cosine similarity** (intensity-preserving). Unlike Pearson, does NOT
   mean-subtract, so a tribe that almost always votes "buy with conv 0.8"
   registers as highly similar to another tribe with the same constant
   bias. That's the correct notion of "passenger" — riding the same
   side most of the time even if the variance is low.

2. **Directional-sign cosine** (conviction-free). Same thing but V is
   binarized to {-1, 0, +1}. Strips intensity, keeps side. Useful to tell
   whether two tribes disagree on intensity but still agree on direction
   (common: a noisy TA tribe and a low-conviction systematic trend tribe).

3. **PCA via SVD** on a column-centered V. Shows effective dimensionality:
   how many components explain 95% of council variance. If 27 tribes
   collapse to 3 components, the council is rank-3 — it's doing 3 things,
   not 27, and we're paying 27× inference cost for 3× signal.

Outputs:
  - Per-tribe activity rate (non-abstain fraction).
  - Both cos-sim matrices, printed in a clustered order (tribes re-ordered
    by 1st principal-component loading so redundant clusters appear as
    blocks on the diagonal).
  - Top 15 |cos-sim| pairs with REDUNDANT flag above threshold.
  - Lone-wolves (max |cos-sim| to any other tribe < 0.30).
  - PCA scree: variance per component, cumulative, effective rank @ 95%.
  - Optional JSON dump for downstream tooling.

Usage:
    python -m scripts.analyze_council_orthogonality 2026-04-20
    python -m scripts.analyze_council_orthogonality 2026-04-20 --threshold 0.60
    python -m scripts.analyze_council_orthogonality 2026-04-20 --json out.json
    python -m scripts.analyze_council_orthogonality --days 2026-04-18 2026-04-19 2026-04-20
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

from src.core import serde
from src.core.config import PROJECT_ROOT


LEDGER_DIR = PROJECT_ROOT / "data" / "ledger"
DEFAULT_REDUNDANCY = 0.70
LONE_WOLF_THRESHOLD = 0.30


# -----------------------------------------------------------------------------
# Ledger streaming
# -----------------------------------------------------------------------------


def _iter_tick_votes(path: Path) -> Iterable[dict]:
    """Yield each tick's votes payload (list of {tribe_id, side, conviction, ...})."""
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = serde.loads(line)
            except Exception:
                continue
            if rec.get("event") != "tick":
                continue
            yield rec.get("votes") or []


def _signed_vote(v: dict) -> float:
    side = str(v.get("side", "abstain")).lower()
    conv = float(v.get("conviction") or 0.0)
    if side == "buy":
        return conv
    if side == "sell":
        return -conv
    return 0.0


def _signed_dir(v: dict) -> float:
    side = str(v.get("side", "abstain")).lower()
    if side == "buy":
        return 1.0
    if side == "sell":
        return -1.0
    return 0.0


# -----------------------------------------------------------------------------
# Matrix build
# -----------------------------------------------------------------------------


def build_vote_matrices(paths: list[Path]) -> tuple[np.ndarray, np.ndarray, list[str], int]:
    """Return (V_conv, V_dir, tribe_ids, n_ticks).

    V_conv: signed conviction, shape (n_ticks, n_tribes)
    V_dir:  signed direction  {-1, 0, +1}, shape (n_ticks, n_tribes)
    tribe_ids: stable order (sorted)
    n_ticks: number of ticks actually scanned (possibly multi-day total)
    """
    # Pass 1: discover tribe roster across all files.
    tribes: set[str] = set()
    for p in paths:
        for votes in _iter_tick_votes(p):
            for v in votes:
                tid = v.get("tribe_id")
                if tid:
                    tribes.add(tid)
    tribe_ids = sorted(tribes)
    idx = {t: i for i, t in enumerate(tribe_ids)}
    n_tribes = len(tribe_ids)

    # Pass 2: count ticks (to pre-allocate exactly).
    n_ticks = 0
    for p in paths:
        for _ in _iter_tick_votes(p):
            n_ticks += 1
    if n_ticks == 0:
        return np.zeros((0, n_tribes)), np.zeros((0, n_tribes)), tribe_ids, 0

    V_conv = np.zeros((n_ticks, n_tribes), dtype=np.float32)
    V_dir = np.zeros((n_ticks, n_tribes), dtype=np.float32)

    row = 0
    for p in paths:
        for votes in _iter_tick_votes(p):
            for v in votes:
                tid = v.get("tribe_id")
                if tid not in idx:
                    continue
                k = idx[tid]
                V_conv[row, k] = _signed_vote(v)
                V_dir[row, k] = _signed_dir(v)
            row += 1

    return V_conv, V_dir, tribe_ids, n_ticks


# -----------------------------------------------------------------------------
# Cosine similarity
# -----------------------------------------------------------------------------


def cos_sim_matrix(V: np.ndarray) -> np.ndarray:
    """Cosine similarity between columns of V. Shape (K, K).
    Zero-norm columns return zero similarity (no votes = cannot compare)."""
    # Column norms
    norms = np.linalg.norm(V, axis=0)
    # Avoid div-by-zero; anything with zero norm gets a zero row/col.
    safe = np.where(norms > 0, norms, 1.0)
    Vn = V / safe
    S = Vn.T @ Vn  # (K, K)
    # Zero out rows/cols where the original norm was zero.
    zero_mask = norms == 0
    if zero_mask.any():
        S[zero_mask, :] = 0.0
        S[:, zero_mask] = 0.0
    # Force diagonal = 1 where defined (avoid tiny float drift).
    diag = (~zero_mask).astype(np.float32)
    np.fill_diagonal(S, diag)
    return S


# -----------------------------------------------------------------------------
# PCA via SVD (column-centered)
# -----------------------------------------------------------------------------


def pca_spectrum(V: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (singular_values, var_ratio, cum_var_ratio) on column-centered V."""
    if V.shape[0] < 2:
        z = np.zeros(V.shape[1])
        return z, z, z
    Vc = V - V.mean(axis=0, keepdims=True)
    # Full SVD on the Gram-equivalent; for (T, K) with T >> K, use compact.
    _, s, _ = np.linalg.svd(Vc, full_matrices=False)
    # Eigenvalues of cov ~ s^2 / (T-1). Ratios are scale-invariant.
    eig = s**2
    total = eig.sum()
    if total <= 0:
        z = np.zeros_like(eig)
        return s, z, z
    ratio = eig / total
    cum = np.cumsum(ratio)
    return s, ratio, cum


def first_pc_loading(V: np.ndarray) -> np.ndarray:
    """Per-tribe loading on the first principal component, for seriation."""
    if V.shape[0] < 2:
        return np.zeros(V.shape[1])
    Vc = V - V.mean(axis=0, keepdims=True)
    # Right singular vectors = principal axes in column space.
    _, _, vt = np.linalg.svd(Vc, full_matrices=False)
    return vt[0]  # shape (K,)


# -----------------------------------------------------------------------------
# Seriation — re-order tribes so redundancy clusters are visible.
# -----------------------------------------------------------------------------


def cluster_order(V: np.ndarray, tribe_ids: list[str]) -> list[int]:
    """Return an index permutation that puts similar tribes adjacent.
    Seriation along PC1 loading — cheap, no scipy, good enough to show
    block structure in the heatmap."""
    pc1 = first_pc_loading(V)
    return list(np.argsort(pc1))


# -----------------------------------------------------------------------------
# Printing helpers
# -----------------------------------------------------------------------------


def _short(t: str, w: int = 12) -> str:
    return t if len(t) <= w else t[: w - 1] + "."


def _cell(v: float) -> str:
    # Compact cell: sign + 2 decimals, no space for neg sign takes it.
    if abs(v) >= 0.995:
        return f"{v:+.2f}"  # includes sign
    return f"{v:+.2f}"


def print_matrix(label: str, S: np.ndarray, tribe_ids: list[str], order: list[int]) -> None:
    print(f"\n-- {label} (seriated by PC1) --")
    labels = [tribe_ids[i] for i in order]
    short = [_short(t, 10) for t in labels]
    head = " " * 14 + "".join(f"{s:>7}" for s in short)
    print(head)
    for ri, i in enumerate(order):
        cells = "".join(f"{S[i, j]:>+7.2f}" for j in order)
        print(f"{short[ri]:<14}{cells}")


def print_heat_ascii(label: str, S: np.ndarray, tribe_ids: list[str], order: list[int]) -> None:
    """Pure-ASCII heatmap where cell glyph encodes |cos-sim| magnitude."""
    glyphs = [" ", ".", ":", "-", "+", "x", "#", "@"]  # low -> high magnitude
    print(f"\n-- {label} -- ASCII heat (| |: space=0 -> @=1) --")
    labels = [tribe_ids[i] for i in order]
    short = [_short(t, 10) for t in labels]
    head_cols = ["".join(s[k] if k < len(s) else " " for s in short) for k in range(10)]
    print(" " * 14 + " ".join([""] * 0))
    for k in range(10):
        print(" " * 13 + " " + head_cols[k])
    for ri, i in enumerate(order):
        row = ""
        for j in order:
            mag = min(1.0, abs(float(S[i, j])))
            g = glyphs[min(len(glyphs) - 1, int(mag * (len(glyphs) - 1) + 0.0001))]
            # Use different glyph for anti-correlation to distinguish direction.
            if S[i, j] < -0.3:
                g = g.upper() if g.isalpha() else g  # stays visual; sign printed in matrix view
            row += g + " "
        print(f"{short[ri]:<12} {row}")


# -----------------------------------------------------------------------------
# Main analysis pipeline
# -----------------------------------------------------------------------------


def analyze(paths: list[Path], redundancy_threshold: float, json_out: Path | None) -> dict:
    print(f"[ortho] loading {len(paths)} day file(s): {[p.name for p in paths]}")
    V_conv, V_dir, tribe_ids, n_ticks = build_vote_matrices(paths)
    if n_ticks == 0:
        print("[ortho] no tick data found in any of the supplied ledgers")
        return {}
    K = len(tribe_ids)
    print(f"[ortho] scanned {n_ticks:,} ticks across {K} tribes")

    # Activity rates
    activity = (V_dir != 0).mean(axis=0)  # fraction of ticks tribe voted
    print("\n-- activity rate (fraction of ticks with a non-abstain vote) --")
    for i, t in enumerate(tribe_ids):
        flag = "" if activity[i] >= 0.05 else "  <- rarely votes"
        print(f"  {t:<36}  {activity[i]*100:>5.1f}%{flag}")

    # Cosine similarity — both views
    S_conv = cos_sim_matrix(V_conv)
    S_dir = cos_sim_matrix(V_dir)

    # Seriation ordering from conviction matrix (richer signal)
    order = cluster_order(V_conv, tribe_ids)

    print_matrix("cosine similarity -- signed conviction", S_conv, tribe_ids, order)
    print_matrix("cosine similarity -- binarized direction", S_dir, tribe_ids, order)

    # Top |cos| pairs on conviction view
    pairs = []
    for i in range(K):
        for j in range(i + 1, K):
            pairs.append((float(S_conv[i, j]), tribe_ids[i], tribe_ids[j], float(S_dir[i, j])))
    pairs.sort(key=lambda p: -abs(p[0]))

    print(f"\n-- top 15 |cos-sim| pairs (redundancy threshold |cos| >= {redundancy_threshold:.2f}) --")
    print(f"{'conv':>7}  {'dir':>6}  {'tribe A':<34}  {'tribe B':<34}")
    for c, a, b, d in pairs[:15]:
        flag = "  <- REDUNDANT" if abs(c) >= redundancy_threshold else ""
        print(f"{c:>+7.2f}  {d:>+6.2f}  {a:<34}  {b:<34}{flag}")

    # Lone-wolves: tribes whose max |cos-sim| to any other tribe is below threshold.
    print(f"\n-- lone-wolves (max |cos-sim| to any other tribe < {LONE_WOLF_THRESHOLD:.2f}) --")
    lone_wolves: list[tuple[str, float]] = []
    for i in range(K):
        off_diag = np.delete(S_conv[i], i)
        top = float(np.max(np.abs(off_diag))) if off_diag.size else 0.0
        if top < LONE_WOLF_THRESHOLD:
            lone_wolves.append((tribe_ids[i], top))
    if lone_wolves:
        for t, top in sorted(lone_wolves, key=lambda x: x[1]):
            print(f"  {t:<36}  max |cos| = {top:.2f}")
    else:
        print("  (none -- every tribe has at least one strong neighbor)")

    # PCA scree
    s, ratio, cum = pca_spectrum(V_conv)
    print("\n-- PCA scree (column-centered signed-conviction matrix) --")
    print(f"{'PC':>4}  {'sv':>10}  {'var %':>7}  {'cum %':>7}")
    for i in range(len(ratio)):
        print(f"  {i+1:>2}  {s[i]:>10.2f}  {ratio[i]*100:>6.2f}  {cum[i]*100:>6.2f}")
    # Effective rank at 95%
    idx95 = int(np.searchsorted(cum, 0.95)) + 1
    idx90 = int(np.searchsorted(cum, 0.90)) + 1
    idx80 = int(np.searchsorted(cum, 0.80)) + 1
    idx50 = int(np.searchsorted(cum, 0.50)) + 1
    print(f"\n[ortho] effective rank: 50%={idx50}  80%={idx80}  90%={idx90}  95%={idx95}  (nominal K={K})")

    # Redundancy count
    redundant_pairs = [(c, a, b, d) for c, a, b, d in pairs if abs(c) >= redundancy_threshold]
    anti_pairs = [(c, a, b, d) for c, a, b, d in pairs if c <= -redundancy_threshold]

    print()
    if redundant_pairs:
        print(f"[ortho] {len(redundant_pairs)} redundant pair(s) above |cos| >= {redundancy_threshold:.2f}")
    else:
        print("[ortho] no redundant pairs at this threshold")
    if anti_pairs:
        print(f"[ortho] {len(anti_pairs)} anti-correlated pair(s) (cos <= -{redundancy_threshold:.2f}) -- expected, council doing its job")

    # Machine-readable dump
    result = {
        "n_ticks": int(n_ticks),
        "tribes": tribe_ids,
        "activity_rate": {t: float(activity[i]) for i, t in enumerate(tribe_ids)},
        "cos_sim_conviction": S_conv.tolist(),
        "cos_sim_direction": S_dir.tolist(),
        "serialized_order": [tribe_ids[i] for i in order],
        "top_pairs": [
            {"conv": c, "dir": d, "a": a, "b": b} for c, a, b, d in pairs[:30]
        ],
        "lone_wolves": [{"tribe": t, "max_abs_cos": top} for t, top in lone_wolves],
        "pca_variance_ratio": ratio.tolist(),
        "pca_cumulative": cum.tolist(),
        "effective_rank": {"50%": idx50, "80%": idx80, "90%": idx90, "95%": idx95, "nominal": K},
        "redundancy_threshold": redundancy_threshold,
        "n_redundant_pairs": len(redundant_pairs),
    }

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(result, indent=2))
        print(f"\n[ortho] dumped full result to {json_out}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Council-orthogonality analyzer (Phase G)")
    parser.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD (UTC). Default = today.")
    parser.add_argument("--days", nargs="+", default=None,
                        help="Multiple days to concatenate, e.g. 2026-04-18 2026-04-19 2026-04-20")
    parser.add_argument("--threshold", type=float, default=DEFAULT_REDUNDANCY)
    parser.add_argument("--json", type=str, default=None, help="Optional JSON output path")
    parser.add_argument("--path", default=None, help="Explicit ledger file path")
    args = parser.parse_args()

    if args.path:
        paths = [Path(args.path)]
    elif args.days:
        paths = [LEDGER_DIR / f"{d}.jsonl" for d in args.days]
    else:
        day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        paths = [LEDGER_DIR / f"{day}.jsonl"]

    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"[ortho] missing ledger file(s): {[str(m) for m in missing]}", file=sys.stderr)
        sys.exit(1)

    json_out = Path(args.json) if args.json else None
    analyze(paths, redundancy_threshold=args.threshold, json_out=json_out)


if __name__ == "__main__":
    main()
