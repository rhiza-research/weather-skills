#!/bin/bash
# Final fits on all 20 training seasons, then the 2026 blends.
set -euo pipefail
cd "$WORK"
uv run -q "$SKILLS/mme-fit/scripts/mme_fit.py" --forecast ecmwf_hind.zarr --forecast gefs_hind.zarr --names ecmwf,gefs \
  --obs obs_event.zarr --model logistic --fit-along step -o w_models.zarr
uv run -q "$SKILLS/mme-fit/scripts/mme_fit.py" --forecast ecmwf_hind.zarr --forecast gefs_hind.zarr --forecast clim_hind.zarr \
  --names ecmwf,gefs,clim --obs obs_event.zarr --model logistic --fit-along step -o w_models_clim.zarr
uv run -q "$SKILLS/mme-blend/scripts/mme_blend.py" --forecast f2026/ecmwf_wk_p.zarr --forecast f2026/gefs_wk.zarr \
  --names ecmwf,gefs --weights w_models.zarr -o p2026_models.zarr
uv run -q "$SKILLS/mme-blend/scripts/mme_blend.py" --forecast f2026/ecmwf_wk_p.zarr --forecast f2026/gefs_wk.zarr \
  --forecast clim_2026_analog.zarr --names ecmwf,gefs,clim --weights w_models_clim.zarr -o p2026_models_analog.zarr
