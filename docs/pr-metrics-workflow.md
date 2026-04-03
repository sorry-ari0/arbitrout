# PR → metrics loop

Tie merged work to **measurable** paper-trading outcomes using the trade journal (`src/data/positions/trade_journal_paper.json`).

## Snapshot tool

```bash
cd src   # repo root also works
python tools/metrics_snapshot.py
```

- Prints a JSON document: totals, win rate, **gross before fees**, **total fees**, **fee/gross ratio**, and per-`strategy_type` breakdown.
- Optional: `--write tools/metrics_baseline.json` to save the current journal as the approved reference.
- Optional: `--compare tools/metrics_baseline.json` exits **1** if guarded metrics regress (see `tools/metrics_snapshot.py` tolerances).

## When to run

| Moment | Action |
|--------|--------|
| Before opening a PR that touches execution, exits, fees, or auto-trader filters | Run snapshot; paste **total_pnl_usd**, **fee_to_gross_ratio**, and **by_strategy** deltas into the PR description. |
| After merging a behavior-changing PR | Run paper for a defined window (e.g. 48h or N closes), then snapshot again. |
| Intentional baseline reset | When metrics **improve**, run `--write tools/metrics_baseline.json` and commit so CI/manual compare expects the new bar. |

## What “good” looks like (directional)

- **fee_to_gross_ratio** should fall toward **&lt; 1** (fees should not exceed aggregate gross P&amp;L before fees). Values **&gt; 1** mean friction dominates edge.
- **pure_prediction**: watch **fees** vs **gross**; large ratio means exits/entries are still too expensive relative to captured move.
- **news_driven** / **portfolio_no**: positive **pnl** and stable win rate are guardrails for structural flows.
- **cross_platform_arb**: win rate and P&amp;L should improve after matcher/executor PRs; **0% wins** with negative gross means **prices are wrong or events are not equivalent**, not just “bad luck.”

## CI (optional)

Add a job that runs `python tools/metrics_snapshot.py --compare tools/metrics_baseline.json` only when `trade_journal_paper.json` changes on a branch, or run it manually before release — automated compare is noisy if the journal is shared/dev-specific. For team use, prefer a **dedicated metrics journal** or exported CSV from prod-like paper.
