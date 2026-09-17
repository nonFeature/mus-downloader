"""
bot/keyboards.py
Inline keyboard builders for mus-downloader.

Labels come from the i18n engine; icons come from the custom emoji system
(``make_inline_button``): a custom ``icon_custom_emoji_id`` when enabled, or a
plain Unicode fallback emoji prepended to the label otherwise.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from .emoji import ID_ASK, ID_AUDIO, ID_VINYL, emoji_kwargs, make_inline_button
from .i18n import t

__all__ = [
    "build_quality_keyboard",
    "build_quality_settings_keyboard",
]


def build_quality_keyboard(qid: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """
    Inline keyboard for choosing download quality of a specific track.

    Callback data format: ``dl:<qid>:MP3`` / ``dl:<qid>:FLAC``
    """
    emj = emoji_kwargs()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_inline_button(
                    text=t("btn.mp3", lang),
                    callback_data=f"dl:{qid}:MP3",
                    emoji_id=ID_AUDIO,
                    fallback_emoji=emj["emoji_audio"],
                ),
                make_inline_button(
                    text=t("btn.flac", lang),
                    callback_data=f"dl:{qid}:FLAC",
                    emoji_id=ID_VINYL,
                    fallback_emoji=emj["emoji_vinyl"],
                ),
            ]
        ]
    )


def build_quality_settings_keyboard(
    current_pref: str, lang: str = "ru"
) -> InlineKeyboardMarkup:
    """
    Inline keyboard for /quality settings, marks the currently active choice.

    Callback data format: ``pref:MP3`` / ``pref:FLAC`` / ``pref:ASK``
    """
    emj = emoji_kwargs()
    mark = lambda chosen: " ✓" if current_pref == chosen else ""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_inline_button(
                    text=f"{t('quality.mp3', lang)}{mark('MP3')}",
                    callback_data="pref:MP3",
                    emoji_id=ID_AUDIO,
                    fallback_emoji=emj["emoji_audio"],
                ),
                make_inline_button(
                    text=f"{t('quality.flac', lang)}{mark('FLAC')}",
                    callback_data="pref:FLAC",
                    emoji_id=ID_VINYL,
                    fallback_emoji=emj["emoji_vinyl"],
                ),
            ],
            [
                make_inline_button(
                    text=f"{t('quality.ask', lang)}{mark('ASK')}",
                    callback_data="pref:ASK",
                    emoji_id=ID_ASK,
                    fallback_emoji=emj["emoji_ask"],
                ),
            ],
        ]
    )
