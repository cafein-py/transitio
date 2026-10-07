# Installation

transitio requires Python >= 3.10.

## From PyPI

```
pip install transitio
```

Binary wheels ship for Linux, macOS and Windows, so no Rust toolchain is
needed. The `notebook` extra adds ipywidgets, so the download progress bars
of `fetch` and `transitio.index.refresh` show as widgets in Jupyter:

```
pip install "transitio[notebook]"
```

## From source

A source build compiles the Rust core, so a [Rust
toolchain](https://rustup.rs/) must be installed:

```
git clone https://github.com/cafein-py/transitio.git
cd transitio
pip install .
```

## Optional pieces

- **Mobility Database API token** — a free
  [Mobility Database](https://mobilitydatabase.org/) refresh token, passed as
  `refresh_token=` or set in the `MOBILITY_API_REFRESH_TOKEN` environment
  variable. A token is needed for a feed's dataset versions
  (`MobilityDatabase.datasets`, `dataset_for` and `datasets_for`), the
  checksum-verified versioned downloads and hosted canonical-validator
  reports that start from them (`download`, `validation_report`),
  `compare_feed_history`, and the choice of historical Mobility Database
  datasets by `fetch(when=...)`. Without one, `MobilityDatabase.search_feeds`
  and `MobilityDatabase.feed` read the public CSV catalogue export, and
  `download_latest` fetches the latest hosted feed zip (an unverified moving
  target).
- **Feed index** — `transitio.index.refresh()` installs the newest feed
  index. `fetch(place=...)` needs it, and `fetch(aoi=...)` takes the feeds
  of the index places covering the area from it; without one, `fetch(aoi=...)`
  searches the Mobility Database catalogue by bounding box, with a warning.
- **Feed credentials** — some feeds need a free account with their
  provider. `IndexedFeed.access_instructions()` names the provider, where to
  register and the credential fields it issues. Store them with
  `transitio.credentials.set("<provider>", {"<field>": "..."})`, which writes
  `credentials.toml` in the user config directory (mode 0600, in a
  directory created 0700; an existing directory must be yours and not
  writable by group or others), or set one environment variable per field,
  `TRANSITIO_KEY_<PROVIDER>__<FIELD>` (upper case, `-` as `_`); a variable
  wins over the file. On Windows there is no credentials file and the
  environment variables are the only store. `fetch` sends a feed's
  credentials to the origin of its access URL alone and keeps them
  out of the reasons, paths, sidecars and reports it writes and out of the
  `httpx` log records; a server that echoes one into a response body puts
  it in the cache, and one echoed into a response header shows in
  `httpcore` debug logging.
- **cafein** — needed only for `FetchResult.to_cafein()`.

## Verifying the installation

```python
import transitio

print(transitio.__version__)
```
