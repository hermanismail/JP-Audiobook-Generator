"""
JP Audiobook Generator - Shared UI tokens & widgets
------------------------------------------------------
Design tokens and small reusable widgets shared between gui_settings.py
and progress_window.py.

This module exists specifically to avoid a circular import: gui_settings.py
needs to import ProgressWindow (Save & Run launches it) and
progress_window.py needs these same design tokens/widgets (so its window
visually matches gui_settings.py). If either module imported directly from
the other, Python's import system would deadlock on the cycle, since each
would need the other to finish loading first. Both import from here
instead; neither imports the other.
"""

import customtkinter as ctk

# Core palette - shared by every window in the app. gui_settings.py also
# defines a few additional tokens only it uses (sidebar colors, page icon
# tuples), which stay local to gui_settings.py rather than living here.
COLOR_BG = "#F7F7FA"
COLOR_CARD = "#FFFFFF"
COLOR_CARD_BORDER = "#E7E7EC"
COLOR_TITLE = "#17171C"
COLOR_SUBTITLE = "#8B8B94"
COLOR_ENTRY_BORDER = "#E2E2E8"
COLOR_ENTRY_TEXT = "#3A3A42"
COLOR_ACCENT = "#6C5DD3"
COLOR_ACCENT_HOVER = "#5B4FC0"
COLOR_TOGGLE_ON = "#34C773"
COLOR_BTN_NEUTRAL_BORDER = "#D8D8DE"
COLOR_BTN_NEUTRAL_TEXT = "#3A3A42"


class IconBadge(ctk.CTkFrame):
    """Rounded colored square with a centered glyph/emoji or short text."""

    def __init__(self, parent, glyph, bg_color, text_color="white",
                 size=44, font_size=18, corner_radius=12, **kwargs):
        super().__init__(parent, width=size, height=size, corner_radius=corner_radius,
                          fg_color=bg_color, **kwargs)
        self.pack_propagate(False)
        ctk.CTkLabel(self, text=glyph, text_color=text_color,
                     font=ctk.CTkFont(size=font_size, weight="bold")).place(
            relx=0.5, rely=0.5, anchor="center")
