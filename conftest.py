"""
Pytest bootstrap for mus-downloader.

Importing ``core`` registers backward-compatible sys.modules aliases for the
modules that moved into the downloader core package (``core.metadata``,
``core.tagger``, ``core.sources.*``). This keeps legacy import paths such as
``import metadata`` / ``from sources.youtube import ...`` working during test
collection.
"""

import core  # noqa: F401
