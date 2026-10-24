"""Correctness tests for label-group."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core import region as region_mod
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def label_group():
    return load_skill("label-group", "label_group").label_group


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def offline(iso3, level):
        raise region_mod._AdminMissing("offline test")

    region_mod._admin_collection.cache_clear()
    monkeypatch.setattr("weather_skills_core.region._load_admin_geojson", offline)


def _box(name, west, south, east, north):
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {
        "type": "Feature",
        "properties": {"basin": name},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _geojson(tmp_path, *features):
    path = tmp_path / "zones.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": list(features)}))
    return str(path)


def test_geojson_labels_grid_by_cell_centre(tmp_path, label_group):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(
        tmp_path, _box("west", 9.5, 0.5, 11.5, 3.5), _box("east", 11.5, 0.5, 12.5, 2.5)
    )
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["basin"].dims == ("latitude", "longitude")
    # Rows are lat 1, 2, 3; columns lon 10..13.
    assert ds["basin"].values.tolist() == [
        ["west", "west", "east", ""],
        ["west", "west", "east", ""],
        ["west", "west", "", ""],
    ]
    assert ds["precip"].values.tolist() == make_gridded()["precip"].values.tolist()
    assert load_history(out)[-1]["skill"] == "label-group"


def test_geojson_labels_stations(tmp_path, label_group):
    src = write_zarr(make_point_obs(n_points=2), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("north", 10.5, 1.5, 11.5, 2.5))
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["basin"].dims == ("point_id",)
    assert ds["basin"].values.tolist() == ["", "north"]


def test_level_subregion_uses_resolve_region_keys(tmp_path, label_group):
    # 0.0/38.0 is in Kenya; 10.0/8.0 is in Nigeria; 0.0/-30.0 is open Atlantic.
    ds = make_gridded(lats=(0.0, 10.0), lons=(-30.0, 8.0, 38.0))
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", "subregion")

    labels = xr.open_zarr(out, consolidated=True)["subregion"]
    assert labels.sel(latitude=0.0, longitude=38.0).item() == "eastern_africa"
    assert labels.sel(latitude=10.0, longitude=8.0).item() == "western_africa"
    assert labels.sel(latitude=0.0, longitude=-30.0).item() == ""
    names = xr.open_zarr(out, consolidated=True)["subregion_name"]
    assert names.sel(latitude=0.0, longitude=38.0).item() == "Eastern Africa"


def test_time_parts_and_month_groups(tmp_path, label_group):
    ds = make_gridded(n_time=365, start="2023-07-01")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        label_group,
        "-i",
        str(src),
        "-o",
        str(out),
        "--time",
        "month",
        "--time",
        "year",
        "--time",
        "DJF,MAM",
    )

    ds = xr.open_zarr(out, consolidated=True)
    at = ds.sel(time=["2023-07-01", "2023-12-15", "2024-01-15", "2024-02-29", "2024-04-01"])
    assert at["month"].values.tolist() == [7, 12, 1, 2, 4]
    assert at["month_group"].values.tolist() == ["", "DJF", "DJF", "DJF", "MAM"]
    # DJF takes the year it begins.
    assert at["year"].values.tolist() == [2023, 2023, 2023, 2023, 2024]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("J", "more than one"),
        ("MAX", "not a run"),
        ("mam", "not a run"),
        ("MAM;OND", "not a run"),
        ("MAM,AMJ", "share a month"),
    ],
)
def test_bad_month_groups(tmp_path, label_group, spec, message, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(label_group, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "--time", spec)
    assert message in capsys.readouterr().err


def test_level_and_geojson_are_exclusive(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("a", 0, 0, 1, 1))
    with pytest.raises(SystemExit):
        run_skill(
            label_group,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--level",
            "country",
            "--geojson",
            zones,
            "--property",
            "basin",
        )
    assert "not both" in capsys.readouterr().err


def test_unit_without_cells_is_reported(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("tiny", 10.1, 1.1, 10.2, 1.2), _box("all", 9, 0, 14, 4))
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    assert "tiny" in capsys.readouterr().err
    assert set(np.unique(xr.open_zarr(out, consolidated=True)["basin"].values)) == {"all"}


def test_point_value_output_with_grid_coords(tmp_path, label_group):
    """Stations that also carry grid_latitude/grid_longitude (point-value output) still label."""
    ds = make_point_obs(n_points=2)
    ds = ds.assign_coords(
        grid_latitude=("point_id", [1.0, 2.0]), grid_longitude=("point_id", [10.0, 11.0])
    )
    ds["grid_latitude"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["grid_longitude"].attrs.update(standard_name="longitude", units="degrees_east")
    src = write_zarr(ds, tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("north", 10.5, 1.5, 11.5, 2.5))
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    assert xr.open_zarr(out, consolidated=True)["basin"].values.tolist() == ["", "north"]


def test_overlapping_units_are_reported(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("a", 9.5, 0.5, 11.5, 3.5), _box("b", 10.5, 0.5, 13.5, 3.5))
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    # Column lon=11 is in both boxes: 3 cells, kept by the first unit.
    assert "3 cell(s)/station(s) lie in two units" in capsys.readouterr().err
    assert (xr.open_zarr(out, consolidated=True)["basin"].sel(longitude=11.0) == "a").all()


def _stations(lats, lons):
    ds = make_point_obs(n_points=len(lats))
    return ds.assign_coords(latitude=("point_id", list(lats)), longitude=("point_id", list(lons)))


def test_coastal_stations_join_nearest_country(tmp_path, label_group, capsys):
    """Mogadishu airport and Lamu lie ~1 km off the 1:50m outlines; open ocean stays ""."""
    src = write_zarr(
        _stations((2.014, -2.2524, 0.0), (45.305, 40.9131, 50.0)), tmp_path / "in.zarr"
    )
    out = tmp_path / "out.zarr"

    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", "country")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["country"].values.tolist() == ["somalia", "kenya", ""]
    assert ds["country_name"].values.tolist() == ["Somalia", "Kenya", ""]
    assert "2 station(s) outside every unit joined the nearest unit" in capsys.readouterr().err


def test_grid_cells_do_not_snap(tmp_path, label_group):
    """Cell centres 0.05° outside a polygon stay unlabelled; stations that close snap."""
    zones = _geojson(tmp_path, _box("west", 9.5, 0.5, 11.95, 3.5))
    grid = write_zarr(make_gridded(), tmp_path / "grid.zarr")
    run_skill(
        label_group,
        "-i",
        str(grid),
        "-o",
        str(tmp_path / "g.zarr"),
        "--geojson",
        zones,
        "--property",
        "basin",
    )
    assert xr.open_zarr(tmp_path / "g.zarr", consolidated=True)["basin"].sel(
        longitude=12.0
    ).values.tolist() == ["", "", ""]

    points = write_zarr(_stations((1.0, 1.0), (12.0, 12.2)), tmp_path / "pts.zarr")
    run_skill(
        label_group,
        "-i",
        str(points),
        "-o",
        str(tmp_path / "p.zarr"),
        "--geojson",
        zones,
        "--property",
        "basin",
    )
    # 0.05° away snaps; 0.25° away is beyond the 0.1° tolerance.
    assert xr.open_zarr(tmp_path / "p.zarr", consolidated=True)["basin"].values.tolist() == [
        "west",
        "",
    ]


def test_antimeridian_grid_queries_two_spans(tmp_path, monkeypatch):
    module = load_skill("label-group", "label_group")
    seen = []

    def spy(level, bbox):
        seen.append(bbox)
        return region_mod.level_features(level, bbox)

    monkeypatch.setattr(module, "level_features", spy)
    src = write_zarr(
        make_gridded(lats=(-17.8, -16.0), lons=(-179.0, 175.0, 178.0)), tmp_path / "in.zarr"
    )
    out = tmp_path / "out.zarr"

    run_skill(module.label_group, "-i", str(src), "-o", str(out), "--level", "country")

    north, west, south, east = seen[0]
    # west > east: 175E to 179W (padded 0.1°), not the whole band.
    assert (west, east) == pytest.approx((174.9, -178.9))
    assert (
        xr.open_zarr(out, consolidated=True)["country"].sel(latitude=-17.8, longitude=178.0).item()
        == "fiji"
    )


def test_0_360_longitudes_are_refused(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(lons=(175.0, 181.0)), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(label_group, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "--level", "country")
    assert "needs -180..180" in capsys.readouterr().err

    zones = _geojson(tmp_path, _box("p", 190.0, 0.5, 210.0, 3.5))
    src = write_zarr(make_gridded(), tmp_path / "in2.zarr")
    with pytest.raises(SystemExit):
        run_skill(
            label_group, "-i", str(src), "-o", str(tmp_path / "o.zarr"),
            "--geojson", zones, "--property", "basin",
        )  # fmt: skip
    assert "longitudes above 180" in capsys.readouterr().err


def test_existing_name_coord_is_refused(tmp_path, label_group, capsys):
    ds = make_gridded().assign_coords(country_name=("latitude", ["a", "b", "c"]))
    src = write_zarr(ds, tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(label_group, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "--level", "country")
    assert "'country_name' already exists" in capsys.readouterr().err


def _fake_admin(monkeypatch):
    """Kenya admin layers: Nairobi and Kiambu (admin_1), and Kiambu Town in Kiambu (admin_2)."""

    def unit(name, west, south, east, north):
        feature = _box(name, west, south, east, north)
        feature["properties"] = {"shapeName": name}
        return feature

    layers = {
        1: [unit("Nairobi", 36.6, -1.45, 37.1, -1.15), unit("Kiambu", 36.6, -1.15, 37.1, -0.75)],
        2: [
            unit("Kiambu Town", 36.7, -1.1, 36.9, -0.9),
            unit("Westlands", 36.6, -1.45, 37.1, -1.15),
        ],
    }

    def fake(iso3, level):
        if iso3 != "KEN":
            raise region_mod._AdminMissing("not in this test")
        return {"type": "FeatureCollection", "features": layers[level]}

    region_mod._admin_collection.cache_clear()
    monkeypatch.setattr("weather_skills_core.region._load_admin_geojson", fake)


def test_level_admin_1_labels_grid(tmp_path, label_group, monkeypatch):
    _fake_admin(monkeypatch)
    src = write_zarr(make_gridded(lats=(-1.3, -1.0), lons=(36.8,)), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", "admin_1")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["admin_1"].values.ravel().tolist() == ["kenya-nairobi", "kenya-kiambu"]
    assert ds["admin_1_name"].values.ravel().tolist() == ["Nairobi", "Kiambu"]


def test_level_admin_2_keys_name_their_admin_1(tmp_path, label_group, monkeypatch):
    _fake_admin(monkeypatch)
    src = write_zarr(_stations((-1.3, -1.0), (36.8, 36.8)), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", "admin_2")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["admin_2"].values.tolist() == ["kenya-nairobi-westlands", "kenya-kiambu-kiambu_town"]
    assert ds["admin_2_name"].values.tolist() == ["Westlands", "Kiambu Town"]


def test_month_groups_add_up_across_flags(tmp_path, label_group):
    src = write_zarr(make_gridded(n_time=5), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        label_group, "-i", str(src), "-o", str(out),
        "--time", "DJF,", "--time", "MAM", "--time", "DJF",
    )  # fmt: skip
    ds = xr.open_zarr(out, consolidated=True)
    expected = np.where(
        np.isin(ds["time"].dt.month, [12, 1, 2]),
        "DJF",
        np.where(np.isin(ds["time"].dt.month, [3, 4, 5]), "MAM", ""),
    )
    assert ds["month_group"].values.tolist() == expected.tolist()


def test_cell_centre_on_shared_border_takes_first_unit(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    # Column lon=11 lies on the border of both boxes.
    zones = _geojson(tmp_path, _box("a", 9.5, 0.5, 11.0, 3.5), _box("b", 11.0, 0.5, 13.5, 3.5))
    out = tmp_path / "out.zarr"
    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )
    basin = xr.open_zarr(out, consolidated=True)["basin"]
    assert (basin != "").all()
    assert (basin.sel(longitude=11.0) == "a").all()
    assert "shared border" in capsys.readouterr().err


def test_null_geometry_is_skipped_and_null_property_refused(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    hollow = _box("hollow", 9.5, 0.5, 13.5, 3.5)
    hollow["geometry"] = None
    zones = _geojson(tmp_path, hollow, _box("b", 9.5, 0.5, 13.5, 3.5))
    out = tmp_path / "out.zarr"
    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )
    assert (xr.open_zarr(out, consolidated=True)["basin"] == "b").all()
    assert "hollow" in capsys.readouterr().err

    unnamed = _box(None, 9.5, 0.5, 13.5, 3.5)
    zones = _geojson(tmp_path, unnamed)
    with pytest.raises(SystemExit):
        run_skill(
            label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
        )
    assert "missing or null on 1 feature" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("level", "lats", "lons"),
    [
        ("country", np.arange(-35, 38, 1.0), np.arange(-18, 52, 1.0)),
        ("continent", np.arange(-89, 90, 4.0), np.arange(-179, 180, 4.0)),
        # Crosses 180°: label-group queries a west > east box.
        ("country", np.arange(-25, -10, 0.5), np.r_[np.arange(170, 180, 0.5), np.arange(-180, -170, 0.5)]),
    ],
)  # fmt: skip
def test_grid_labels_match_brute_force(tmp_path, label_group, level, lats, lons):
    """Every cell centre against every world outline, first one wins: the obvious answer."""
    import shapely
    from shapely.geometry import shape

    ds = xr.Dataset(
        {"x": (("latitude", "longitude"), np.ones((lats.size, lons.size), "float32"))},
        coords={"latitude": lats, "longitude": lons},
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", level)

    lon2d, lat2d = np.meshgrid(lons, lats)
    expected = np.full(lon2d.shape, "", dtype=object)
    for feature in region_mod.level_features(level, None):
        hit = shapely.intersects_xy(shape(feature["geometry"]), lon2d, lat2d) & (expected == "")
        expected[hit] = feature["properties"]["region_name"]
    got = xr.open_zarr(out, consolidated=True)[level].values
    assert (got != "").any()
    np.testing.assert_array_equal(got, expected.astype(str))


def test_admin_unit_past_the_bundled_coastline_is_fetched(tmp_path, label_group, monkeypatch):
    # Natural Earth's Madagascar stops at -12.08°; geoBoundaries' Diana reaches -11.95°.
    diana = _box("Diana", 48.5, -14.5, 50.5, -11.95)
    diana["properties"] = {"shapeName": "Diana"}

    def fake(iso3, level):
        if iso3 != "MDG":
            raise region_mod._AdminMissing("not in this test")
        return {"type": "FeatureCollection", "features": [diana]}

    region_mod._admin_collection.cache_clear()
    monkeypatch.setattr("weather_skills_core.region._load_admin_geojson", fake)
    src = write_zarr(make_gridded(lats=(-12.0, -11.0), lons=(49.25,)), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", "admin_1")

    labels = xr.open_zarr(out, consolidated=True)["admin_1"].values.ravel().tolist()
    assert labels == ["madagascar-diana", ""]
