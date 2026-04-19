"""Historical replay harness for the PAM council.

Swaps the live Binance feed for cached 1m klines + funding history,
feeding the same decision pipeline unchanged. Two modes live here:

  - sprint: headless batch run, JSONL decision log + rich HTML report
  - watch:  paced virtual-clock replay with live UI and controls

The cold-start stance is intentional. No warm-up, no HTF pre-fill: the
report visualizes how tribes stabilize (or not) as context accumulates.
"""
