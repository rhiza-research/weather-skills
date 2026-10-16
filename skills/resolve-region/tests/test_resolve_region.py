"""Correctness tests for resolve-region."""

import json
from pathlib import Path

import pytest
from conftest import load_skill, run_skill

_NAIROBI = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "shapeName": "Nairobi",
                "shapeGroup": "KEN",
                "shapeType": "ADM1",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [36.6, -1.4],
                        [37.0, -1.4],
                        [37.0, -1.1],
                        [36.6, -1.1],
                        [36.6, -1.4],
                    ]
                ],
            },
        }
    ],
}


@pytest.fixture(scope="module")
def resolve_mod():
    return load_skill("resolve-region", "resolve")


@pytest.fixture
def resolve_region(resolve_mod):
    return resolve_mod.resolve_region


def test_ken_prints_bbox(capsys, resolve_region):
    run_skill(resolve_region, "KEN")

    line = capsys.readouterr().out.strip()
    n, w, s, e = (float(x) for x in line.split("/"))
    assert n > s
    assert -180 <= w <= 180
    assert -180 <= e <= 180


def test_geojson_write(tmp_path, resolve_region):
    geo = tmp_path / "ken.geojson"

    run_skill(resolve_region, "KEN", "--geojson", str(geo))

    assert Path(geo).exists()
    data = json.loads(geo.read_text())
    assert data["type"] == "FeatureCollection"
    assert data["features"][0]["properties"]["iso3"] == "KEN"


def test_lowercase_iso3_is_usage_error(resolve_region):
    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_region, "ken")
    assert exc.value.code == 2


def test_county_prints_bbox(capsys, resolve_mod, monkeypatch):
    from weather_skills_core.region import _admin_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_admin_geojson",
        lambda iso3, level: (
            _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []}
        ),
    )
    _admin_collection.cache_clear()

    run_skill(resolve_mod.resolve_region, "kenya-nairobi")

    line = capsys.readouterr().out.strip()
    n, w, s, e = (float(x) for x in line.split("/"))
    assert n == pytest.approx(-1.1)
    assert s == pytest.approx(-1.4)
    assert w == pytest.approx(36.6)
    assert e == pytest.approx(37.0)


def test_county_geojson_write(tmp_path, resolve_mod, monkeypatch):
    from weather_skills_core.region import _admin_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_admin_geojson",
        lambda iso3, level: (
            _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []}
        ),
    )
    _admin_collection.cache_clear()

    geo = tmp_path / "nairobi.geojson"
    run_skill(resolve_mod.resolve_region, "KEN-nairobi", "--geojson", str(geo))

    data = json.loads(geo.read_text())
    props = data["features"][0]["properties"]
    assert props["iso3"] == "KEN"
    assert props["level"] == "admin_1"
    assert props["region_name"] == "kenya-nairobi"
    assert props["name"] == "Nairobi"


_MOUNT_KENYA_HIT = [
    {
        "display_name": "Mount Kenya, Kenya",
        "name": "Mount Kenya",
        "boundingbox": ["-0.25", "-0.05", "37.2", "37.4"],
        "lat": "-0.15",
        "lon": "37.3",
        "geojson": {
            "type": "Polygon",
            "coordinates": [
                [
                    [37.2, -0.25],
                    [37.4, -0.25],
                    [37.4, -0.05],
                    [37.2, -0.05],
                    [37.2, -0.25],
                ]
            ],
        },
    }
]


def test_landmark_falls_through_to_nominatim(capsys, resolve_mod, monkeypatch):
    from weather_skills_core.region import _nominatim_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_nominatim",
        lambda query: _MOUNT_KENYA_HIT,
    )
    _nominatim_collection.cache_clear()

    run_skill(resolve_mod.resolve_region, "Mount Kenya, Kenya")

    captured = capsys.readouterr()
    n, w, s, e = (float(x) for x in captured.out.strip().split("/"))
    assert (n, w, s, e) == pytest.approx((-0.05, 37.2, -0.25, 37.4))
    assert "nominatim: Mount Kenya, Kenya" in captured.err


def test_unknown_admin_key_does_not_geocode(resolve_mod, monkeypatch):
    from weather_skills_core.region import _admin_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_admin_geojson",
        lambda iso3, level: (
            _NAIROBI if level == 1 else {"type": "FeatureCollection", "features": []}
        ),
    )
    _admin_collection.cache_clear()

    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for admin typo; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_mod.resolve_region, "kenya-nairbi")
    assert exc.value.code == 1


