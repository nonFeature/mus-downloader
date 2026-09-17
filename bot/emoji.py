"""
bot/emoji.py
Custom Telegram emoji support with standard emoji fallbacks for mus-downloader.

Music-domain emoji mappings: audio, vinyl, track, download, check, cross,
warning, settings, quality, search, upload, queue, error.
"""

from __future__ import annotations

from typing import Optional

from aiogram import types

# ---------------------------------------------------------------------------
# Global toggle — set to False to use plain Unicode fallbacks only.
# Disabled by default: enable via set_custom_emoji_enabled(True) once the
# bot's messages/buttons are rendered with custom emoji IDs.
# ---------------------------------------------------------------------------
USE_CUSTOM_EMOJI: bool = False

__all__ = [
    "USE_CUSTOM_EMOJI",
    "set_custom_emoji_enabled",
    "is_custom_emoji_enabled",
    "e",
    "make_inline_button",
]

# ---------------------------------------------------------------------------
# Custom emoji IDs (Telegram animated emoji sticker IDs).

_ID_DOWNLOAD   = "5285387557616525663"   # ⬇️
_ID_SEARCH     = "5287402829286253493"   # 🔍
_ID_MUSIC      = "5287402824991287382"   # 🎵
_ID_VINYL      = "5287597429959467041"   # 💿
_ID_TRACK      = "5287421701372550267"   # 🎤
_ID_QUALITY_HI = "5287280899459689301"   # 🎚
_ID_QUALITY_LO = "5287483097930050600"   # 🎼
_ID_CLOCK      = "5285345441167224939"   # ⏱
_ID_CHECK      = "5251750898467649056"   # ✅
_ID_CROSS      = "5251563676548246141"   # ❌
_ID_WARNING    = "5251531176530714957"   # ⚠️
_ID_INFO       = "5251750791093464353"   # ℹ️ (справка)
_ID_IDEA       = "5251566481161888804"   # 💡
_ID_GEAR       = "5251569208466121931"   # ⚙️
_ID_WAVE       = "5251642639521981799"   # 👋
_ID_ROCKET     = "5251203324497145220"   # 🚀
_ID_HOURGLASS  = "5253819444911710732"   # ⏳
_ID_QUESTION   = "5267515172200691869"   # ❓
_ID_PRIORITY   = "5253449291745241072"   # ⚡
_ID_CALENDAR   = "5267337403504306304"   # 📅
_ID_REFRESH    = "5251394437656911929"   # 🔄

# ---------------------------------------------------------------------------
# Music-domain mappings used by the bot. Every emoji that appears in messages
# or buttons goes through this table:
#     name -> (custom emoji ID or None, standard fallback)
# When custom emoji mode is on, entries with an ID render as <tg-emoji>;
# entries with None keep the Unicode fallback until an ID is provided.
# ---------------------------------------------------------------------------
DOMAIN_EMOJI: dict[str, tuple[Optional[str], str]] = {
    # music / quality
    "audio":    (_ID_MUSIC,      "🎵"),
    "vinyl":    (_ID_VINYL,      "💿"),
    "track":    (_ID_TRACK,      "🎤"),
    "quality":  (_ID_QUALITY_HI, "🎚"),
    # actions
    "download": (_ID_DOWNLOAD,   "⬇️"),
    "upload":   (_ID_ROCKET,     "🚀"),
    "search":   (_ID_SEARCH,     "🔍"),
    "queue":    (_ID_HOURGLASS,  "⏳"),
    "settings": (_ID_GEAR,       "⚙️"),
    # status / feedback
    "check":    (_ID_CHECK,      "✅"),
    "cross":    (_ID_CROSS,      "❌"),
    "warning":  (_ID_WARNING,    "⚠️"),
    "ask":      (_ID_QUESTION,   "❓"),
    "wave":     (_ID_WAVE,       "👋"),
    "help":     (_ID_INFO,       "ℹ️"),
    "bolt":     (_ID_PRIORITY,   "⚡"),
    "bulb":     (_ID_IDEA,       "💡"),
    "calendar": (_ID_CALENDAR,   "📅"),
    "clock":    (_ID_CLOCK,      "⏱"),
}
# Backward/alias access, e.g. ``from bot.emoji import ID_AUDIO``.
ID_AUDIO = DOMAIN_EMOJI["audio"][0]
ID_VINYL = DOMAIN_EMOJI["vinyl"][0]
ID_TRACK = DOMAIN_EMOJI["track"][0]
ID_DOWNLOAD = DOMAIN_EMOJI["download"][0]
ID_CHECK = DOMAIN_EMOJI["check"][0]
ID_CROSS = DOMAIN_EMOJI["cross"][0]
ID_WARNING = DOMAIN_EMOJI["warning"][0]
ID_SETTINGS = DOMAIN_EMOJI["settings"][0]
ID_QUALITY = DOMAIN_EMOJI["quality"][0]
ID_ASK = DOMAIN_EMOJI["ask"][0]
ID_SEARCH = DOMAIN_EMOJI["search"][0]
ID_QUEUE = DOMAIN_EMOJI["queue"][0]
ID_UPLOAD = DOMAIN_EMOJI["upload"][0]
ID_WAVE = DOMAIN_EMOJI["wave"][0]
ID_REFRESH = _ID_REFRESH


