import json
import os

import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
from gi.repository import Adw, Gtk, Gdk, GLib, Gio, GObject

from .constants import (APP_CSS, APP_ID, AR_FONT_FAMILY, DATA_DIR,
                        FONTS_DIR, HAS_TEXT_DB, PAGES_DIR, SURAHS, SURAH_BY_NUM)
from .db import BASMALA, SURAH_FIRST_PAGE, load_ayahs, search_ayahs


_ARABIC_DIGITS = str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩')

def _to_arabic_digits(n: int) -> str:
    return str(n).translate(_ARABIC_DIGITS)


class AyahItem(GObject.Object):
    __gtype_name__ = 'AyahItem'

    def __init__(self, surah_num, ayah_num, arabic, english,
                 is_basmala=False, surah_name=''):
        super().__init__()
        self.surah_num  = surah_num
        self.ayah_num   = ayah_num
        self.arabic     = arabic
        self.english    = english
        self.is_basmala = is_basmala
        self.surah_name = surah_name  # non-empty for search results


class QuranBrowser(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id='io.github.hihebark.QuranReader',
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.lang           = 'ar'
        self.mode           = 'mushaf'
        self.current_page   = 1
        self.current_surah  = None
        self._filtered      = list(SURAHS)
        self.font_size      = 22
        self._search_active = False
        self._bookmark_set  = set()
        self._bookmarks     = []   # [[surah_num, ayah_num], ...]
        self._color_scheme  = 'auto'

        state_dir = os.path.join(GLib.get_user_data_dir(), APP_ID)
        os.makedirs(state_dir, exist_ok=True)
        self._state_file     = os.path.join(state_dir, 'state.json')
        self._bookmarks_file = os.path.join(state_dir, 'bookmarks.json')
        self._load_bookmarks()

    # ------------------------------------------------------------------ lifecycle

    @staticmethod
    def _register_fonts():
        """Register each bundled TTF with fontconfig, then refresh Pango."""
        if not os.path.isdir(FONTS_DIR):
            return
        try:
            import ctypes
            fc = ctypes.CDLL("libfontconfig.so.1")
            for fname in os.listdir(FONTS_DIR):
                if fname.lower().endswith(('.ttf', '.otf')):
                    path = os.path.join(FONTS_DIR, fname).encode()
                    fc.FcConfigAppFontAddFile(None, path)
        except Exception as e:
            print(f"Font registration failed: {e}")
        try:
            gi.require_version('PangoCairo', '1.0')
            from gi.repository import PangoCairo
            PangoCairo.FontMap.get_default().changed()
        except Exception as e:
            print(f"Pango refresh failed: {e}")

    def do_activate(self):
        self._register_fonts()
        display = Gdk.Display.get_default()

        provider = Gtk.CssProvider()
        provider.load_from_data(APP_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.IconTheme.get_for_display(display).add_search_path(
            os.path.join(DATA_DIR, "icons")
        )

        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("Quran Browser")
        self.window.set_default_size(1020, 760)
        self.window.connect("close-request", self._on_close_request)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self._build_header())

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_vexpand(True)
        self.paned.set_position(270)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)
        self.paned.set_start_child(self._build_sidebar())
        self.paned.set_end_child(self._build_content())
        self.paned.set_direction(Gtk.TextDirection.RTL)
        root.append(self.paned)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.window.add_controller(key_ctrl)

        self._theme_btn.set_icon_name(self._SCHEME_ICONS[self._color_scheme])
        self.window.set_content(root)
        self.window.present()
        self._restore_state()

    # ------------------------------------------------------------------ header

    def _build_header(self):
        header = Adw.HeaderBar()

        # Mode toggle
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        mode_box.add_css_class("linked")
        self.btn_mushaf = Gtk.ToggleButton(label="Mushaf")
        self.btn_text   = Gtk.ToggleButton(label="Text")
        self.btn_text.set_group(self.btn_mushaf)
        self.btn_mushaf.set_active(True)
        if not HAS_TEXT_DB:
            self.btn_text.set_sensitive(False)
            self.btn_text.set_tooltip_text("Run scripts/build_text_db.py first")
        self.btn_mushaf.connect("toggled", self._on_mode_toggled)
        self.btn_text.connect("toggled", self._on_mode_toggled)
        mode_box.append(self.btn_mushaf)
        mode_box.append(self.btn_text)
        header.pack_start(mode_box)

        # Bookmarks popover
        self._bookmarks_listbox = Gtk.ListBox()
        self._bookmarks_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._bookmarks_listbox.connect("row-activated", self._on_bookmark_row_activated)
        self._update_bookmarks_ui()

        bm_scroll = Gtk.ScrolledWindow()
        bm_scroll.set_min_content_height(80)
        bm_scroll.set_max_content_height(300)
        bm_scroll.set_min_content_width(220)
        bm_scroll.set_child(self._bookmarks_listbox)

        self._bookmarks_popover = Gtk.Popover()
        self._bookmarks_popover.set_child(bm_scroll)

        btn_bm = Gtk.MenuButton()
        btn_bm.set_icon_name("bookmark-new-symbolic")
        btn_bm.set_tooltip_text("Bookmarks")
        btn_bm.set_popover(self._bookmarks_popover)
        header.pack_end(btn_bm)

        # Language toggle
        lang_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        lang_box.add_css_class("linked")
        self.btn_ar = Gtk.ToggleButton(label="AR")
        self.btn_en = Gtk.ToggleButton(label="EN")
        self.btn_en.set_group(self.btn_ar)
        self.btn_ar.set_active(True)
        self.btn_ar.connect("toggled", self._on_lang_toggled)
        self.btn_en.connect("toggled", self._on_lang_toggled)
        lang_box.append(self.btn_ar)
        lang_box.append(self.btn_en)
        header.pack_end(lang_box)

        # Night mode cycle button
        self._theme_btn = Gtk.Button()
        self._theme_btn.set_tooltip_text("Toggle color scheme")
        self._theme_btn.connect("clicked", self._on_theme_clicked)
        header.pack_end(self._theme_btn)

        return header

    # ------------------------------------------------------------------ sidebar

    def _build_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        search = Gtk.SearchEntry()
        search.set_placeholder_text("Search surah...")
        search.set_margin_top(10)
        search.set_margin_bottom(6)
        search.set_margin_start(10)
        search.set_margin_end(10)
        search.connect("search-changed", self._on_search_changed)
        box.append(search)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        self.surah_listbox = Gtk.ListBox()
        self.surah_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.surah_listbox.connect("row-activated", self._on_surah_activated)
        scrolled.set_child(self.surah_listbox)
        box.append(scrolled)

        self._populate_surah_list(SURAHS)
        return box

    def _populate_surah_list(self, surahs):
        while child := self.surah_listbox.get_first_child():
            self.surah_listbox.remove(child)
        for num, ar, en, trans, count in surahs:
            if self.lang == 'ar':
                title    = f"{ar} .{num}"
                subtitle = f"آيات {count} · {en}"
            else:
                title    = f"{num}. {en}"
                subtitle = f"{trans} · {count} ayat"

            title_lbl = Gtk.Label(label=title)
            title_lbl.set_halign(Gtk.Align.END)
            title_lbl.set_xalign(1.0)
            title_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END

            sub_lbl = Gtk.Label(label=subtitle)
            sub_lbl.set_halign(Gtk.Align.END)
            sub_lbl.set_xalign(1.0)
            sub_lbl.set_ellipsize(3)
            sub_lbl.add_css_class("dim-label")
            sub_lbl.add_css_class("caption")

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            vbox.set_margin_top(8)
            vbox.set_margin_bottom(8)
            vbox.set_margin_start(12)
            vbox.set_margin_end(12)
            vbox.append(title_lbl)
            vbox.append(sub_lbl)

            row = Gtk.ListBoxRow()
            row.set_child(vbox)
            row.surah_number = num
            self.surah_listbox.append(row)

    # ------------------------------------------------------------------ content stack

    def _build_content(self):
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_vexpand(True)
        self.content_stack.set_hexpand(True)
        self.content_stack.add_named(self._build_mushaf_view(), "mushaf")
        self.content_stack.add_named(self._build_text_view(),   "text")
        return self.content_stack

    # ------------------------------------------------------------------ mushaf view

    def _build_mushaf_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("mushaf-page")

        self.page_picture = Gtk.Picture()
        self.page_picture.set_vexpand(True)
        self.page_picture.set_hexpand(True)
        self.page_picture.set_can_shrink(True)
        self.page_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        box.append(self.page_picture)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        nav.set_halign(Gtk.Align.CENTER)
        nav.set_margin_top(8)
        nav.set_margin_bottom(12)

        self.btn_prev = Gtk.Button(label="←")
        self.btn_prev.connect("clicked", lambda _: self._go_to_page(self.current_page - 1))
        nav.append(self.btn_prev)

        self.page_label = Gtk.Label()
        self.page_label.set_width_chars(10)
        nav.append(self.page_label)

        self.btn_next = Gtk.Button(label="→")
        self.btn_next.connect("clicked", lambda _: self._go_to_page(self.current_page + 1))
        nav.append(self.btn_next)

        box.append(nav)
        return box

    def _go_to_page(self, page: int):
        page = max(1, min(604, page))
        self.current_page = page
        path = os.path.join(PAGES_DIR, f"{page:03d}.svg")
        self.page_picture.set_file(
            Gio.File.new_for_path(path) if os.path.exists(path) else None
        )
        self.page_label.set_text(f"{page} / 604")
        self.btn_prev.set_sensitive(page > 1)
        self.btn_next.set_sensitive(page < 604)

    # ------------------------------------------------------------------ text view

    def _build_text_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Title row: surah name on the left, font size control on the right
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_row.set_margin_top(10)
        title_row.set_margin_bottom(10)
        title_row.set_margin_start(12)
        title_row.set_margin_end(12)

        font_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        font_box.add_css_class("linked")
        font_box.set_valign(Gtk.Align.CENTER)
        btn_smaller = Gtk.Button(label="A−")
        btn_smaller.set_tooltip_text("Decrease font size")
        btn_smaller.connect("clicked", lambda _: self._on_font_size_changed(-2))
        btn_larger = Gtk.Button(label="A+")
        btn_larger.set_tooltip_text("Increase font size")
        btn_larger.connect("clicked", lambda _: self._on_font_size_changed(+2))
        font_box.append(btn_smaller)
        font_box.append(btn_larger)
        title_row.append(font_box)

        self.text_title = Gtk.Label()
        self.text_title.set_markup("<span size='large'>Select a Surah</span>")
        self.text_title.set_hexpand(True)
        self.text_title.set_halign(Gtk.Align.END)
        title_row.append(self.text_title)

        box.append(title_row)
        box.append(Gtk.Separator())

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search ayahs…")
        self._search_entry.set_margin_start(12)
        self._search_entry.set_margin_end(12)
        self._search_entry.set_margin_top(8)
        self._search_entry.set_margin_bottom(4)
        self._search_entry.connect("search-changed", self._on_text_search_changed)
        box.append(self._search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)

        self.ayah_store = Gio.ListStore(item_type=AyahItem)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup",  self._on_ayah_setup)
        factory.connect("bind",   self._on_ayah_bind)
        factory.connect("unbind", self._on_ayah_unbind)

        self.ayah_listview = Gtk.ListView(
            model=Gtk.NoSelection(model=self.ayah_store),
            factory=factory,
        )
        self.ayah_listview.set_margin_start(12)
        self.ayah_listview.set_margin_end(12)
        self.ayah_listview.set_margin_top(4)
        self.ayah_listview.set_margin_bottom(12)
        scrolled.set_child(self.ayah_listview)
        box.append(scrolled)

        return box

    def _load_text(self, surah_number: int):
        self.ayah_store.remove_all()

        s = SURAH_BY_NUM[surah_number]
        if self.lang == 'ar':
            self.text_title.set_markup(
                f"<span size='x-large' font_weight='bold'>{s[1]}</span>"
                f"  <span size='small' foreground='gray'>{s[2]}</span>"
            )
        else:
            self.text_title.set_markup(
                f"<span size='x-large' font_weight='bold'>{s[2]}</span>"
                f"  <span size='small' foreground='gray'>{s[3]}</span>"
            )

        rows = load_ayahs(surah_number)
        if not rows:
            return

        items = []
        if surah_number not in (1, 9) and rows[0][1].startswith(BASMALA):
            items.append(AyahItem(surah_number, 0, '', '', is_basmala=True))
            n, ar, en = rows[0]
            rows[0] = (n, ar[len(BASMALA):].lstrip(), en)

        for n, ar, en in rows:
            items.append(AyahItem(surah_number, n, ar, en))

        self.ayah_store.splice(0, 0, items)

    # ------------------------------------------------------------------ ayah factory

    def _on_ayah_setup(self, _factory, list_item):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)

        # Bookmark icon — top-left, hidden by default
        bm_icon = Gtk.Image.new_from_icon_name("user-bookmarks-symbolic")
        bm_icon.set_halign(Gtk.Align.START)
        bm_icon.set_valign(Gtk.Align.START)
        bm_icon.add_css_class("accent")
        bm_icon.set_pixel_size(14)
        outer.append(bm_icon)

        # Reference header shown for search results
        ref_label = Gtk.Label()
        ref_label.set_halign(Gtk.Align.START)
        ref_label.add_css_class("dim-label")
        outer.append(ref_label)

        basmala_label = Gtk.Label()
        basmala_label.set_halign(Gtk.Align.CENTER)
        outer.append(basmala_label)

        ar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        ar_label = Gtk.Label()
        ar_label.set_wrap(True)
        ar_label.set_wrap_mode(Gtk.WrapMode.WORD)
        ar_label.set_selectable(True)
        ar_label.set_hexpand(True)
        ar_label.set_halign(Gtk.Align.FILL)
        ar_label.set_direction(Gtk.TextDirection.RTL)
        ar_label.set_xalign(0.0)   # 0 = start edge; in RTL start = right
        ar_label.set_justify(Gtk.Justification.LEFT)  # LEFT = start = right in RTL
        ar_row.append(ar_label)
        outer.append(ar_row)

        en_label = Gtk.Label()
        en_label.set_wrap(True)
        en_label.set_wrap_mode(Gtk.WrapMode.WORD)
        en_label.set_selectable(True)
        en_label.set_xalign(0.0)
        en_label.set_hexpand(True)
        en_label.add_css_class("dim-label")
        en_label.add_css_class("ayah-english")
        outer.append(en_label)

        gesture = Gtk.GestureClick(button=3)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_ayah_right_click)
        outer.add_controller(gesture)

        outer._refs = (bm_icon, ref_label, basmala_label, ar_row, ar_label, en_label)
        outer._item = None
        list_item.set_child(outer)

    def _on_ayah_bind(self, _factory, list_item):
        item  = list_item.get_item()
        outer = list_item.get_child()
        outer._item = item
        bm_icon, ref_label, basmala_label, ar_row, ar_label, en_label = outer._refs

        bookmarked = not item.is_basmala and (item.surah_num, item.ayah_num) in self._bookmark_set
        bm_icon.set_visible(bookmarked)
        if bookmarked:
            outer.add_css_class("bookmarked-ayah")
        else:
            outer.remove_css_class("bookmarked-ayah")

        if item.surah_name:
            ref_label.set_markup(
                f"<small><b>{GLib.markup_escape_text(item.surah_name)}"
                f"  {item.surah_num}:{item.ayah_num}</b></small>"
            )
            ref_label.set_visible(True)
        else:
            ref_label.set_visible(False)

        ar_span = f"font='{AR_FONT_FAMILY} {self.font_size}'"
        if item.is_basmala:
            basmala_label.set_markup(f"<span {ar_span}>﷽</span>")
            basmala_label.set_visible(True)
            ar_row.set_visible(False)
            en_label.set_visible(False)
        else:
            basmala_label.set_visible(False)
            ar_row.set_visible(True)
            en_label.set_visible(True)
            marker = f"﴿{_to_arabic_digits(item.ayah_num)}﴾"
            text   = GLib.markup_escape_text(item.arabic + marker)
            ar_label.set_markup(f"<span {ar_span}>{text}</span>")
            en_label.set_label(item.english)

    def _on_ayah_unbind(self, _factory, list_item):
        outer = list_item.get_child()
        if outer:
            outer._item = None

    def _on_ayah_right_click(self, gesture, _n, x, y):
        outer = gesture.get_widget()
        item  = outer._item
        if item is None or item.is_basmala:
            return

        bm_key   = (item.surah_num, item.ayah_num)
        bm_label = "Remove Bookmark" if bm_key in self._bookmark_set else "Bookmark"

        menu = Gio.Menu()
        menu.append("Copy Arabic",    "app.copy-arabic")
        menu.append("Copy English",   "app.copy-english")
        menu.append("Copy Reference", "app.copy-reference")
        menu.append(bm_label,         "app.toggle-bookmark")

        for name, text in [
            ("copy-arabic",    item.arabic),
            ("copy-english",   item.english),
            ("copy-reference", f"{item.surah_num}:{item.ayah_num}"),
        ]:
            action = Gio.SimpleAction(name=name)
            action.connect("activate", self._copy_to_clipboard, text)
            self.remove_action(name)
            self.add_action(action)

        action = Gio.SimpleAction(name="toggle-bookmark")
        action.connect("activate", self._on_bookmark_action, item.surah_num, item.ayah_num)
        self.remove_action("toggle-bookmark")
        self.add_action(action)

        popover = Gtk.PopoverMenu(menu_model=menu)
        popover.set_parent(outer)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    def _copy_to_clipboard(self, _action, _param, text: str):
        self.window.get_clipboard().set(text)

    # ------------------------------------------------------------------ color scheme

    _SCHEME_CYCLE = ['auto', 'dark', 'light']
    _SCHEME_ICONS = {
        'auto':  'display-brightness-symbolic',
        'dark':  'weather-clear-night-symbolic',
        'light': 'weather-clear-symbolic',
    }
    _SCHEME_ADWAITA = {
        'auto':  Adw.ColorScheme.DEFAULT,
        'dark':  Adw.ColorScheme.FORCE_DARK,
        'light': Adw.ColorScheme.FORCE_LIGHT,
    }

    def _apply_color_scheme(self):
        Adw.StyleManager.get_default().set_color_scheme(
            self._SCHEME_ADWAITA[self._color_scheme]
        )
        self._theme_btn.set_icon_name(self._SCHEME_ICONS[self._color_scheme])

    def _on_theme_clicked(self, _btn):
        idx = self._SCHEME_CYCLE.index(self._color_scheme)
        self._color_scheme = self._SCHEME_CYCLE[(idx + 1) % 3]
        self._apply_color_scheme()

    # ------------------------------------------------------------------ surah navigation

    def _navigate_surah(self, delta: int):
        if not self._filtered:
            return
        if self.current_surah is None:
            idx = 0
        else:
            nums = [s[0] for s in self._filtered]
            try:
                idx = nums.index(self.current_surah) + delta
            except ValueError:
                idx = 0
        idx = max(0, min(len(self._filtered) - 1, idx))
        row = self.surah_listbox.get_row_at_index(idx)
        if row:
            self.surah_listbox.select_row(row)
            self._on_surah_activated(self.surah_listbox, row)

    # ------------------------------------------------------------------ font size

    def _on_font_size_changed(self, delta: int):
        new_size = max(14, min(40, self.font_size + delta))
        if new_size == self.font_size:
            return
        self.font_size = new_size
        if self.mode != 'text':
            return
        if self._search_active:
            self._on_text_search_changed(self._search_entry)
        elif self.current_surah:
            self._load_text(self.current_surah)

    # ------------------------------------------------------------------ ayah search

    def _on_text_search_changed(self, entry):
        text = entry.get_text().strip()
        if len(text) < 2:
            if self._search_active:
                self._search_active = False
                if self.current_surah:
                    self._load_text(self.current_surah)
                else:
                    self.ayah_store.remove_all()
                    self.text_title.set_markup("<span size='large'>Select a Surah</span>")
            return

        self._search_active = True
        results = search_ayahs(text)
        self.ayah_store.remove_all()
        self.text_title.set_markup(
            f"<span size='large'>“{GLib.markup_escape_text(text)}”</span>"
            f"  <span size='small' foreground='gray'>{len(results)} results</span>"
        )
        lang_idx = 1 if self.lang == 'ar' else 2
        items = [
            AyahItem(s, n, ar, en, surah_name=SURAH_BY_NUM[s][lang_idx])
            for s, n, ar, en in results
        ]
        self.ayah_store.splice(0, 0, items)

    # ------------------------------------------------------------------ jump to ayah

    def _show_jump_popover(self):
        popover = Gtk.Popover()
        popover.set_parent(self.text_title)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        hint = Gtk.Label(label="Go to ayah  (255  or  2:255)")
        hint.add_css_class("dim-label")
        box.append(hint)

        entry = Gtk.Entry()
        entry.set_placeholder_text("ayah  or  surah:ayah")
        entry.connect("activate", self._on_jump_activated, popover)
        box.append(entry)

        popover.set_child(box)
        popover.popup()
        entry.grab_focus()

    def _on_jump_activated(self, entry, popover):
        popover.popdown()
        text = entry.get_text().strip()
        if ':' in text:
            try:
                surah_str, ayah_str = text.split(':', 1)
                surah_num = int(surah_str)
                ayah_num  = int(ayah_str)
            except ValueError:
                return
            if surah_num != self.current_surah:
                self.current_surah = surah_num
                self._search_entry.set_text('')
                self._search_active = False
                self._load_text(surah_num)
            GLib.idle_add(self._scroll_to_ayah, ayah_num)
        else:
            try:
                ayah_num = int(text)
            except ValueError:
                return
            self._scroll_to_ayah(ayah_num)

    def _scroll_to_ayah(self, ayah_num: int) -> bool:
        for i in range(self.ayah_store.get_n_items()):
            if self.ayah_store.get_item(i).ayah_num == ayah_num:
                try:
                    self.ayah_listview.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                except Exception:
                    pass
                break
        return False  # stop idle_add

    # ------------------------------------------------------------------ bookmarks

    def _on_bookmark_action(self, _action, _param, surah_num: int, ayah_num: int):
        key = (surah_num, ayah_num)
        if key in self._bookmark_set:
            self._bookmark_set.discard(key)
            self._bookmarks = [b for b in self._bookmarks if tuple(b) != key]
        else:
            self._bookmark_set.add(key)
            self._bookmarks.append([surah_num, ayah_num])
        self._save_bookmarks()
        self._update_bookmarks_ui()
        for i in range(self.ayah_store.get_n_items()):
            item = self.ayah_store.get_item(i)
            if not item.is_basmala and item.surah_num == surah_num and item.ayah_num == ayah_num:
                self.ayah_store.remove(i)
                self.ayah_store.insert(i, item)
                break

    def _update_bookmarks_ui(self):
        while child := self._bookmarks_listbox.get_first_child():
            self._bookmarks_listbox.remove(child)

        if not self._bookmarks:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            label = Gtk.Label(label="No bookmarks yet")
            label.add_css_class("dim-label")
            label.set_margin_top(10)
            label.set_margin_bottom(10)
            label.set_margin_start(12)
            label.set_margin_end(12)
            row.set_child(label)
            self._bookmarks_listbox.append(row)
            return

        for surah_num, ayah_num in self._bookmarks:
            s    = SURAH_BY_NUM[surah_num]
            name = s[1] if self.lang == 'ar' else s[2]
            row  = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.surah_num = surah_num
            row.ayah_num  = ayah_num
            label = Gtk.Label()
            label.set_markup(
                f"<b>{GLib.markup_escape_text(name)}</b>"
                f"  <small>{surah_num}:{ayah_num}</small>"
            )
            label.set_halign(Gtk.Align.START)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(12)
            label.set_margin_end(12)
            row.set_child(label)
            self._bookmarks_listbox.append(row)

    def _on_bookmark_row_activated(self, _listbox, row):
        if not hasattr(row, 'surah_num'):
            return
        self._bookmarks_popover.popdown()
        surah_num = row.surah_num
        ayah_num  = row.ayah_num
        self.current_surah = surah_num
        self._search_entry.set_text('')
        self._search_active = False
        if self.mode != 'text':
            self.btn_text.set_active(True)  # triggers _on_mode_toggled → _load_text
        else:
            self._load_text(surah_num)
        GLib.idle_add(self._scroll_to_ayah, ayah_num)

    def _load_bookmarks(self):
        try:
            with open(self._bookmarks_file) as f:
                self._bookmarks = json.load(f)
            self._bookmark_set = {(s, n) for s, n in self._bookmarks}
        except (FileNotFoundError, json.JSONDecodeError):
            self._bookmarks    = []
            self._bookmark_set = set()

    def _save_bookmarks(self):
        try:
            with open(self._bookmarks_file, 'w') as f:
                json.dump(self._bookmarks, f)
        except OSError:
            pass

    # ------------------------------------------------------------------ state

    def _save_state(self):
        state = {
            'mode':         self.mode,
            'page':         self.current_page,
            'surah':        self.current_surah,
            'font_size':    self.font_size,
            'color_scheme': self._color_scheme,
        }
        try:
            with open(self._state_file, 'w') as f:
                json.dump(state, f)
        except OSError:
            pass

    def _restore_state(self):
        try:
            with open(self._state_file) as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._go_to_page(1)
            return

        self.font_size     = state.get('font_size', 22)
        self._color_scheme = state.get('color_scheme', 'auto')
        self._apply_color_scheme()

        mode  = state.get('mode', 'mushaf')
        surah = state.get('surah')
        page  = state.get('page', 1)

        if mode == 'text' and HAS_TEXT_DB and surah:
            self.current_surah = surah
            self.btn_text.set_active(True)  # triggers mode switch + _load_text
        else:
            self._go_to_page(page)

    def _on_close_request(self, _window) -> bool:
        self._save_state()
        return False

    # ------------------------------------------------------------------ event handlers

    def _on_surah_activated(self, _listbox, row):
        self.current_surah = row.surah_number
        self._search_entry.set_text('')
        self._search_active = False
        if self.mode == 'mushaf':
            self._go_to_page(SURAH_FIRST_PAGE.get(self.current_surah, 1))
        else:
            self._load_text(self.current_surah)

    def _on_search_changed(self, entry):
        text = entry.get_text().lower()
        self._filtered = [s for s in SURAHS if
                          text in s[1] or
                          text in s[2].lower() or
                          text in str(s[0])]
        self._populate_surah_list(self._filtered)

    def _on_mode_toggled(self, _btn):
        new_mode = 'mushaf' if self.btn_mushaf.get_active() else 'text'
        if new_mode == self.mode:
            return
        self.mode = new_mode
        self.content_stack.set_visible_child_name(new_mode)
        if new_mode == 'text' and self.current_surah:
            self._load_text(self.current_surah)

    def _on_lang_toggled(self, _btn):
        new_lang = 'ar' if self.btn_ar.get_active() else 'en'
        if new_lang == self.lang:
            return
        self.lang = new_lang
        self.paned.set_direction(
            Gtk.TextDirection.RTL if new_lang == 'ar' else Gtk.TextDirection.LTR
        )
        self._populate_surah_list(self._filtered)
        self._update_bookmarks_ui()
        if self.mode == 'text':
            if self._search_active:
                self._on_text_search_changed(self._search_entry)
            elif self.current_surah:
                self._load_text(self.current_surah)

    def _on_key_pressed(self, _ctrl, keyval, _keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)

        if ctrl and keyval == Gdk.KEY_Up:
            self._navigate_surah(-1)
            return True
        if ctrl and keyval == Gdk.KEY_Down:
            self._navigate_surah(+1)
            return True

        if self.mode == 'text':
            if ctrl and keyval == Gdk.KEY_f:
                self._search_entry.grab_focus()
                return True
            if ctrl and keyval == Gdk.KEY_g:
                self._show_jump_popover()
                return True
            return False

        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down):
            self._go_to_page(self.current_page + 1)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up):
            self._go_to_page(self.current_page - 1)
            return True
        return False
