# Configuration file for the Sphinx documentation builder.
#
# For the full list of options see:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

from datetime import datetime
from importlib.metadata import version as _package_version

current_year = datetime.now().year

project = "transitio"
copyright = f"2026-{current_year}, Henrikki Tenkanen + transitio contributors"
author = "Henrikki Tenkanen + transitio contributors"

# autodoc imports the installed transitio (Read the Docs runs
# `pip install .`), so the compiled `_core` is present and the version stays
# single-sourced from the Cargo workspace via the package metadata.
release = _package_version("transitio")
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    # Generate the API reference as category tables with one-line summaries
    # and a page per class/function (see reference.rst).
    "sphinx.ext.autosummary",
    # NumPy-style docstrings.
    "sphinx.ext.napoleon",
    "myst_parser",
]

# Generate the per-object stub pages referenced by the autosummary tables.
autosummary_generate = True

# Enable MyST's colon-fence syntax (:::{admonition} ... :::) in the
# Markdown pages.
myst_enable_extensions = ["colon_fence"]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- HTML output -------------------------------------------------------------

html_theme = "sphinx_book_theme"
html_title = ""

html_theme_options = {
    "repository_url": "https://github.com/cafein-py/transitio/",
    "repository_branch": "main",
    "path_to_docs": "docs/",
    "use_edit_page_button": True,
    "use_repository_button": True,
}

master_doc = "index"

html_static_path = ["_static"]

pygments_style = "sphinx"