def test_unknown_iso3_does_not_geocode(resolve_region, monkeypatch):
    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for ISO3; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_region, "ZZZ")
    assert exc.value.code == 1


def test_east_africa_is_eastern_africa(capsys, resolve_region, monkeypatch):
    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for East Africa; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    run_skill(resolve_region, "East Africa")
    east = capsys.readouterr().out.strip()
    run_skill(resolve_region, "Eastern Africa")
    assert capsys.readouterr().out.strip() == east
    n, w, s, e = (float(x) for x in east.split("/"))
    assert s < -20  # keeps Madagascar
    assert n > 10
    assert w < 30 < e


def test_indian_ocean_prints_basin_bbox(capsys, resolve_region, monkeypatch):
    def _fail_nominatim(query):
        raise AssertionError(f"Nominatim should not run for Indian Ocean; got {query!r}")

    monkeypatch.setattr("weather_skills_core.region._load_nominatim", _fail_nominatim)

    run_skill(resolve_region, "Indian Ocean")
    assert capsys.readouterr().out.strip() == "30.0/20.0/-40.0/120.0"
    run_skill(resolve_region, "Indian Ocean basin")
    assert capsys.readouterr().out.strip() == "30.0/20.0/-40.0/120.0"


def test_landmark_geojson_write(tmp_path, resolve_mod, monkeypatch):
    from weather_skills_core.region import _nominatim_collection

    monkeypatch.setattr(
        "weather_skills_core.region._load_nominatim",
        lambda query: _MOUNT_KENYA_HIT,
    )
    _nominatim_collection.cache_clear()

    geo = tmp_path / "mount-kenya.geojson"
    run_skill(resolve_mod.resolve_region, "Mount Kenya", "--geojson", str(geo))

    data = json.loads(geo.read_text())
    props = data["features"][0]["properties"]
    assert props["level"] == "nominatim"
    assert props["name"] == "Mount Kenya"
    assert data["features"][0]["geometry"]["type"] == "Polygon"


def test_point_sized_nominatim_hit_is_refused(capsys, resolve_mod, monkeypatch):
    from weather_skills_core.region import _nominatim_collection

    point_hit = [
        {
            "display_name": "Indian Ocean Drive, Yanchep, Western Australia, Australia",
            "name": "Indian Ocean Drive",
            "boundingbox": ["-31.5044", "-31.5006", "115.6889", "115.6913"],
            "geojson": {"type": "Point", "coordinates": [115.69, -31.502]},
        }
    ]
    monkeypatch.setattr("weather_skills_core.region._load_nominatim", lambda query: point_hit)
    _nominatim_collection.cache_clear()

    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_mod.resolve_region, "western Indian Ocean")
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Indian Ocean Drive" in captured.err
    assert "centre point" in captured.err


def _point_hit(display, category, kind):
    return [
        {
            "display_name": display,
            "name": display.split(",")[0],
            "category": category,
            "type": kind,
            "boundingbox": ["-0.1528", "-0.1527", "37.3091", "37.3092"],
            "lat": "-0.15275",
            "lon": "37.30915",
        }
    ]


def test_point_landmark_is_padded(capsys, resolve_mod, monkeypatch, tmp_path):
    from weather_skills_core.region import _nominatim_collection

    hit = _point_hit("Batian, Embu, Kenya", "natural", "volcano")
    monkeypatch.setattr("weather_skills_core.region._load_nominatim", lambda query: hit)
    _nominatim_collection.cache_clear()

    geo = tmp_path / "peak.geojson"
    run_skill(
        resolve_mod.resolve_region, "Mount Kenya, Kenya", "--pad", "0.5", "--geojson", str(geo)
    )
    captured = capsys.readouterr()
    n, w, s, e = (float(v) for v in captured.out.strip().split("/"))
    assert (n, w, s, e) == pytest.approx((0.3473, 36.8091, -0.6528, 37.8092))
    assert "padded by 0.5" in captured.err
    assert json.loads(geo.read_text())["features"][0]["geometry"]["type"] == "Polygon"


def test_point_centroid_of_an_area_is_refused(capsys, resolve_mod, monkeypatch):
    from weather_skills_core.region import _nominatim_collection

    hit = _point_hit("Somali Peninsula, Bari, Somalia", "natural", "peninsula")
    monkeypatch.setattr("weather_skills_core.region._load_nominatim", lambda query: hit)
    _nominatim_collection.cache_clear()

    with pytest.raises(SystemExit) as exc:
        run_skill(resolve_mod.resolve_region, "Horn of Africa")
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "centre point" in captured.err
