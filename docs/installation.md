# Installation

transitio requires Python >= 3.10.

## From PyPI

```
pip install transitio
```

Binary wheels ship for Linux, macOS and Windows, so no Rust toolchain is
needed.

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
  [Mobility Database](https://mobilitydatabase.org/) refresh token unlocks
  historical dataset selection, checksum-verified versioned downloads and the
  hosted canonical-validator reports. Pass it as `refresh_token=` or set the
  `MOBILITY_API_REFRESH_TOKEN` environment variable. Without one, transitio
  transparently falls back to the public CSV catalogue and the latest hosted
  feed zips (unverified moving targets).
- **Feed credentials** — some feeds need a free account with their
  provider. `IndexedFeed.access_instructions()` names the provider, where to
  register and the credential fields it issues. Store them with
  `transitio.credentials.set("<provider>", {"<field>": "..."})`, which writes
  `credentials.toml` in the user config directory (mode 0600, in a
  directory created 0700; an existing directory must be yours and not
  writable by group or others), or set one environment variable per field,
  `TRANSITIO_KEY_<PROVIDER>__<FIELD>` (upper case, `-` as `_`); a variable
  wins over the file. On Windows there is no credentials file and the
  environment variables are the only store.
- **cafein** — needed only for `FetchResult.to_cafein()`.

## Verifying the installation

```python
import transitio

print(transitio.__version__)
```
