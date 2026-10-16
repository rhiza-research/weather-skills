# resolve-region reference

## Lookup order

1. **Countries** — uppercase ISO3 or a country name, matched offline against the bundled Natural Earth 1:110m admin-0 list (177 countries; micro-island states are absent). Names are cleaned (lowercase, `_` for spaces, accents stripped) and a few aliases apply (`united_states`, `ivory_coast`, `the_gambia`, `burma`, …). The outline is then fetched from geoBoundaries `gbOpen` ADM0; if that layer is missing or unreachable, the bundled 1:110m outline is used and stderr warns. `boundary_source` says which.
2. **Multi-country regions** — Natural Earth `SUBREGION` / `CONTINENT` / `REGION_UN` / `REGION_WB` labels, dissolved from the bundled 1:110m countries (offline). Short forms: `East Africa` → `Eastern Africa`, `West Africa` → `Western Africa`, `Central Africa` → `Middle Africa`. Country names win (`South Africa` is ZAF).
3. **Custom boxes** — rectangles bundled in weather-skills-core: `Indian Ocean basin` (`Indian Ocean`, `IOB`) = 30°N–40°S, 20°E–120°E.
4. **Sub-national** — geoBoundaries `gbOpen` ADM1 and ADM2, fetched per country from `https://www.geoboundaries.org/api/current/gbOpen/{ISO3}/ADM{1|2}/`. The country segment is a cleaned country name or ISO3. The remainder is matched against ADM1 `shapeName`, then ADM2 `shapeName`, then `admin1-admin2`. Hyphens inside one name stay (`kenya-elgeyo-marakwet` is ADM1 Elgeyo-Marakwet). Kenyan counties are ADM1 (47); sub-counties are ADM2.
5. **Landmarks** — one Nominatim search (`format=jsonv2`, `limit=1`, `polygon_geojson=1`). The hit's `boundingbox` gives the bbox; `--geojson` writes OSM's polygon when it has one, else the bbox rectangle. A hit smaller than 0.05° is padded by `--pad` when it is a point feature, and refused when its OSM type names an area (peninsula, region, sea, bay, state, …). Roads (`highway`) are refused.

## Country outlines

geoBoundaries draws each country as its government defines it: the USA includes Pacific and Caribbean territories (its bbox crosses the antimeridian), France is metropolitan only, Norway excludes Svalbard, and disputed areas follow the country's own claim. For part of a country (e.g. the contiguous US), pass an explicit bbox.

`iso3` comes from Natural Earth `ISO_A3` (e.g. `PSE` for Palestine). Its `-99` gaps are patched: France `FRA`, Norway `NOR`, Kosovo `XKX`, N. Cyprus `CYN`, Somaliland `SOL`.

A geometry that crosses 180° gets a wrapped bbox with west > east (RFC 7946 §5.2); a circumpolar one (Antarctica) gets `-180`/`180`.

## Licences

- Natural Earth — public domain.
- geoBoundaries — CC BY 4.0.
- OpenStreetMap Nominatim — ODbL 1.0. Public Nominatim is donated capacity ([usage policy](https://operations.osmfoundation.org/policies/nominatim/)): one request per run, no bulk lookups.
