# Attribution and licensing

transitio itself is licensed under the MIT License.

The feed index that transitio bundles in the wheel and downloads with
{func}`transitio.index.refresh` is *derived data*, compiled from several open
sources. Redistributing it carries the upstreams' attribution obligations, so
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
- **GBFS `systems.csv`** (MobilityData) — shared-mobility system identities.
  <https://github.com/MobilityData/gbfs>
- **Overture Maps divisions** — administrative boundary geometry. The divisions
  theme includes content derived from **OpenStreetMap**, © OpenStreetMap
  contributors, available under the
  [Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/);
  its share-alike and attribution terms apply to the boundary geometry the
  index ships. <https://overturemaps.org/> ·
  <https://www.openstreetmap.org/copyright>
- **Wikidata** — place identifiers and names, dedicated to the public domain
  under [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
  <https://www.wikidata.org/>

## Coverage geometry

A feed's coverage hull is derived from the feed's own data, so it is withheld
only where the feed's licence **explicitly disallows** redistribution: such a
feed keeps its row and its membership in the index, but its coverage geometry is
dropped. A feed whose licence is **unknown keeps its hull** — the conservative
choice is not to discard data whose terms are merely unresolved — and the
index's `redistribution_allowed` column records the status (true, false or
unresolved) judged for each feed, so a stricter user can filter on it. The
per-snapshot `NOTICE` records the licence judged for each feed.