def set_custom_emoji_enabled(enabled: bool) -> None:
    """Enable or disable custom <tg-emoji> rendering globally."""
    global USE_CUSTOM_EMOJI
    USE_CUSTOM_EMOJI = bool(enabled)


def is_custom_emoji_enabled() -> bool:
    return USE_CUSTOM_EMOJI


def e(emoji_id: Optional[str], fallback: str) -> str:
    """
    Return a <tg-emoji emoji-id="…">fallback</tg-emoji> tag when custom emoji
    is enabled, or just the plain fallback string otherwise.

    Usage:
        e(_ID_CHECK, "✅")  →  '<tg-emoji emoji-id="5233491…">✅</tg-emoji>'
    """
    if USE_CUSTOM_EMOJI and emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


def emoji_kwargs() -> dict[str, str]:
    """
    Placeholder values for localisation strings:
    ``{emoji_audio}``, ``{emoji_vinyl}``, ``{emoji_check}``, …

    ``bot.i18n.t()`` injects these automatically, so message templates
    render custom emoji when enabled and plain Unicode fallbacks otherwise.
    """
    return {f"emoji_{name}": e(emoji_id, fallback) for name, (emoji_id, fallback) in DOMAIN_EMOJI.items()}


def make_inline_button(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    emoji_id: Optional[str] = None,
    fallback_emoji: str = "",
    style: Optional[str] = None,
    **kwargs: object,
) -> types.InlineKeyboardButton:
    """
    Build an InlineKeyboardButton with optional custom emoji icon.

    When USE_CUSTOM_EMOJI is True and emoji_id is provided, sets
    icon_custom_emoji_id on the button. Otherwise prepends the fallback emoji
    to the label. ``style`` and extra kwargs are forwarded to aiogram as-is.
    """
    button_kwargs: dict[str, object] = {
        "text": text,
        "callback_data": callback_data,
        "url": url,
    }
    if style is not None:
        button_kwargs["style"] = style
    button_kwargs.update(kwargs)

    if USE_CUSTOM_EMOJI and emoji_id:
        return types.InlineKeyboardButton(icon_custom_emoji_id=emoji_id, **button_kwargs)  # type: ignore[arg-type]
    label = f"{fallback_emoji} {text}".strip() if fallback_emoji else text
    button_kwargs["text"] = label
    return types.InlineKeyboardButton(**button_kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _EmojiAccessor — property-based accessor with lazy rendering
# ---------------------------------------------------------------------------

class _EmojiAccessor:
    """Provides named emoji properties that render on access."""

    # Status / feedback
    @property
    def CHECK(self) -> str:      return e(_ID_CHECK,      "✅")
    @property
    def CROSS(self) -> str:      return e(_ID_CROSS,      "❌")
    @property
    def WARNING(self) -> str:    return e(_ID_WARNING,    "⚠️")
    @property
    def REFRESH(self) -> str:    return e(_ID_REFRESH,    "🔄")
    @property
    def INFO(self) -> str:       return e(_ID_INFO,       "ℹ️")
    @property
    def IDEA(self) -> str:       return e(_ID_IDEA,       "💡")
    @property
    def WAVE(self) -> str:       return e(_ID_WAVE,       "👋")
    @property
    def CALENDAR(self) -> str:   return e(_ID_CALENDAR,   "📅")

    # Music domain
    @property
    def MUSIC(self) -> str:      return e(_ID_MUSIC,      "🎵")
    @property
    def VINYL(self) -> str:      return e(_ID_VINYL,      "💿")
    @property
    def TRACK(self) -> str:      return e(_ID_TRACK,      "🎤")

    # Actions
    @property
    def DOWNLOAD(self) -> str:   return e(_ID_DOWNLOAD,   "⬇️")
    @property
    def UPLOAD(self) -> str:     return e(_ID_ROCKET,     "🚀")
    @property
    def SEARCH(self) -> str:     return e(_ID_SEARCH,     "🔍")
    @property
    def QUEUE(self) -> str:      return e(_ID_HOURGLASS,  "⏳")
    @property
    def SETTINGS(self) -> str:   return e(_ID_GEAR,       "⚙️")
    @property
    def PRIORITY(self) -> str:   return e(_ID_PRIORITY,   "⚡")

    # Quality levels
    @property
    def QUALITY_HI(self) -> str: return e(_ID_QUALITY_HI, "🎚")
    @property
    def QUALITY_LO(self) -> str: return e(_ID_QUALITY_LO, "🎼")
    @property
    def ASK(self) -> str:        return e(_ID_QUESTION,   "❓")


_accessor = _EmojiAccessor()


def __getattr__(name: str) -> str:
    """Module-level __getattr__ so callers can do: from bot.emoji import CHECK."""
    if hasattr(_accessor, name):
        return getattr(_accessor, name)
    raise AttributeError(f"module 'bot.emoji' has no attribute {name!r}")
