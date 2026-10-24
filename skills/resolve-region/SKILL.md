---
name: resolve-region
description: "Turn a country, multi-country region, ocean box, state/province/county, or landmark name into a N/W/S/E lat/lon bounding box, and optionally write its boundary polygon as GeoJSON. Use whenever a task names a place and a later step needs a bbox or a polygon mask."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py *)
metadata:
  version: "0.0.4"
  catalog-group: agent-tooling
---

# resolve-region

Prints the bounding box of a named place, and can write its outline as a GeoJSON polygon.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py QUERY [--geojson PATH] [--pad DEG]
```

### Arguments
- `code` (positional `QUERY`) — one of, tried in this order:
  - Country: uppercase ISO 3166-1 alpha-3 (`KEN`, `COD`) or country name (`Kenya`).
  - Multi-country region, by Natural Earth continent / UN subregion / World Bank label: `East Africa`, `Western Africa`, `Sub-Saharan Africa`.
  - Custom box: `Indian Ocean` (also `Indian Ocean basin`, `IOB`) = `30.0/20.0/-40.0/120.0`.
  - Sub-national key: names lowercased, spaces to `_`, accents stripped, levels joined by `-`. State/province/Kenyan county is `country-admin1` (`kenya-nairobi`, `KEN-nairobi`, `USA-california`); district/sub-county is `country-admin1-admin2` or `country-admin2` (`kenya-nairobi-westlands`, `kenya-westlands`).
  - Anything else is a landmark, looked up once on OpenStreetMap's Nominatim geocoder: `Lake Victoria, Kenya`, `Mount Kenya`.
- `--geojson` — also write the outline to this path as a one-feature GeoJSON FeatureCollection. Default: not written.
- `--pad` — degrees added on each side of a landmark that is only a point (a peak, a town). Default: `0.25`.

### Output
- stdout: one line `N/W/S/E` in decimal degrees, ready for any `--bbox` flag. West > east means the box crosses the 180° meridian (e.g. Russia, Fiji).
- `--geojson`: feature properties `iso3`, `name`, `region_name`, `level` (`country`, `region`, `custom`, `admin_1`, `admin_2`, `nominatim`), `country`; countries add `boundary_source`; regions, custom boxes and landmarks add `bbox` (`[N, W, S, E]`).
- stderr names what matched: `nominatim: <display name>` for landmarks, a padding note for points, a warning when a country falls back to the coarse outline.

### Notes
- Use ISO3, region names, and sub-national keys whenever they fit; only leftover text goes to Nominatim. Read the `nominatim:` line to check the match. Make a landmark more specific by adding the country (`Lake Victoria, Kenya`).
- River basins and catchments are not supported: Nominatim returns an unrelated basin (`Tana River basin` matches one in the Philippines). Use a basin GeoJSON from elsewhere. `kenya-tana_river` is the county.
- Exits non-zero, printing nothing on stdout, for: lowercase 3-letter tokens or alpha-2 codes, unknown sub-national names (never sent to Nominatim), no Nominatim hit, a road match, or an area OSM stores only as a centre point (`Horn of Africa`). Pass an explicit bbox for those.
- `East Africa` is the full UN Eastern Africa subregion, including Madagascar, Mozambique and Zambia.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/resolve.py KEN --geojson ken.json
```
Prints `5.430648327058691/33.91181945760195/-4.702209472788866/41.90625762938612` and writes Kenya's geoBoundaries outline to `ken.json`.

## Details

Countries use the geoBoundaries ADM0 outline, which shares edges with the county layers and is fine enough to mask a 0.05° grid. Multi-country regions are dissolved from coarse Natural Earth 1:110m outlines. Sources, licences, and matching rules: [references/REFERENCE.md](references/REFERENCE.md).
