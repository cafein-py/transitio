"""Write the table of the languages each country uses, which place lookup reads.

For each territory Unicode CLDR lists, the base codes (``sr`` for ``sr_Latn``)
of its official, de facto official and official regional languages, as the
installed babel carries them (babel 2.18 carries CLDR 47); babel is needed
only here. The table is written to
``python/transitio/index/country_languages.json``; a territory without such a
language is left out.

Usage::

    python scripts/country_languages.py
"""

import json
from pathlib import Path

from babel.core import get_cldr_version, get_global
from babel.languages import get_official_languages

TARGET = (
    Path(__file__).resolve().parent.parent
    / "python"
    / "transitio"
    / "index"
    / "country_languages.json"
)


def country_languages():
    """``{country code: [base language codes]}``, sorted by code."""
    table = {}
    for code in sorted(get_global("territory_languages")):
        languages = get_official_languages(code, regional=True, de_facto=True)
        bases = dict.fromkeys(language.split("_")[0].lower() for language in languages)
        if bases:
            table[code] = list(bases)
    return table


def main():
    table = country_languages()
    rows = [f"{json.dumps(code)}: {json.dumps(bases)}" for code, bases in table.items()]
    TARGET.write_text("{\n" + ",\n".join(rows) + "\n}\n", encoding="utf-8")
    print(f"{len(table)} countries from CLDR {get_cldr_version()} -> {TARGET}")


if __name__ == "__main__":
    main()
