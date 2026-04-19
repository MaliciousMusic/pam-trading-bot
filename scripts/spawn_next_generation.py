"""Spawn the next evolutionary generation from a completed week's results.

Reads a `week_manifest.json`, ranks variants by composite fitness, picks
the top-K parents, and writes N child variant directories under
`config/tribes_variants/<new_gen_id>/` (each with a `lineage.yaml`).
The seed bank `variant_0` is *always* a verbatim copy of `config/tribes/`.

Typical pipeline:

    # 1) Last week's experiment finished:
    python -m scripts.run_week --start 2026-04-13 --days 7 \
        --tribes-dir config/tribes_variants/gen_3/

    # 2) Spawn generation 4 from gen_3's top-2 survivors:
    python -m scripts.spawn_next_generation \
        --week-manifest data/replays/week_2026-04-13_*/week_manifest.json \
        --new-generation gen_4 \
        --top-k 2 \
        --children 6

    # 3) Run the next week with the new generation:
    python -m scripts.run_week --start 2026-04-20 --days 7 \
        --tribes-dir config/tribes_variants/gen_4/

The seed bank baseline (`variant_0`) is *not* counted toward `--top-k` —
it's already added unconditionally as the immutable reference. The
`--children` count produces evolved siblings *in addition* to the seed
bank, so the directory ends up with `1 + children` variants.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import orjson
from loguru import logger

from src.core.config import CONFIG_DIR, PROJECT_ROOT
from src.experiments import (
    SEED_BANK_VARIANT_ID,
    cull_bottom_half,
    fitness_from_ledger,
    rank_variants,
    spawn_generation,
)


def _resolve_week_root(week_manifest: Path) -> Path:
    return week_manifest.parent


def _collect_fitness(week_manifest: Path) -> list:
    """Walk a week_manifest.json's days/variants tree and score each
    variant by aggregating its per-day ledgers + manifests.

    The fitness of a variant across the week uses the *first day's*
    starting equity and the *last day's* ending equity for the directional
    sign, plus the union of all per-trade close events for Sharpe + MDD.
    Equity carry-over is already encoded in the per-day manifests, so the
    cumulative curve is well-defined.
    """
    week_root = _resolve_week_root(week_manifest)
    raw = orjson.loads(week_manifest.read_bytes())
    variants = [v["variant_id"] for v in raw["variants"]]
    out = []
    for variant_id in variants:
        first_day = None
        last_day = None
        merged_ledgers: list[Path] = []
        for d in raw["days"]:
            run_id = d["run_id"]
            run_path = week_root.parent / run_id  # week_root/day_X
            # Fallback if path layout differs (week_root contains day dirs)
            if not run_path.exists():
                run_path = week_root / Path(run_id).name
            v_dir = run_path / "variants" / variant_id
            manifest = v_dir / "manifest.json"
            if not manifest.exists():
                continue
            m = orjson.loads(manifest.read_bytes())
            if first_day is None:
                first_day = m
            last_day = m
            ldir = v_dir / "ledger"
            if ldir.exists():
                merged_ledgers.append(ldir)
        if first_day is None or last_day is None:
            continue
        # Use a temp pseudo-dir trick: compute fitness per-ledger and sum?
        # Simpler: build one synthetic ledger view by concatenating files.
        # The selection.fitness_from_ledger reads `*.jsonl` under one dir,
        # so just point it at each day's dir in turn and aggregate manually.
        from src.experiments.selection import _iter_close_events, VariantFitness
        import math
        pnls: list[float] = []
        cum: list[float] = []
        running = 0.0
        for ldir in merged_ledgers:
            for ev in _iter_close_events(ldir):
                try:
                    net = float(ev.get("net_usd", 0.0))
                except (TypeError, ValueError):
                    net = 0.0
                pnls.append(net)
                running += net
                cum.append(running)
        start_eq = float(first_day.get("start_equity_usd", 0.0))
        end_eq = float(last_day.get("end_equity_usd", 0.0))
        n = len(pnls)
        delta = end_eq - start_eq
        if n < 2:
            out.append(VariantFitness(
                variant_id=variant_id, fitness=0.0, sharpe=0.0,
                equity_delta_usd=delta, max_drawdown_frac=0.0, trades=n,
            ))
            continue
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = (mean / sd) * math.sqrt(n) if sd > 0 else 0.0
        # MDD on cumulative PnL relative to start equity
        peak = start_eq
        max_dd = 0.0
        for c in cum:
            eq = start_eq + c
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        sign = 1.0 if delta >= 0 else -1.0
        fitness = sharpe * sign * (1.0 - max_dd)
        out.append(VariantFitness(
            variant_id=variant_id, fitness=fitness, sharpe=sharpe,
            equity_delta_usd=delta, max_drawdown_frac=max_dd, trades=n,
        ))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--week-manifest", required=True, type=Path,
                    help="path to week_manifest.json from the prior week")
    ap.add_argument("--new-generation", required=True, type=str,
                    help="generation tag to write under config/tribes_variants/")
    ap.add_argument("--parent-generation-dir", default=None, type=Path,
                    help="parent generation root; defaults to looking up the "
                         "tribes_dir from each variant's manifest")
    ap.add_argument("--baseline-dir", default=None, type=Path,
                    help="directory used as the seed-bank baseline; defaults "
                         "to the project's config/tribes/")
    ap.add_argument("--top-k", default=2, type=int,
                    help="how many top variants become parents (default 2)")
    ap.add_argument("--children", default=6, type=int,
                    help="number of evolved children to spawn (default 6)")
    ap.add_argument("--sigma", default=0.05, type=float,
                    help="Gaussian mutation sigma per field (default 0.05)")
    ap.add_argument("--fields-per-tribe", default=2, type=int,
                    help="number of params to perturb per tribe (default 2)")
    ap.add_argument("--rng-seed", default=None, type=int,
                    help="optional seed for reproducible mutation draws")
    args = ap.parse_args()

    if not args.week_manifest.exists():
        logger.error("no week manifest at {}", args.week_manifest)
        sys.exit(1)

    fitness = _collect_fitness(args.week_manifest)
    if not fitness:
        logger.error("no variant fitness collected from {}", args.week_manifest)
        sys.exit(1)

    ranked = rank_variants(fitness)
    print()
    print(f"  Week: {args.week_manifest.parent.name}")
    print(f"  Variants scored: {len(ranked)}")
    print()
    print(f"  {'rank':<5} {'variant':<24} {'fitness':>10} {'sharpe':>8} "
          f"{'Δeq':>10} {'MDD':>6} {'trades':>7}")
    for i, vf in enumerate(ranked, 1):
        marker = " *" if vf.variant_id == SEED_BANK_VARIANT_ID else "  "
        print(f"  {i:<5} {marker}{vf.variant_id:<22} "
              f"{vf.fitness:>10.3f} {vf.sharpe:>8.2f} "
              f"{vf.equity_delta_usd:>+10.2f} {vf.max_drawdown_frac:>6.1%} "
              f"{vf.trades:>7}")

    parents = cull_bottom_half(
        [vf for vf in ranked if vf.variant_id != SEED_BANK_VARIANT_ID],
        keep_fraction=1.0,  # we manually slice below
    )[: args.top_k]
    if not parents:
        logger.error("no parents survive the cull — check fitness inputs")
        sys.exit(1)

    # Resolve each parent's source directory.
    raw = orjson.loads(args.week_manifest.read_bytes())
    week_root = _resolve_week_root(args.week_manifest)
    parent_dirs: list[Path] = []
    for vf in parents:
        # Look up tribes_dir via the first day's per-variant manifest.
        for d in raw["days"]:
            run_id = d["run_id"]
            run_path = week_root.parent / run_id
            if not run_path.exists():
                run_path = week_root / Path(run_id).name
            mp = run_path / "variants" / vf.variant_id / "manifest.json"
            if mp.exists():
                m = orjson.loads(mp.read_bytes())
                td = m.get("tribes_dir")
                if td and Path(td).exists():
                    parent_dirs.append(Path(td))
                    break
        else:
            logger.warning("could not resolve tribes_dir for parent {}", vf.variant_id)

    if not parent_dirs:
        if args.parent_generation_dir is None:
            logger.error(
                "no parent tribes_dirs resolved from manifests; "
                "pass --parent-generation-dir as a fallback"
            )
            sys.exit(1)
        parent_dirs = [args.parent_generation_dir / vf.variant_id for vf in parents]

    baseline = args.baseline_dir or (CONFIG_DIR / "tribes")
    if not baseline.exists():
        logger.error("baseline dir not found: {}", baseline)
        sys.exit(1)

    new_root = CONFIG_DIR / "tribes_variants" / args.new_generation
    # generation index = previous max + 1 (best effort: count existing dirs)
    try:
        existing = sorted(
            int(p.name.split("_")[-1])
            for p in (CONFIG_DIR / "tribes_variants").iterdir()
            if p.is_dir() and p.name.split("_")[-1].isdigit()
        )
        gen_idx = (existing[-1] + 1) if existing else 1
    except Exception:
        gen_idx = 1

    print()
    print(f"  Spawning generation idx={gen_idx} → {new_root}")
    print(f"  Parents (top-{args.top_k}): {[p.name for p in parent_dirs]}")
    print(f"  Baseline (seed bank): {baseline}")
    print(f"  Children: {args.children}, sigma: {args.sigma}, "
          f"fields/tribe: {args.fields_per_tribe}")
    print()

    results = spawn_generation(
        generation_root=new_root,
        seed_bank_dir=baseline,
        parent_dirs=parent_dirs,
        n_children=args.children,
        sigma=args.sigma,
        rng_seed=args.rng_seed,
        generation_index=gen_idx,
        parent_run_id=raw.get("week_id"),
        fields_per_tribe=args.fields_per_tribe,
    )

    print(f"  Wrote seed bank + {len(results)} children:")
    for r in results:
        print(f"    {r.variant_id}  parent={r.parent_variant_id}  "
              f"mutated={len(r.mutated_fields)}")
    print()
    print(f"  Next step:")
    print(f"    python -m scripts.run_week --start <DATE> --days 7 \\")
    print(f"        --tribes-dir {new_root.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
