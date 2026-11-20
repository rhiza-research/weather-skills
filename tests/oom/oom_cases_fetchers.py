"""Fetcher cases for the OOM suite: every gridded fetcher, small and large.

Station/API fetchers come later: they can be big too, but start with grids.

``large_args`` is sized so the OUTPUT is about 512 MiB - 1 GiB uncompressed
(cells x times x vars x itemsize, checked against the source's grid and dtype
where the store is public). ``small_args`` is the same request shape (same flags,
a smaller window or box) at about 1/8 of that, so the small run already fills the
dask pipeline: peak memory then ramps up only with the data, not with fixed
per-source costs. Each ``note`` gives the arithmetic. Where the source is chunked by
time, the large window is one whole time chunk and the bbox is sized to fit,
so the download is about the size of the output.

Sources are read in whole stored chunks, so a chunk bigger than 1/8 of the output
(CMIP6: 93 MiB) cannot be saturated by a request that small: those cases take the
smallest request that spans several chunks while the large one is still 5x bigger
(the scaling tier insists on that).
"""

from __future__ import annotations

from oom_harness import FetchCase

TINY_BBOX = "1/36/-1/38"  # a 2 deg box on the equator in Kenya, inside every grid here

CASES = [
    FetchCase(
        "arco-era5-fetch",
        "arco-era5-fetch",
        large_args=(
            "--start-time",
            "2025-01-01",
            "--end-time",
            "2025-01-02",
            "-v",
            "2m_temperature",
            "-v",
            "total_precipitation",
            "-v",
            "10m_u_component_of_wind",
        ),
        small_args=(
            "--start-time",
            "2025-01-01",
            "--end-time",
            "2025-01-01",
            "--bbox",
            "90/-180/-90/-90",
            "-v",
            "2m_temperature",
            "-v",
            "total_precipitation",
            "-v",
            "10m_u_component_of_wind",
        ),
        note="0.25 deg global, 721 x 1440 float32 in the store (checked), one chunk per hour "
        "per field, so the whole globe is read anyway. The skill writes precip as float64 "
        "(the rate conversion upcasts; seen in the small run), the rest float32: 48 h x "
        "1.04M cells x (4 + 8 + 4) B = 797 MB (760 MiB)."
        " Small: one day over a quarter of the globe (361 columns), same three fields: 24 h x "
        "721 x 361 x 16 B = 100 MB (95 MiB), 1/8 of the large (the hourly chunks are global, "
        "so half its download).",
    ),
    FetchCase(
        "cams-fetch-eac4",
        "cams-fetch",
        large_args=(
            "--dataset",
            "eac4",
            "--start-time",
            "2023-01-01",
            "--end-time",
            "2023-01-16",
            "--bbox",
            "90/-180/-90/179.25",
            *("-v", "pm25", "-v", "pm10", "-v", "pm1", "-v", "no2", "-v", "o3", "-v", "so2"),
            *("-v", "co", "-v", "aod550", "-v", "duaod550", "-v", "tcno2", "-v", "tco3"),
            *("-v", "tcco", "-v", "tcso2"),
        ),
        small_args=(
            "--dataset",
            "eac4",
            "--start-time",
            "2023-01-01",
            "--end-time",
            "2023-01-02",
            "--bbox",
            "90/-180/-90/179.25",
            "-v",
            "pm25",
            "-v",
            "pm10",
            "-v",
            "pm1",
            "-v",
            "no2",
            "-v",
            "o3",
            "-v",
            "so2",
            "-v",
            "co",
            "-v",
            "aod550",
            "-v",
            "duaod550",
            "-v",
            "tcno2",
            "-v",
            "tco3",
            "-v",
            "tcco",
            "-v",
            "tcso2",
        ),
        creds=("ADS_API_KEY|~/.cdsapirc",),
        note="EAC4 is 0.75 deg, 3-hourly; the box stops at 179.25 so the -180/180 seam is "
        "not duplicated: 241 x 480 cells x 128 times x 13 fields x 4 B (cast to float32) "
        "= 770 MB (734 MiB), two ADS jobs (surface and gas groups, one month). The "
        "skill also reads a ~/.cdsapirc whose url is the ADS."
        " Small: two days, same box and fields: 241 x 480 x 16 x 13 x 4 B = 96 MB (92 MiB), "
        "exactly 1/8.",
    ),
    FetchCase(
        "chirps-fetch",
        "chirps-fetch",
        large_args=(
            "--start-time",
            "2022-12-22",
            "--end-time",
            "2023-12-21",
            "--bbox",
            "21/24/-18/60",
        ),
        small_args=(
            "--start-time",
            "2022-12-22",
            "--end-time",
            "2023-12-21",
            "--bbox",
            "9/24/-3/39",
        ),
        note="CHIRPS is 0.05 deg float32 in 365-day x 60x60-cell (3 deg) chunks counted from "
        "1981-01-01 (checked), so the window is exactly chunk 42 (2022-12-22..2023-12-21, "
        "final product) and the box is on 3 deg chunk edges (East Africa and Arabia): "
        "780 x 720 cells x 365 days x 4 B = 820 MB (782 MiB). A shorter window would "
        "still download the whole year over the box."
        " Small: the same year over a 12 x 15 deg box (also on 3 deg edges): 240 x 300 x 365 x "
        "4 B = 105 MB (100 MiB), 1/7.8; it reads 4 x 5 stored chunks, each the whole year.",
    ),
    FetchCase(
        "clim-fetch",
        "clim-fetch",
        large_args=(
            "--dataset",
            "chirps",
            "--start-time",
            "2024-01-01",
            "--end-time",
            "2024-12-31",
            "--bbox",
            "30/30/-15/45",
        ),
        small_args=(
            "--dataset",
            "chirps",
            "--start-time",
            "2024-01-01",
            "--end-time",
            "2024-02-15",
            "--bbox",
            "30/30/-15/45",
        ),
        note="CHIRPS climatology mirror is 0.05 deg float32 in 300x300-cell (15 deg) x "
        "365-day chunks (checked); the box is on 15 deg edges and the leap year uses every "
        "day-of-year row once: 900 x 300 cells x 366 days x 2 vars (_avg, _std) x 4 B "
        "= 791 MB (754 MiB)."
        " Small: the same box for 46 days: 900 x 300 x 46 x 2 x 4 B = 99 MB (95 MiB), 1/8 (it "
        "reads the same six stored chunks as the large).",
    ),
    FetchCase(
        "cmip6-fetch",
        "cmip6-fetch",
        large_args=(
            "--model",
            "CNRM-CM6-1-HR",
            "--experiment",
            "historical",
            "-v",
            "tas",
            "--member",
            "r1i1p1f2",
            "--table",
            "day",
            "--grid",
            "gr",
            "--start-time",
            "2013-01-01",
            "--end-time",
            "2014-12-31",
        ),
        small_args=(
            "--model",
            "CNRM-CM6-1-HR",
            "--experiment",
            "historical",
            "-v",
            "tas",
            "--member",
            "r1i1p1f2",
            "--table",
            "day",
            "--grid",
            "gr",
            "--start-time",
            "2014-09-01",
            "--end-time",
            "2014-12-31",
        ),
        note="CNRM-CM6-1-HR is the finest daily grid in the Pangeo catalog: 360 x 720 "
        "float32, chunked 94 days x whole globe, gregorian calendar (checked). 730 days x "
        "259,200 cells x 4 B = 757 MB (722 MiB). One variable per call, so only time can "
        "grow once the box is global."
        " Small: 122 days, global: 126 MB (121 MiB), 1/6 of the large. A stored chunk is 93 "
        "MiB, so 1/8 would touch one chunk and not fill the pipeline; 122 days span three, "
        "and the scaling tier needs the large request to be 5x the small one.",
    ),
    FetchCase(
        "cumulus-fetch",
        "cumulus-fetch",
        large_args=("--dataset", "precip", "--bbox", "40/-20/-40/80"),
        small_args=("--dataset", "precip", "--bbox", "40/-20/0/5"),
        creds=("AZURE_STORAGE_SAS_TOKEN",),
        note="Latest init (live stores only, so no fixed date). 0.25 deg, 30 members x 46 "
        "daily leads, float32: 321 x 401 cells x 30 x 46 x 4 B = 711 MB (678 MiB). Not "
        "checked against the store (private); sizes from the SKILL."
        " Small: 161 x 101 cells x 30 x 46 x 4 B = 90 MB (86 MiB), 1/7.9.",
    ),
    FetchCase(
        "dynamical-fetch-analysis",
        "dynamical-fetch",
        large_args=(
            "--dataset",
            "noaa-gfs-analysis",
            "--start-time",
            "2025-04-10",
            "--end-time",
            "2025-06-08",
            "--bbox",
            "40/-20/-40/80",
            "-v",
            "precip",
        ),
        small_args=(
            "--dataset",
            "noaa-gfs-analysis",
            "--start-time",
            "2025-04-10",
            "--end-time",
            "2025-04-17",
            "--bbox",
            "40/-20/-40/80",
            "-v",
            "precip",
        ),
        note="GFS analysis is hourly 0.25 deg float32 in 1440-h (60-day) x 50x50 chunks from "
        "2021-05-01 (checked), so the window is exactly one time chunk "
        "(2025-04-10..2025-06-08): 321 x 401 cells x 1440 h x 4 B = 741 MB (707 MiB)."
        " Small: eight days of the same chunk and box: 321 x 401 x 192 h x 4 B = 99 MB (94 "
        "MiB), 1/7.5.",
    ),
    FetchCase(
        "dynamical-fetch-forecast",
        "dynamical-fetch",
        large_args=(
            "--dataset",
            "ecmwf-ifs-ens-forecast-15-day-0-25-degree",
            "--date",
            "2025-06-01",
            "--bbox",
            "30/10/-30/60",
            "-v",
            "precip",
        ),
        small_args=(
            "--dataset",
            "ecmwf-ifs-ens-forecast-15-day-0-25-degree",
            "--date",
            "2025-06-01",
            "--bbox",
            "30/10/0/22.5",
            "-v",
            "precip",
        ),
        note="Forecast path (lead_time -> step, members, uneven 3h/6h leads with step_bounds, "
        "lead-0 drop for precip). IFS ENS 0.25 deg: 51 members x 85 leads (84 after the "
        "lead-0 drop) float32, chunked 1 init x all leads x all members x 32x32 cells "
        "(checked): 241 x 201 cells x 51 x 84 x 4 B = 830 MB (792 MiB)."
        " Small: the same init over a 30 x 12.5 deg box: 121 x 51 x 51 x 84 x 4 B = 106 MB "
        "(101 MiB), 1/7.8.",
    ),
    FetchCase(
        "ecmwf-fetch-catalog",
        "ecmwf-fetch",
        large_args=("--date", "2026-03-01", "--bbox", "60/-180/-60/180", "-v", "tp,t2m"),
        small_args=("--date", "2026-03-01", "--bbox", "60/-180/-60/-135", "-v", "tp,t2m"),
        note="Inits from 2026 come from dynamical.org's 46-day daily 1.5 deg store: 121 x 240 "
        "global, 101 members, 47 leads (46 after the lead-0 drop; tp and t2m are both "
        "daily means), float32 (checked). One init per call, so box and variables are the "
        "levers: 81 x 240 cells x 46 x 101 x 2 vars x 4 B = 723 MB (689 MiB)."
        " Small: the same two fields over 45 deg of longitude: 81 x 31 x 46 x 101 x 2 x 4 B = "
        "93 MB (89 MiB), 1/7.7.",
    ),
    FetchCase(
        "ecmwf-fetch-ecds",
        "ecmwf-fetch",
        large_args=("--date", "2025-03-03", "--bbox", "60/-180/-60/180", "-v", "tp,t2m"),
        small_args=("--date", "2025-03-03", "--bbox", "60/-180/-60/-135", "-v", "tp,t2m"),
        creds=("ECMWF_DATASTORES_KEY",),
        note="Pre-2026 inits go to the ECMWF Data Store (control and perturbed GRIB). Same "
        "1.5 deg grid, 101 members and 46 daily steps as the catalog case: 81 x 240 x 46 x "
        "101 x 2 x 4 B = 723 MB (689 MiB). ECDS queues can take an hour."
        " Small: the same two fields over 45 deg of longitude: 81 x 31 x 46 x 101 x 2 x 4 B = "
        "93 MB (89 MiB), 1/7.7.",
    ),
    FetchCase(
        "ecmwf-hres-fetch",
        "ecmwf-hres-fetch",
        large_args=(
            "--date",
            "2025-06-01",
            "--run",
            "0",
            "--bbox",
            "90/-180/-90/180",
            "-v",
            "t2m",
            "-v",
            "msl",
        ),
        small_args=(
            "--date",
            "2025-06-01",
            "--run",
            "0",
            "--bbox",
            "90/-180/-90/180",
            "-v",
            "t2m",
            "-v",
            "msl",
            "--max-lead",
            "27h",
        ),
        note="Open Data is global GRIB at 0.25 deg (721 x 1440, float32 via cfgrib); a 00 UTC "
        "run has 85 steps (0-144 h by 3, 150-360 h by 6). Two instant fields (no tp, so no "
        "step is dropped): 2 x 85 x 1.04M cells x 4 B = 706 MB (673 MiB), ~160 MB of GRIB."
        " Small: the first ten steps (0-27 h), same global fields: 2 x 10 x 1.04M x 4 B = 83 "
        "MB (79 MiB), 1/8.5.",
    ),
    FetchCase(
        "gsmap-fetch",
        "gsmap-fetch",
        large_args=(
            "--start-time",
            "2025-07-01",
            "--end-time",
            "2025-07-02",
            "--bbox",
            "60/-180/-60/180",
        ),
        small_args=(
            "--start-time",
            "2025-07-01",
            "--end-time",
            "2025-07-01",
            "--bbox",
            "60/-180/-60/-90",
        ),
        note="0.1 deg hourly, 60N-60S, float32. The SKILL says at most 22 images a day, but "
        "2025-07-01 served all 24 (small run): 1200 x 3600 cells x 24 h x 2 days x 4 B = "
        "829 MB (791 MiB), or 760 MB with 22. The whole band is the widest box the source "
        "allows."
        " Small: one day over a quarter of the band: 1200 x 900 x 24 h x 4 B = 104 MB (99 "
        "MiB), 1/8.",
    ),
    FetchCase(
        "imerg-fetch",
        "imerg-fetch",
        large_args=("--version", "final", "--start-time", "2025-01-01", "--end-time", "2025-01-30"),
        small_args=("--version", "final", "--start-time", "2025-01-01", "--end-time", "2025-01-04"),
        creds=("EARTHDATA_USERNAME+EARTHDATA_PASSWORD|~/.netrc",),
        note="IMERG Final daily is 1800 x 3600 (0.1 deg global) float32, one ~32 MB global "
        "file per day: 30 days x 6.48M cells x 4 B = 778 MB (742 MiB). Final V07 ends "
        "2025-09-30. The skill also logs in from a urs.earthdata.nasa.gov ~/.netrc entry."
        " Small: four global days: 4 x 6.48M x 4 B = 104 MB (99 MiB), 1/7.5.",
    ),
    FetchCase(
        "meteosat-fetch",
        "meteosat-fetch",
        large_args=(
            "--date",
            "2025-03-20",
            "--time",
            "11:00",
            "--time",
            "12:00",
            "--time",
            "13:00",
            "--bbox",
            "5/10/-5/50",
            "-v",
            "vis,ir",
        ),
        small_args=(
            "--date",
            "2025-03-20",
            "--time",
            "12:00",
            "--bbox",
            "5/10/-5/25",
            "-v",
            "vis,ir",
        ),
        creds=("EUMETSAT_CONSUMER_KEY", "EUMETSAT_CONSUMER_SECRET"),
        note="FCI, all 16 bands resampled to the 0.01 deg visible grid: 1000 x 4000 cells x "
        "16 bands x 3 daylight scans x 4 B (float32) = 768 MB (732 MiB). Bands and box "
        "width are free (every file holds all channels; strips are west-east), so the box "
        "is wide and short: ~250 MB downloaded per scan."
        " Small: one scan over 15 deg of longitude: 1000 x 1500 x 16 x 4 B = 96 MB (92 MiB), "
        "1/8.",
    ),
    FetchCase(
        "neuralgcm-fetch",
        "neuralgcm-fetch",
        large_args=(
            "--dataset",
            "era5:surface",
            "--start-time",
            "2026-09-01",
            "--end-time",
            "2026-09-01",
        ),
        small_args=(
            "--dataset",
            "era5:surface",
            "--start-time",
            "2026-09-01",
            "--end-time",
            "2026-09-01",
            "--bbox",
            "90/-180/0/-90",
        ),
        creds=("GCLOUD_SERVICE_CREDENTIALS",),  # a gcloud login without bucket access gets 403
        note="Not checked against the store (private). The SKILL puts a global init at ~0.4 GB "
        "per field (up to 50 members x 240 6-hourly steps on a ~2.8 deg, ~8k-cell grid); "
        "era5:surface has t2m and d2m: one global 00 UTC init x 2 x ~0.39 GB = ~790 MB "
        "(~750 MiB). The skill also takes GOOGLE_APPLICATION_CREDENTIALS or gcloud ADC."
        " Small: the same init over a quarter of the longitudes and half the latitudes, 1/8 of "
        "the cells (~95 MiB).",
    ),
    FetchCase(
        "oisst-fetch",
        "oisst-fetch",
        large_args=("--start-time", "2023-01-01", "--end-time", "2023-06-30"),
        small_args=("--start-time", "2023-01-01", "--end-time", "2023-01-23"),
        note="OISST is 720 x 1440 (0.25 deg global) float32, one field: 181 days x 1.04M "
        "cells x 4 B = 751 MB (716 MiB), read in month blocks over OPeNDAP (8-day blocks "
        "with --dask)."
        " Small: 23 days, global: 23 x 1.04M x 4 B = 96 MB (92 MiB), 1/7.8.",
    ),
    FetchCase(
        "pbc-fetch",
        "pbc-fetch",
        large_args=(
            "--dataset",
            "era5-p_pr_19",
            "--start-time",
            "2025-07-01",
            "--end-time",
            "2026-10-31",
        ),
        small_args=(
            "--dataset",
            "era5-p_pr_19",
            "--start-time",
            "2026-09-01",
            "--end-time",
            "2026-09-15",
        ),
        creds=("GCLOUD_SERVICE_CREDENTIALS|~/.config/gcloud/application_default_credentials.json",),
        note="CANNOT REACH 512 MiB: PBC is 5 quintiles on the 1.5 deg global grid (121 x 240), "
        "one step per init, ~0.6 MB per init, and the whole era5-p_pr_19 archive is 123 "
        "inits (2025-07..2026-10, checked): 123 x 5 x 29,040 x 4 B = ~71 MB. This request "
        "is that whole archive, 8x the small one. The skill also "
        "takes GOOGLE_APPLICATION_CREDENTIALS or gcloud ADC."
        " Small: 15 daily inits (2026-09-01..09-15, ~0.55 MiB each), ~8 MiB, 1/8.",
    ),
    FetchCase(
        "rfe-fetch",
        "rfe-fetch",
        large_args=("--start-time", "2023-01-01", "--end-time", "2023-11-30"),
        small_args=("--start-time", "2023-01-01", "--end-time", "2023-02-11"),
        note="RFE2 is Africa only, 801 x 751 cells at 0.1 deg, float32, one ~0.7 MB file a "
        "day; the whole grid is the widest box, so time is the lever: 334 days x 601,551 "
        "cells x 4 B = 804 MB (766 MiB), less the known 2023-07-25 gap."
        " Small: 42 days of the same grid: 42 x 601,551 x 4 B = 101 MB (96 MiB), 1/8.",
    ),
    FetchCase(
        "smap-fetch",
        "smap-fetch",
        large_args=("--start-time", "2024-06-01", "--end-time", "2024-06-16"),
        small_args=("--start-time", "2024-06-01", "--end-time", "2024-06-02"),
        creds=("EARTHDATA_USERNAME+EARTHDATA_PASSWORD|~/.netrc",),
        note="9 km EASE-Grid 2.0 global is 1624 x 3856 cells and the skill writes float64: "
        "16 days x 6.26M cells x 8 B = 802 MB (764 MiB). Slow: each day is a ~690 MB "
        "global granule (~11 GB here; even the small case downloads one). The skill also "
        "logs in from a urs.earthdata.nasa.gov ~/.netrc entry."
        " Small: two days: 2 x 6.26M x 8 B = 100 MB (95 MiB), 1/8 (two ~690 MB granules).",
    ),
    FetchCase(
        "weathernext-fetch",
        "weathernext-fetch",
        large_args=(
            "--version",
            "2",
            "--date",
            "2025-06-01",
            "--cycle",
            "00",
            "-v",
            "t2m",
            "--member",
            "0",
            "--member",
            "1",
            "--member",
            "2",
        ),
        small_args=(
            "--version",
            "2",
            "--date",
            "2025-06-01",
            "--cycle",
            "00",
            "-v",
            "t2m",
            "--member",
            "0",
            "--member",
            "1",
            "--member",
            "2",
            "--max-lead",
            "48h",
        ),
        creds=("GCLOUD_SERVICE_CREDENTIALS|~/.config/gcloud/application_default_credentials.json",),
        note="WeatherNext 2 is 0.25 deg global (721 x 1440) float32, 60 6-hourly leads, one "
        "whole-globe chunk per member and lead (checked), so a bbox saves nothing: "
        "3 members x 60 leads x 1.04M cells x 4 B = 748 MB (713 MiB). The skill also "
        "takes GOOGLE_APPLICATION_CREDENTIALS or gcloud ADC."
        " Small: the same three members to 48 h (8 leads): 3 x 8 x 1.04M x 4 B = 100 MB (95 "
        "MiB), 1/7.5.",
    ),
    FetchCase(
        "windborne-fetch-grid",
        "windborne-fetch",
        large_args=("--product", "grid", "--ensemble", "members", "-v", "t2m"),
        small_args=("--product", "grid", "--ensemble", "members", "-v", "t2m", "--max-lead", "42"),
        creds=("WB_CLIENT_ID", "WB_API_KEY"),
        note="Latest run over the key's whole region (the key's region, not a bbox, bounds "
        "the grid, so neither request passes one). Not checked (needs a key): the SKILL "
        "gives ~7 MB per lead for 128 members of one field (about 14k 0.25 deg cells x "
        "128 x 4 B), so 121 leads x ~7 MB = ~850 MB (~810 MiB); the small one is 1 lead. "
        "Scales with the key's region."
        " Small: the first 15 of the 121 leads (0-42 h): ~105 MB, 1/8.",
    ),
]
