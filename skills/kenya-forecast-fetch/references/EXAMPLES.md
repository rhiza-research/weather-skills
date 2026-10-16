# kenya-forecast-fetch examples

Each pipeline fetches one product and turns its rates into weekly totals.
Scripts of other skills are run from the repository root.

## Weekly totals (native S2S)

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip \
    --date 2026-08-18 -v tp -o /tmp/kenya_tp.zarr

uv run skills/aggregate-temporal/scripts/aggregate.py \
    -i /tmp/kenya_tp.zarr -o /tmp/kenya_weekly.zarr --period weekly

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_weekly.zarr -o /tmp/kenya_weekly_mm.zarr
```

## Weekly downscale (~0.05°)

Already weekly with `aggregation_period` stamped, so it goes straight to totals.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --dataset precip_downscaled \
    --date 2026-08-30 -o /tmp/kenya_tp_ds.zarr

uv run skills/convert-to-totals/scripts/convert_to_totals.py \
    -i /tmp/kenya_tp_ds.zarr -o /tmp/kenya_tp_ds_mm.zarr
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
```
