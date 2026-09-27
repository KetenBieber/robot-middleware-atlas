project = "Robot Middleware Source Tutorials"
author = "Robot Middleware Source Atlas"
release = "2026.09"

extensions = [
    'sphinxcontrib.mermaid',
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_rtd_theme",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}
master_doc = "index"
language = "zh_CN"
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "generated/audit.md",
    "sphinx_reconstruction_review.md",
    "reconstruction_audit.md",
    "method.rst",
    "generated/*/reconstruction.md",
]
pygments_style = "sphinx"

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    "collapse_navigation": False,
    "sticky_navigation": True,
    "navigation_depth": -1,
    "prev_next_buttons_location": "bottom",
    "style_external_links": True,
}
html_title = "Robot Middleware Source Tutorials"
html_show_sourcelink = True
html_copy_source = True
html_search_language = "zh"
html_show_search_summary = False
html_static_path = ["_static"]
html_css_files = ["custom.css"]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "tasklist",
]
myst_heading_anchors = 4
copybutton_exclude = ".linenos, .gp, .go"
