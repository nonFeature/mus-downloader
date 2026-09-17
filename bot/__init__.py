"""
bot package
Telegram-bot implementation for mus-downloader.

The full implementation lives in ``bot/bot.py``. The package module is replaced
by that module on import, so existing code and tests can keep using
``import bot`` with attribute access (``bot.user_settings``, ``bot.router`` …)
and ``unittest.mock.patch("bot.<name>")`` targets.
"""

import sys
from pathlib import Path

from bot import bot as _impl

# Keep ``python -m bot`` (bot/__main__.py) and ``bot.<submodule>`` imports
# working after the package is swapped for the implementation module.
_impl.__path__ = [str(Path(__file__).resolve().parent)]
if _impl.__spec__ is not None:
    _impl.__spec__.submodule_search_locations = list(_impl.__path__)

# Re-attach submodules to the implementation module so that imports like
# ``from bot import emoji`` and ``import bot.storage`` keep resolving after
# the swap below (importlib sets those attributes on the original package
# object, which is replaced here).
from . import (  # noqa: E402,F401
    emoji,
    i18n,
    keyboards,
    local_bot_api,
    notifier,
    process_lock,
    storage,
)

for _submodule in (emoji, i18n, keyboards, local_bot_api, notifier, process_lock, storage):
    setattr(_impl, _submodule.__name__.rsplit(".", 1)[-1], _submodule)
del _submodule

sys.modules[__name__] = _impl
