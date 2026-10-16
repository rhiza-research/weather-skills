# kenya-forecast-fetch examples

Each pipeline fetches one product, turns rates into weekly totals, and plots
them. Scripts of other skills are run from the repository root.

## KMSA weekly precip product figure (native S2S)

The archive figure `weekly_precip.png` shows weekly totals on the Kenya
product extent `7/32/-6/43` with plot's default precip palette (omit
`theme.colormap`).

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip \
    --date 2026-08-18 -v tp -o /tmp/kenya_tp.zarr

uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/kenya_tp.zarr -o /tmp/kenya_weekly.zarr --period weekly

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_weekly.zarr -o /tmp/kenya_weekly_mm.zarr

uv run skills/plot/scripts/plot.py -i /tmp/kenya_weekly_mm.zarr -o /tmp/kenya_weekly.png \
    --spec '{"inputs":[{"variable":"tp"}],"geo":{"bbox":[7,32,-6,43]}}'
```

## Weekly downscale (~0.05°)

Already weekly with `aggregation_period` stamped, so it goes straight to totals.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip_downscaled \
    --date 2026-08-30 -o /tmp/kenya_tp_ds.zarr

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_tp_ds.zarr -o /tmp/kenya_tp_ds_mm.zarr

uv run skills/plot/scripts/plot.py -i /tmp/kenya_tp_ds_mm.zarr -o /tmp/kenya_weekly_downscaled.png \
    --spec '{"inputs":[{"variable":"tp"}]}'
```

## Daily downscale (~0.05°, ensemble mean)

Daily rates, so aggregate to weeks first.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip_downscaled_daily \
    --date 2026-09-16 -o /tmp/kenya_tp_ds_daily.zarr

uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/kenya_tp_ds_daily.zarr -o /tmp/kenya_tp_ds_daily_weekly.zarr \
    --period weekly

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_tp_ds_daily_weekly.zarr -o /tmp/kenya_tp_ds_daily_weekly_mm.zarr

uv run skills/plot/scripts/plot.py -i /tmp/kenya_tp_ds_daily_weekly_mm.zarr \
    -o /tmp/kenya_daily_downscaled.png \
    --spec '{"inputs":[{"variable":"tp"}]}'
```
