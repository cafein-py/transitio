# Attribution and licensing

transitio itself is licensed under the MIT License.

The feed index that transitio installs with {func}`transitio.index.refresh`
is *derived data*, compiled from several open sources. Release wheels ship
without an index; `refresh` downloads a snapshot published by
[transitio-index](https://github.com/transitio-dev/transitio-index).
Redistributing the index carries the upstreams' attribution obligations, so
they are recorded here and in the `NOTICE` file shipped with the distribution.

Every built index additionally carries its **own** `NOTICE`, generated when it
is built, that records the exact versions and licences of the sources that
snapshot was built from. That per-snapshot `NOTICE` travels with the index and
is authoritative for a given snapshot; the list below names the upstreams in
general terms.

## Sources

- **Transitland Atlas** (Interline) — feed identities, URLs and declared
  licences. Licensed [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/);
  attribution required. <https://github.com/transitland/transitland-atlas>
- **Mobility Database catalog** (MobilityData) — feed identities and metadata.
  <https://mobilitydatabase.org/>
- **GBFS `systems.csv`** (MobilityData) — shared-mobility system identities,
  read by the build to keep them apart from the transit feeds; the index does
  not list them. <https://github.com/MobilityData/gbfs>
- **Overture Maps divisions** — administrative boundary geometry, provided
  under [CDLA-Permissive-2.0](https://cdla.dev/permissive-2-0/). The divisions
  theme is derived from:
  - **OpenStreetMap**, © OpenStreetMap contributors, under the
    [Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/);
    geometry derived from it is a Derived Database under that licence, and
    its share-alike and attribution terms apply.
    <https://www.openstreetmap.org/copyright>
  - **geoBoundaries**, under
    [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  - **Esri Community Maps** and **Maps Entity Variant Names**, under
    [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).

  <https://overturemaps.org/>
- **Wikidata** — place identifiers and names, dedicated to the public domain
  under [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
  <https://www.wikidata.org/>

## Metro memberships

Which metros a place belongs to was derived at build time from the sources
below; the index ships no boundary data of theirs.

- **GHS Urban Centre Database 2025** (GHS-UCDB R2024A), European Commission,
  Joint Research Centre, doi:10.2905/1a338be6-7eaf-480c-9664-3a8ade88cbcd —
  CC BY 4.0 (Commission Decision 2011/833/EU).
- **Worldwide Delineation of Multi-Tier City-Regions**, Girgin, Cattaneo,
  de By, McMenomy, Nelson and Vaz (2024), Zenodo — the FAO city-regions,
  [CC BY 4.0](https://doi.org/10.5281/zenodo.11187634).
- **Eurostat, Urban Audit functional urban areas** (2024) and **Eurostat,
  metropolitan regions** (NUTS 2021) — under the
  [Eurostat copyright notice](https://ec.europa.eu/eurostat/help/copyright-notice).
  The administrative boundaries are © EuroGeographics, under the
  [Eurostat/GISCO conditions of use](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units),
  which restrict them to non-commercial use.
- **Overture Maps divisions**, as above.

## Coverage geometry

A feed's coverage hull is derived from the feed's own data, so it is withheld
only where the feed's licence **explicitly disallows** redistribution: such a
feed keeps its row and its membership in the index, but its coverage geometry is
dropped. A feed whose licence is **unknown keeps its hull** — the conservative
choice is not to discard data whose terms are merely unresolved — and the
index's `redistribution_allowed` column records the status (true, false or
unresolved) judged for each feed, so a stricter user can filter on it. The
per-snapshot `NOTICE` records the licence judged for each feed.
