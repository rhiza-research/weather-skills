# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.2.0",
#   "pandas",
# ]
# ///
"""Fetch daily 3D-PAWS weather-station observations from a CHORDS portal as a station Zarr."""

import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.standard_utils import is_transient, require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

DEFAULT_PORTAL = "https://3d-fewsnet.icdp.ucar.edu"
DEFAULT_WORKERS = 8
# Days whose readings span less than this share are NaN (coverage still written).
DEFAULT_MIN_COVERAGE = 0.8
# CHORDS refuses a request over its max_download_points (200,000 on the FEWS
# NET portal). A 1-minute station with ~23 variables sends ~33,000 points a
# day, so 3-day requests stay well under the cap.
CHUNK_DAYS = 3

# Output variable -> (CHORDS short name, daily statistic). Precip comes from
# the rain gauges rg1 / rg2 instead.
SENSORS = {
    "temperature": ("mt1", "mean"),
    "tmax": ("mt1", "max"),
    "tmin": ("mt1", "min"),
    "humidity": ("sh1", "mean"),
    "pressure": ("bp1", "mean"),
    "wind_speed": ("ws", "mean"),
    "wind_gust": ("wg", "max"),
}
# Plausible range per short name. Outside it the reading is missing; this drops
# the -999.9 a station sends for a failed read.
VALID_RANGE = {
    "mt1": (-40.0, 60.0),
    "sh1": (0.0, 100.0),
    "bp1": (500.0, 1100.0),
    "ws": (0.0, 75.0),
    "wg": (0.0, 100.0),
    "rg1": (0.0, 100.0),
    "rg2": (0.0, 100.0),
}
# A station with no sensor fitted reports exactly 0 for it, so an exact 0 is
# missing for these (calm wind and no rain are real zeros and stay).
ZERO_IS_MISSING = {"mt1", "sh1", "bp1"}
# A rain reading over this rate (mm per minute of reporting interval) is no
# real rain: that gauge is broken for the day.
RAIN_MAX_MM_PER_MIN = 5
# Two gauges agree when their daily totals differ by at most this much.
GAUGE_TOLERANCE_MM = 2.0
GAUGE_TOLERANCE_FRAC = 0.5
# Longest gap between readings, in reporting intervals, that still counts as
# covered (3 min at 1-minute stations). A rain reading holds the rain since the
# previous one (checked against FEWS NET running totals, Oct 2026), so a skip
# loses no rain.
GAP_INTERVALS = 3
CF_META = {
    "precip": ("lwe_precipitation_rate", "mm day-1", None, "daily rainfall"),
    "temperature": (
        "air_temperature",
        "degree_Celsius",
        "time: mean",
        "daily mean air temperature",
    ),
    "tmax": ("air_temperature", "degree_Celsius", "time: maximum", "daily maximum air temperature"),
    "tmin": ("air_temperature", "degree_Celsius", "time: minimum", "daily minimum air temperature"),
    "humidity": ("relative_humidity", "1", "time: mean", "daily mean relative humidity"),
    "pressure": ("surface_air_pressure", "hPa", "time: mean", "daily mean station pressure"),
    "wind_speed": ("wind_speed", "m s-1", "time: mean", "daily mean wind speed"),
    "wind_gust": ("wind_speed_of_gust", "m s-1", "time: maximum", "daily maximum wind gust"),
}


