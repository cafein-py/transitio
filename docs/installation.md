# Installation

`transitio` requires Python 3.10 or newer.

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

### Mobility Database API token

The [Mobility Database](https://mobilitydatabase.org/) catalogues public
transport feeds worldwide. With a free Mobility Database refresh token,
`transitio` can also read the dated versions of a feed from it. Pass the
token as `refresh_token=`, or set it in the `MOBILITY_API_REFRESH_TOKEN`
environment variable. You need a token for:

- a feed's dataset versions (`MobilityDatabase.datasets`, `dataset_for` and
  `datasets_for`);
- the checksum-verified downloads and hosted canonical-validator reports of
  those versions (`download`, `validation_report`);
- `compare_feed_history`;
- the choice of a historical Mobility Database dataset by `fetch(when=...)`.

Without a token, `MobilityDatabase.search_feeds` and `MobilityDatabase.feed`
read the public CSV export of the catalogue, and `download_latest` fetches
the latest hosted feed zip, which is not checksum-verified and can change at
any time.

### Feed index

`transitio.index.refresh()` installs the newest feed index. `fetch(place=...)`
needs it, and `fetch(aoi=...)` takes from it the feeds of the index places
covering the area. Without an index, `fetch(aoi=...)` searches the Mobility
Database catalogue by bounding box, with a warning.

### Feed credentials

Some feeds need a free account with their provider.
`IndexedFeed.access_instructions()` names the provider, where to register and
the credential fields it issues. You can store the credentials in two ways:

- with `transitio.credentials.set("<provider>", {"<field>": "..."})`, which
  writes `credentials.toml` in your user config directory;
- with one environment variable per field,
  `TRANSITIO_KEY_<PROVIDER>__<FIELD>` (upper case, with `-` written as `_`).

A variable wins over the file. On Windows there is no credentials file, and
the environment variables are the only store. Elsewhere, the file is
readable only by you (mode 0600, in a directory created with mode 0700), and
an existing directory must be yours and must not be writable by group or
others.

`fetch` sends a feed's credentials only to the origin of the feed's access
URL. It keeps them out of the reasons, paths, sidecars and reports it writes,
and out of the `httpx` log records. A server that echoes a credential into a
response body puts it in the cache, and one that echoes it into a response
header shows in `httpcore` debug logging.

### cafein

[cafein](https://github.com/cafein-py/cafein) is needed only for
`FetchResult.to_cafein()`.

## Verifying the installation

```python
import transitio

print(transitio.__version__)
```