def _get_json(url: str, params: dict | None = None):
    """GET ``url`` and decode JSON, retrying one transient error after a short backoff."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise UsageError(
                    "CHORDS refused CHORDS_EMAIL / CHORDS_API_KEY (HTTP 403). Keys are "
                    "issued per portal: use the key from this portal's /users page, and "
                    "the account needs data-download access."
                ) from None
            if attempt == 0 and is_transient(exc):
                time.sleep(2.0)
                continue
            body = exc.read().decode(errors="replace")[:300]
            raise DataError(f"CHORDS HTTP {exc.code}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 0:
                time.sleep(2.0)
                continue
            raise DataError(f"could not reach CHORDS ({exc})") from None


def _station_daily(portal, station_id, start, end, day_start_hour, min_coverage, auth):
    """Download one station's readings and reduce them to daily values.

    Days ``start`` .. ``end`` inclusive, each starting at ``day_start_hour`` UTC.
    Returns ``(daily DataFrame, reporting interval in minutes)``, or ``(None, None)``
    when the station sent nothing. Days missing any expected reading are NaN.
    """
    import pandas as pd

    offset = pd.Timedelta(hours=day_start_hour)
    # Fetch an hour either side of the requested days, so a gap at the edge of
    # the first or last day is measured against the readings next to it.
    fetch_start = start + offset - pd.Timedelta(hours=1)
    fetch_end = end + pd.Timedelta(days=1) + offset + pd.Timedelta(hours=1)
    rows = []
    chunk_start = fetch_start
    while chunk_start < fetch_end:
        chunk_end = min(chunk_start + pd.Timedelta(days=CHUNK_DAYS), fetch_end)
        params = {
            "start": chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **auth,
        }
        payload = _get_json(f"{portal}/api/v1/data/{station_id}.json", params)
        for feature in payload.get("features", []):
            for point in feature["properties"]["data"]:
                # CHORDS sends the test flag as the string "true" / "false".
                if str(point.get("test")).lower() != "true":
                    rows.append({"time": point["time"], **point["measurements"]})
        chunk_start = chunk_end
    if not rows:
        return None, None

    raw = pd.DataFrame(rows)
    raw["time"] = pd.to_datetime(raw["time"], utc=True).dt.tz_convert(None)
    # A reading sent twice counts once.
    raw = raw.drop_duplicates("time").set_index("time").sort_index()
    raw = raw[(raw.index >= fetch_start) & (raw.index < fetch_end)]
    raw = raw.apply(pd.to_numeric, errors="coerce")
    # Gauge 1 is "rg1" on newer stations and "rg" on older ones.
    raw = raw.rename(columns={"rg": "rg1"})

    clean = pd.DataFrame(index=raw.index)
    for short, (lo, hi) in VALID_RANGE.items():
        if short not in raw.columns:
            continue
        values = raw[short].where(raw[short].between(lo, hi))
        if short in ZERO_IS_MISSING:
            values = values.where(values != 0)
        # A sensor that sent only missing codes is treated as not fitted.
        if values.notna().any():
            # Relative humidity is stored as a fraction, as tahmo-fetch does.
            clean[short] = values / 100 if short == "sh1" else values
    # Shift by the day-start hour so each day bins as one calendar day labelled
    # by the date it starts on.
    clean.index = clean.index - offset

    # Reporting interval in whole minutes: the most common spacing between
    # readings over the whole fetch (ties go to the shorter). 1-minute stations drift 60-61 s apart on
    # their own timer; 15-minute stations report just after each quarter hour.
    # Days at any other spacing (dropped readings, or a changed interval) are
    # judged at this one and so come out NaN.
    spacing = (clean.index.to_series().diff().dt.total_seconds() / 60).round()
    spacing = spacing[spacing >= 1]
    interval_min = int(spacing.mode().iloc[0]) if len(spacing) else 1
    interval = pd.Timedelta(minutes=interval_min)

    # Rain gauges, judged day by day. A gauge with any reading over
    # RAIN_MAX_MM_PER_MIN that day is broken and left out. With both gauges
    # usable, rain counts only at readings where both reported (rain2 holds
    # gauge 2 there, to check the totals agree); with one, that gauge alone.
    day_of = clean.index.floor("D")
    usable = {}
    for g in [g for g in ("rg1", "rg2") if g in clean.columns]:
        over = pd.Series(raw[g].to_numpy() > RAIN_MAX_MM_PER_MIN * interval_min, index=clean.index)
        broken = over.groupby(day_of).transform("any")
        reported = clean[g].notna().groupby(day_of).transform("any")
        usable[g] = reported & ~broken
        if broken.any():
            # Count only the requested days, not the hour fetched either side.
            n = broken.groupby(day_of).first().loc[start:end].sum()
            print(
                f"{station_id}: {g} over {RAIN_MAX_MM_PER_MIN} mm/min on {n} day(s), left out",
                file=sys.stderr,
            )
    if len(usable) == 2:
        both_days = usable["rg1"] & usable["rg2"]
        both = both_days & clean["rg1"].notna() & clean["rg2"].notna()
        clean["rain"] = clean["rg1"].where(both | (usable["rg1"] & ~usable["rg2"]))
        clean["rain"] = clean["rain"].fillna(clean["rg2"].where(usable["rg2"] & ~usable["rg1"]))
        clean["rain2"] = clean["rg2"].where(both)
    elif usable:
        g = next(iter(usable))
        clean["rain"] = clean[g].where(usable[g])
    clean = clean.drop(columns=[g for g in ("rg1", "rg2") if g in clean.columns])

    # Coverage: the share of each day with readings. A gap of up to
    # GAP_INTERVALS counts as covered (1-minute stations skip about one reading
    # every 15 minutes); a longer one is uncovered from the reading after the
    # last valid one to the next valid one, split across the days it spans.
    edge0, edge1 = fetch_start - offset, fetch_end - offset
    all_days = pd.date_range(edge0.floor("D"), edge1.floor("D"), freq="D")
    one_day = pd.Timedelta(days=1)
    coverage = pd.DataFrame(1.0, index=all_days, columns=clean.columns)
    for short in clean.columns:
        t = pd.Series([edge0, *clean.index[clean[short].notna()], edge1])
        prev = t.shift()
        gap = (t - prev) > (GAP_INTERVALS + 0.5) * interval
        for a, b in zip(prev[gap] + interval, t[gap], strict=True):
            day = a.floor("D")
            while day < b:
                missed = min(b, day + one_day) - max(a, day)
                coverage.loc[day, short] -= missed / one_day
                day += one_day
    coverage = coverage.clip(lower=0.0).reindex(pd.date_range(start, end, freq="D"), fill_value=0.0)

    days = clean.resample("D")
    wanted = pd.date_range(start, end, freq="D", name="time")
    daily = pd.DataFrame(index=wanted)
    # A day keeps its value when its readings span at least min_coverage of it.
    kept = (coverage > 0) & (coverage >= min_coverage)
    # Temperature, humidity, pressure and wind: the statistic of the readings
    # there are; <var>_coverage says how much of the day they span.
    for name, (short, stat) in SENSORS.items():
        if short in clean.columns:
            daily[name] = days[short].agg(stat).reindex(wanted).where(kept[short])
            daily[f"{name}_coverage"] = coverage[short].to_numpy()
    # Rain: the total of the readings there are. On a partly covered day that is
    # only the rain in the covered part, never scaled up; precip_coverage says how
    # much of the day it is.
    if "rain" in clean.columns:
        has_rain = kept["rain"]
        total = days["rain"].sum().reindex(wanted).where(has_rain)
        precip_coverage = coverage["rain"]
        if "rain2" in clean.columns:
            # On days with both gauges they must agree; a disagreement means one
            # is faulty and there is no way to tell which. When they agree, the
            # larger total is used: a gauge fails low (a clogged funnel), and an
            # impossible high reading already left the gauge out above.
            checked = days["rain2"].count().reindex(wanted, fill_value=0) > 0
            total2 = days["rain2"].sum().reindex(wanted)
            larger = pd.concat([total, total2], axis=1).max(axis=1)
            tolerance = (GAUGE_TOLERANCE_FRAC * larger).clip(lower=GAUGE_TOLERANCE_MM)
            rejected = checked & ((total - total2).abs() > tolerance)
            total = total.where(~checked, larger.where(total.notna())).where(~rejected)
            # A rejected day has no usable rain, whatever its readings spanned.
            precip_coverage = precip_coverage.where(~rejected, 0.0)
        daily["precip"] = total
        daily["precip_coverage"] = precip_coverage.to_numpy()
    return daily, interval_min


@weather_skill(
    name="3dpaws-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    help="Every station whose site is in this box (also filters --list-stations).",
)
@weather_skill.argument(
    "--station",
    action="append",
    help="CHORDS instrument id (repeatable), e.g. 4. Discover ids with --list-stations.",
)
@weather_skill.argument(
    "--portal",
    default=DEFAULT_PORTAL,
    help=f"CHORDS portal base URL (default {DEFAULT_PORTAL}, FEWS NET). Each portal has its own key.",
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Max concurrent station downloads (default {DEFAULT_WORKERS}).",
)
@weather_skill.argument(
    "--day-start-hour",
    type=int,
    default=0,
    help="UTC hour each day starts at (0-23, default 0); 6 for the KMD 06 UTC rain day.",
)
@weather_skill.argument(
    "--min-coverage",
    type=float,
    default=DEFAULT_MIN_COVERAGE,
    help=f"Share of a day (0-1) readings must span to keep its value (default {DEFAULT_MIN_COVERAGE}).",
)
@weather_skill.argument(
    "--list-stations",
    action="store_true",
    default=False,
    probe=True,
    help="Print the stations (in --bbox, if given) as TSV on stdout and exit.",
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help="Print the last day that ended before the newest reading (YYYY-MM-DD) and exit.",
)
def fetch(
    start_time,
    end_time,
    bbox,
    station,
    portal,
    workers,
    day_start_hour,
    min_coverage,
    list_stations,
    probe_latest,
    **kwargs,
):
    """Fetch daily 3D-PAWS weather-station observations as a weather-skills station Zarr."""
    import pandas as pd
    import xarray as xr

    if not 0 <= day_start_hour <= 23:
        raise UsageError(f"--day-start-hour must be 0-23, got {day_start_hour}.")
    if not 0 <= min_coverage <= 1:
        raise UsageError(f"--min-coverage must be 0-1, got {min_coverage}.")
    if not list_stations and probe_latest is None and not station and bbox is None:
        raise UsageError("pass --station ID (repeatable) or --bbox N/W/S/E.")

    # Stations are the portal's instruments joined to their site coordinates.
    portal = portal.rstrip("/")
    instruments = pd.DataFrame(_get_json(f"{portal}/instruments.json"))
    sites = pd.DataFrame(_get_json(f"{portal}/sites.json"))
    sites = sites.rename(columns={"id": "site_id", "name": "site"})
    stations = instruments.merge(sites[["site_id", "site", "lat", "lon"]], on="site_id", how="left")
    stations["station_id"] = stations["id"].astype(str)
    stations["name"] = stations["name"].str.strip()
    stations["site"] = stations["site"].str.strip()
    stations["latitude"] = pd.to_numeric(stations["lat"], errors="coerce")
    stations["longitude"] = pd.to_numeric(stations["lon"], errors="coerce")
    stations = stations.sort_values("id")
    if bbox is not None:
        north, west, south, east = bbox
        lon = stations["longitude"]
        lon_in = lon.between(west, east) if west <= east else (lon >= west) | (lon <= east)
        in_box = stations[stations["latitude"].between(south, north) & lon_in]

    if list_stations:
        listed = stations if bbox is None else in_box
        writer = csv.writer(sys.stdout, dialect="excel-tab", lineterminator="\n")
        writer.writerow(["station_id", "name", "site", "latitude", "longitude"])
        for _, row in listed.iterrows():
            lat = "" if pd.isna(row["latitude"]) else f"{row['latitude']:.5f}"
            lon = "" if pd.isna(row["longitude"]) else f"{row['longitude']:.5f}"
            writer.writerow([row["station_id"], row["name"], row["site"], lat, lon])
        print(f"{len(listed)} stations", file=sys.stderr)
        return

    email, api_key = require_env(
        "CHORDS_EMAIL",
        "CHORDS_API_KEY",
        message="CHORDS_EMAIL and CHORDS_API_KEY must be set (from the portal's /users page).",
    )
    auth = {"email": email, "api_key": api_key}

    located = stations.dropna(subset=["latitude", "longitude"])
    if station:
        wanted = [s.strip() for s in station]
        unknown = [s for s in wanted if s not in set(stations["station_id"])]
        if unknown:
            raise UsageError("unknown station id(s): " + ", ".join(unknown))
        no_coords = [s for s in wanted if s not in set(located["station_id"])]
        if no_coords:
            raise DataError(
                "station(s) have no site coordinates on the portal: " + ", ".join(no_coords)
            )
        selected = located.set_index("station_id", drop=False).loc[wanted]
    elif bbox is not None:
        selected = in_box
        if selected.empty:
            raise DataError(f"no stations in --bbox {north:g}/{west:g}/{south:g}/{east:g}.")
    else:
        selected = located  # --probe-latest with no stations named

    if probe_latest is not None:
        # Ask each station for its single latest reading (CHORDS `last`), all at
        # once; downloading days of 1-minute data would take minutes. The latest
        # day is the last one that ended before the newest reading. Completeness
        # is only checked by a fetch.
        offset = pd.Timedelta(hours=day_start_hour)
        now = pd.Timestamp.now(tz="UTC").tz_convert(None)
        newest = None
        with ThreadPoolExecutor(max_workers=min(len(selected), 32)) as pool:
            futures = {
                pool.submit(_get_json, f"{portal}/api/v1/data/{sid}.json", {"last": 1, **auth}): sid
                for sid in selected["station_id"]
            }
            for fut in as_completed(futures):
                try:
                    payload = fut.result()
                except UsageError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"{futures[fut]}: no latest reading ({exc})", file=sys.stderr)
                    continue
                for feature in payload.get("features", []):
                    for point in feature["properties"]["data"]:
                        t = pd.Timestamp(point["time"]).tz_convert(None)
                        # Some station clocks run ahead (one reports 2027).
                        if t <= now:
                            newest = t if newest is None else max(newest, t)
        if newest is None:
            raise DataError("no station reported a latest reading.")
        print(f"Newest reading: {newest:%Y-%m-%d %H:%M} UTC", file=sys.stderr)
        latest_day = (newest - offset).normalize() - pd.Timedelta(days=1)
        print(latest_day.strftime("%Y-%m-%d"))
        return

    start = pd.Timestamp(start_time)
    end = pd.Timestamp(end_time)
    print(
        f"Fetching {len(selected)} 3D-PAWS station(s) from {portal} {start:%Y-%m-%d} → "
        f"{end:%Y-%m-%d}, days starting {day_start_hour:02d}:00 UTC",
        file=sys.stderr,
    )

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _station_daily,
                portal,
                row["station_id"],
                start,
                end,
                day_start_hour,
                min_coverage,
                auth,
            ): row
            for _, row in selected.iterrows()
        }
        for fut in as_completed(futures):
            row = futures[fut]
            sid = row["station_id"]
            try:
                daily, interval_min = fut.result()
            except UsageError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"{sid}: DROPPED, fetch failed ({exc})", file=sys.stderr)
                continue
            if daily is None:
                print(f"{sid}: no readings in range", file=sys.stderr)
                continue
            values = daily[[c for c in daily.columns if not c.endswith("_coverage")]]
            with_data = int(values.notna().any(axis=1).sum())
            if with_data == 0:
                print(f"{sid}: no usable day for any variable", file=sys.stderr)
                continue
            print(
                f"{sid} ({row['name']}): reports every {interval_min} min, "
                f"{with_data}/{len(daily)} days with data",
                file=sys.stderr,
            )
            daily["station_id"] = sid
            frames.append(daily)
            meta_rows.append(row)

    if not frames:
        raise DataError("no usable daily data for any station.")

    df = pd.concat(frames).reset_index().set_index(["time", "station_id"])
    meta = pd.DataFrame(meta_rows).set_index("station_id")
    ds = xr.Dataset.from_dataframe(df)
    ids = ds["station_id"].values
    ds = ds.assign_coords(
        latitude=("station_id", meta.loc[ids, "latitude"].astype(float).values),
        longitude=("station_id", meta.loc[ids, "longitude"].astype(float).values),
        name=("station_id", meta.loc[ids, "name"].astype(str).values),
        site=("station_id", meta.loc[ids, "site"].fillna("").astype(str).values),
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(
        standard_name="time",
        axis="T",
        comment=f"day D covers D {day_start_hour:02d}:00 to D+1 {day_start_hour:02d}:00 UTC",
    )
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="CHORDS instrument id")
    ds["name"].attrs.update(long_name="instrument name")
    ds["site"].attrs.update(long_name="site name")
    for var in [v for v in ds.data_vars if v in CF_META]:
        std_name, units, cell_methods, long_name = CF_META[var]
        ds[var].attrs.update(
            standard_name=std_name,
            units=units,
            long_name=long_name,
            coordinates="latitude longitude",
            ancillary_variables=f"{var}_coverage",
        )
        # Precip has no cell_methods, as other station fetchers write it.
        if cell_methods:
            ds[var].attrs["cell_methods"] = cell_methods
        # A station absent from the fetch has no readings: coverage 0.
        ds[f"{var}_coverage"] = ds[f"{var}_coverage"].fillna(0.0).astype("float32")
        ds[f"{var}_coverage"].attrs.update(
            long_name=f"share of the day with {var} data",
            units="1",
            valid_min=0.0,
            valid_max=1.0,
            coordinates="latitude longitude",
        )
    ds.attrs.update(
        featureType="timeSeries",
        Conventions="CF-1.13",
        weather_skills_source="3dpaws",
        source_url=portal,
    )
    # Station-days with a value, and with the whole day covered.
    for name in sorted(v for v in ds.data_vars if v in CF_META):
        print(
            f"{name}: {int(ds[name].notnull().sum())} of {ds[name].size} station-days with "
            f"a value, {int((ds[f'{name}_coverage'] >= 1).sum())} fully covered",
            file=sys.stderr,
        )
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
