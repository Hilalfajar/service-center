#!/usr/bin/python3
"""Service Center: daftar dan kelola program sebagai layanan systemd user."""

from __future__ import annotations

import shlex
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from backend import (  # noqa: E402
    APP_ID,
    RESTART_LABELS,
    RESTART_OPTIONS,
    Manager,
    ServiceState,
    detect_command,
    enable_linger,
    linger_enabled,
    pretty_name,
)
from db_restore import (  # noqa: E402
    ENGINE_LABELS,
    RestoreError,
    RestoreRequest,
    restore as restore_database,
)

APP_NAME = "Service Center"
VERSION = "1.1.0"

CSS = b"""
.dot-ok { color: #2ec27e; }
.dot-err { color: #e01b24; }
.dot-warn { color: #e5a50a; }
.dot-off { color: #8a8a8a; }
.service-name { font-weight: 600; }
.logs-view { font-family: monospace; font-size: 0.9em; }
"""


def run_async(func, callback):
    def worker():
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001
            result = exc
        GLib.idle_add(callback, result)

    threading.Thread(target=worker, daemon=True).start()


def status_text(state: ServiceState) -> str:
    if state.active == "active":
        return "Berjalan"
    if state.active == "failed":
        return "Gagal"
    if state.active == "activating":
        return "Menyalakan…"
    if state.active == "deactivating":
        return "Menghentikan…"
    return "Berhenti"


def status_css(state: ServiceState) -> str:
    if state.active == "active":
        return "dot-ok"
    if state.active == "failed":
        return "dot-err"
    if state.active in {"activating", "deactivating"}:
        return "dot-warn"
    return "dot-off"


class ServiceCenterApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self._setup_actions()

    def _setup_actions(self) -> None:
        for name, callback in (
            ("about", self._on_about),
            ("quit", lambda *_: self.quit()),
            ("refresh", lambda *_: self.window and self.window.refresh()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("app.refresh", ["<Primary>r"])

    def _on_about(self, *_args) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="service-center",
            developer_name="Hilal Fajar",
            version=VERSION,
            comments="Daftarkan program web atau aplikasi sebagai layanan systemd "
            "user, lalu kelola dari satu tempat.",
            license_type=Gtk.License.MIT_X11,
        )
        dialog.present(self.window)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(application=self)
        self.window.present()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title(APP_NAME)
        self.set_default_size(1060, 720)
        self.set_icon_name("service-center")

        self.manager = Manager()
        self.states: dict[str, ServiceState] = {}
        self.rows: dict[str, Gtk.ListBoxRow] = {}
        self.current_id: str | None = None
        self._updating = False

        self._build_ui()
        self._load_services()
        self.linger_banner.set_revealed(not linger_enabled())
        self.refresh()
        GLib.timeout_add_seconds(2, self._tick)

    # ---------------------------------------------------------------- UI setup

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(320)
        split.set_max_sidebar_width(420)
        toolbar.set_content(split)

        split.set_sidebar(Adw.NavigationPage.new(self._build_sidebar(), "Layanan"))
        split.set_content(Adw.NavigationPage.new(self._build_content(), "Detail"))

        self.listbox.select_row(None)

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title=APP_NAME, subtitle="Pengelola layanan systemd")
        )

        add_button = Gtk.Button(label="Tambah", icon_name="list-add-symbolic")
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", self._on_add_clicked)
        header.pack_start(add_button)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Muat ulang", "app.refresh")
        menu.append("Tentang Service Center", "app.about")
        menu.append("Keluar", "app.quit")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)
        sidebar.append(header)

        self.linger_banner = Adw.Banner(
            title="Auto-start saat boot belum aktif. Layanan tidak jalan sebelum login"
        )
        self.linger_banner.set_button_label("Aktifkan")
        self.linger_banner.connect("button-clicked", self._on_enable_linger)
        self.linger_banner.set_revealed(False)
        sidebar.append(self.linger_banner)

        self.search = Gtk.SearchEntry(placeholder_text="Cari layanan…")
        self.search.set_margin_top(6)
        self.search.set_margin_bottom(6)
        self.search.set_margin_start(8)
        self.search.set_margin_end(8)
        self.search.connect("search-changed", self._on_search_changed)
        sidebar.append(self.search)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_row_selected)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.listbox)

        empty = Adw.StatusPage()
        empty.set_icon_name("system-run-symbolic")
        empty.set_title("Belum ada layanan")
        empty.set_description(
            "Pilih file program (.py, .js, .sh, package.json, atau binary) "
            "lalu jalankan sebagai layanan."
        )
        empty_button = Gtk.Button(label="Tambah Layanan")
        empty_button.add_css_class("suggested-action")
        empty_button.set_halign(Gtk.Align.CENTER)
        empty_button.connect("clicked", self._on_add_clicked)
        empty.set_child(empty_button)

        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.set_vexpand(True)
        self.sidebar_stack.add_named(scroller, "list")
        self.sidebar_stack.add_named(empty, "empty")
        sidebar.append(self.sidebar_stack)
        return sidebar

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Detail layanan"))
        header.set_show_start_title_buttons(False)
        toolbar.add_top_bar(header)

        self.detail_stack = Gtk.Stack()
        self.detail_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        empty = Adw.StatusPage()
        empty.set_icon_name("utilities-system-monitor-symbolic")
        empty.set_title("Pilih layanan")
        empty.set_description(
            "Pilih layanan di kiri untuk melihat status, mengontrol, dan membaca log."
        )
        self.detail_stack.add_named(empty, "empty")
        self.detail_stack.add_named(self._build_detail(), "detail")
        self.detail_stack.set_visible_child_name("empty")

        toolbar.set_content(self.detail_stack)
        return toolbar

    def _build_detail(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        status_group = Adw.PreferencesGroup(title="Status")
        self.status_banner = Adw.Banner(title="Layanan gagal. Cek log di bawah")
        self.status_banner.set_revealed(False)
        status_group.add(self.status_banner)

        self.state_row = Adw.ActionRow(title="Keadaan")
        self.pid_row = Adw.ActionRow(title="PID")
        status_group.add(self.state_row)
        status_group.add(self.pid_row)

        controls = Adw.ActionRow(title="Kontrol")
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.start_button = Gtk.Button(label="Mulai")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", lambda *_: self._action("start"))
        self.stop_button = Gtk.Button(label="Hentikan")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.connect("clicked", lambda *_: self._action("stop"))
        self.restart_button = Gtk.Button(label="Restart")
        self.restart_button.connect("clicked", lambda *_: self._action("restart"))
        for button in (self.start_button, self.stop_button, self.restart_button):
            buttons.append(button)
        controls.add_suffix(buttons)
        status_group.add(controls)

        self.autostart_row = Adw.SwitchRow(
            title="Mulai otomatis",
            subtitle="Jalankan saat boot (perlu linger aktif)",
        )
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        status_group.add(self.autostart_row)
        page.add(status_group)

        info_group = Adw.PreferencesGroup(title="Detail")
        self.file_row = Adw.ActionRow(title="File")
        self.command_row = Adw.ActionRow(title="Perintah")
        self.workdir_row = Adw.ActionRow(title="Direktori")
        self.unit_row = Adw.ActionRow(title="Unit")
        for row in (self.file_row, self.command_row, self.workdir_row, self.unit_row):
            row.set_subtitle_selectable(True)
            info_group.add(row)
        page.add(info_group)

        database_group = Adw.PreferencesGroup(title="Database")
        restore_row = Adw.ActionRow(
            title="Restore database",
            subtitle="Pulihkan dump PostgreSQL ke database aplikasi",
        )
        restore_button = Gtk.Button(label="Restore")
        restore_button.set_valign(Gtk.Align.CENTER)
        restore_button.connect("clicked", self._on_restore_clicked)
        restore_row.add_suffix(restore_button)
        restore_row.set_activatable_widget(restore_button)
        database_group.add(restore_row)
        page.add(database_group)

        logs_group = Adw.PreferencesGroup(title="Log")
        self.logs_expander = Adw.ExpanderRow(title="Log layanan", subtitle="journalctl --user")
        log_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self.logs_view = Gtk.TextView()
        self.logs_view.set_editable(False)
        self.logs_view.set_cursor_visible(False)
        self.logs_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.logs_view.set_left_margin(8)
        self.logs_view.set_right_margin(8)
        self.logs_view.set_top_margin(6)
        self.logs_view.set_bottom_margin(6)
        self.logs_view.add_css_class("logs-view")

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_min_content_height(220)
        log_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        log_scroller.set_child(self.logs_view)
        log_box.append(log_scroller)

        reload_button = Gtk.Button(label="Muat ulang log")
        reload_button.set_halign(Gtk.Align.END)
        reload_button.connect("clicked", lambda *_: self._load_logs())
        log_box.append(reload_button)

        self.logs_expander.add_row(log_box)
        self.logs_expander.connect("notify::expanded", self._on_logs_expanded)
        logs_group.add(self.logs_expander)
        page.add(logs_group)

        danger_group = Adw.PreferencesGroup(title="Zona berbahaya")
        delete_row = Adw.ActionRow(
            title="Hapus layanan",
            subtitle="Hentikan dan hapus unit systemd (file program tidak disentuh)",
        )
        delete_button = Gtk.Button(icon_name="user-trash-symbolic")
        delete_button.set_valign(Gtk.Align.CENTER)
        delete_button.add_css_class("destructive-action")
        delete_button.connect("clicked", self._on_delete_clicked)
        delete_row.add_suffix(delete_button)
        delete_row.set_activatable_widget(delete_button)
        danger_group.add(delete_row)
        page.add(danger_group)

        return page

    # ------------------------------------------------------------------ rows

    def _load_services(self) -> None:
        for row in list(self.rows.values()):
            self.listbox.remove(row)
        self.rows.clear()

        services = self.manager.services()
        for service in services:
            row = Gtk.ListBoxRow()
            row.service_id = service.id
            box = Gtk.Box(
                spacing=10,
                margin_start=12,
                margin_end=12,
                margin_top=8,
                margin_bottom=8,
            )
            dot = Gtk.Label(label="●")
            dot.add_css_class("dot-off")
            dot.set_valign(Gtk.Align.CENTER)

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)
            name = Gtk.Label(label=service.name, xalign=0)
            name.add_css_class("service-name")
            name.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle = Gtk.Label(xalign=0)
            subtitle.add_css_class("caption")
            subtitle.add_css_class("dim-label")
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            text_box.append(name)
            text_box.append(subtitle)

            box.append(dot)
            box.append(text_box)
            row.set_child(box)
            row.dot = dot
            row.subtitle = subtitle
            self.listbox.append(row)
            self.rows[service.id] = row

        self.sidebar_stack.set_visible_child_name("list" if services else "empty")
        self._apply_search()

    def _apply_search(self) -> None:
        query = self.search.get_text().strip().lower()
        for sid, row in self.rows.items():
            service = self.manager.get(sid)
            row.set_visible(bool(service) and query in service.name.lower())

    def _on_search_changed(self, _entry) -> None:
        self._apply_search()

    def _on_row_selected(self, _listbox, row) -> None:
        if row is None or not hasattr(row, "service_id"):
            return
        self.current_id = row.service_id
        self.detail_stack.set_visible_child_name("detail")
        self._update_detail()

    def _update_row(self, row: Gtk.ListBoxRow, state: ServiceState, unit_missing: bool) -> None:
        for css_class in ("dot-ok", "dot-err", "dot-warn", "dot-off"):
            row.dot.remove_css_class(css_class)
        if unit_missing:
            row.dot.add_css_class("dot-err")
            row.subtitle.set_text("Unit hilang")
            return
        row.dot.add_css_class(status_css(state))
        suffix = " · autostart" if state.enabled == "enabled" else ""
        row.subtitle.set_text(f"{status_text(state)}{suffix}")

    # ---------------------------------------------------------------- refresh

    def _tick(self) -> bool:
        self.refresh()
        return True

    def refresh(self) -> None:
        services = self.manager.services()
        try:
            self.states = self.manager.states([svc.unit_name for svc in services])
        except RuntimeError:
            self.states = {}

        for service in services:
            row = self.rows.get(service.id)
            if row is None:
                continue
            state = self.states.get(service.unit_name) or ServiceState()
            self._update_row(row, state, not self.manager.unit_exists(service.id))

        self._update_detail()

    def _update_detail(self) -> None:
        service = self.manager.get(self.current_id) if self.current_id else None
        if service is None:
            self.detail_stack.set_visible_child_name("empty")
            return

        state = self.states.get(service.unit_name) or ServiceState()
        unit_missing = not self.manager.unit_exists(service.id)

        if unit_missing:
            self.state_row.set_subtitle("Unit hilang")
            self.status_banner.set_title("Unit systemd tidak ditemukan. Hapus lalu daftarkan ulang")
        else:
            detail = status_text(state)
            if state.sub:
                detail = f"{detail} ({state.sub})"
            self.state_row.set_subtitle(detail)
            self.status_banner.set_title("Layanan gagal. Cek log di bawah")
        self.status_banner.set_revealed(state.failed)
        self.pid_row.set_subtitle(state.pid if state.pid not in ("", "0") else "-")

        self.file_row.set_subtitle(service.file)
        self.command_row.set_subtitle(shlex.join(service.command))
        self.workdir_row.set_subtitle(service.workdir)
        self.unit_row.set_subtitle(service.unit_name)

        running = state.running
        self.start_button.set_sensitive(not running and not unit_missing)
        self.stop_button.set_sensitive(running and not unit_missing)
        self.restart_button.set_sensitive(running and not unit_missing)

        self._updating = True
        self.autostart_row.set_active(state.enabled == "enabled")
        self.autostart_row.set_sensitive(not unit_missing)
        self._updating = False

    # --------------------------------------------------------------- actions

    def _action(self, verb: str) -> None:
        sid = self.current_id
        if not sid:
            return
        self._set_busy(True)
        run_async(lambda: self.manager.action(sid, verb), self._on_action_done)

    def _on_action_done(self, result) -> None:
        self._set_busy(False)
        if isinstance(result, Exception):
            self._toast(f"Gagal: {result}")
        else:
            ok, message = result
            if not ok:
                self._toast(message or "Perintah gagal")
        self.refresh()

    def _set_busy(self, busy: bool) -> None:
        for button in (self.start_button, self.stop_button, self.restart_button):
            button.set_sensitive(not busy)

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating or not self.current_id:
            return
        sid = self.current_id
        enabled = row.get_active()
        run_async(lambda: self.manager.set_enabled(sid, enabled), self._on_autostart_done)

    def _on_autostart_done(self, result) -> None:
        if isinstance(result, Exception):
            self._toast(str(result))
        else:
            ok, message = result
            if not ok:
                self._toast(message or "Gagal mengubah autostart")
        self.refresh()

    def _on_logs_expanded(self, expander: Adw.ExpanderRow, _pspec) -> None:
        if expander.get_expanded():
            self._load_logs()

    def _load_logs(self) -> None:
        sid = self.current_id
        if not sid:
            return
        self.logs_view.get_buffer().set_text("Memuat log…")
        run_async(lambda: self.manager.logs(sid), self._on_logs_loaded)

    def _on_logs_loaded(self, result) -> None:
        if isinstance(result, Exception):
            self.logs_view.get_buffer().set_text(f"Gagal memuat log:\n{result}")
            return
        ok, text = result
        if not ok:
            self.logs_view.get_buffer().set_text(text)
            return
        self.logs_view.get_buffer().set_text(text if text.strip() else "Belum ada log.")

    def _on_restore_clicked(self, _button) -> None:
        service = self.manager.get(self.current_id or "")
        if service is None:
            return
        RestoreDatabaseDialog(self, service).present(self)

    def after_restore(self, success: bool, message: str) -> None:
        self.refresh()
        if success:
            self._toast("Restore database selesai")
        else:
            first = message.strip().splitlines()[0] if message.strip() else "Restore gagal"
            self._toast(f"Restore database gagal: {first}"[:180])

    def _on_delete_clicked(self, _button) -> None:
        service = self.manager.get(self.current_id or "")
        if service is None:
            return
        dialog = Adw.AlertDialog(
            heading=f"Hapus “{service.name}”?",
            body="Unit systemd dan entri layanan akan dihapus. File program tidak disentuh.",
        )
        dialog.add_response("cancel", "Batal")
        dialog.add_response("delete", "Hapus")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response)
        dialog.present(self)

    def _on_delete_response(self, _dialog, response: str) -> None:
        if response != "delete" or not self.current_id:
            return
        sid = self.current_id
        run_async(lambda: self.manager.delete(sid), self._on_delete_done)

    def _on_delete_done(self, result) -> None:
        if isinstance(result, Exception):
            self._toast(str(result))
        self.current_id = None
        self._load_services()
        self.detail_stack.set_visible_child_name("empty")
        self.refresh()

    def _on_enable_linger(self, _banner) -> None:
        run_async(enable_linger, self._on_linger_done)

    def _on_linger_done(self, result) -> None:
        if isinstance(result, Exception):
            self._toast(str(result))
        else:
            ok, message = result
            if ok:
                self._toast("Auto-start saat boot aktif")
            else:
                self._toast(message or "Gagal mengaktifkan linger")
        self.linger_banner.set_revealed(not linger_enabled())

    # ------------------------------------------------------------------- add

    def _on_add_clicked(self, _button) -> None:
        dialog = AddServiceDialog(self)
        dialog.present(self)

    def after_add(self, service, warnings: list[str]) -> None:
        self._load_services()
        row = self.rows.get(service.id)
        if row is not None:
            self.listbox.select_row(row)
        if warnings:
            self._toast(warnings[0])
        else:
            self._toast(f"Layanan “{service.name}” ditambahkan")
        self.refresh()

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))


class RestoreDatabaseDialog(Adw.Dialog):
    def __init__(self, window: MainWindow, service) -> None:
        super().__init__()
        self.window = window
        self.manager = window.manager
        self.service = service
        self.dump_path: Path | None = None
        self.running = False
        self.form_widgets: list[Gtk.Widget] = []

        self.set_content_width(640)
        self.set_content_height(780)
        self.set_title("Restore Database")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Restore Database", subtitle=service.name)
        )
        self.close_button = Gtk.Button(label="Tutup")
        self.close_button.connect("clicked", lambda *_: self.close())
        header.pack_start(self.close_button)
        self.start_button = Gtk.Button(label="Start")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._on_start)
        header.pack_end(self.start_button)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.error_banner = Adw.Banner(title="")
        self.error_banner.set_revealed(False)
        body.append(self.error_banner)
        page = Adw.PreferencesPage()
        body.append(page)
        toolbar.set_content(body)

        database_group = Adw.PreferencesGroup(
            title="Database",
            description="Saat ini mendukung PostgreSQL.",
        )
        self.engine_row = Adw.ComboRow(title="Jenis database")
        self.engine_row.set_model(Gtk.StringList.new(list(ENGINE_LABELS.values())))
        self.engine_row.set_selected(0)
        database_group.add(self.engine_row)
        self.form_widgets.append(self.engine_row)

        self.host_row = Adw.EntryRow(title="Host")
        self.host_row.set_text("localhost")
        self.port_row = Adw.EntryRow(title="Port")
        self.port_row.set_text("5432")
        self.user_row = Adw.EntryRow(title="User")
        self.user_row.set_text("postgres")
        self.password_row = Adw.ActionRow(title="Password")
        self.password_entry = Gtk.PasswordEntry()
        self.password_entry.set_show_peek_icon(True)
        self.password_entry.set_hexpand(True)
        self.password_entry.set_size_request(260, -1)
        self.password_row.add_suffix(self.password_entry)
        self.database_row = Adw.EntryRow(title="Nama database")
        for widget in (
            self.host_row,
            self.port_row,
            self.user_row,
            self.password_row,
            self.database_row,
        ):
            database_group.add(widget)
            self.form_widgets.append(widget)
        page.add(database_group)

        file_group = Adw.PreferencesGroup(title="File dump")
        self.file_row = Adw.ActionRow(title="File", subtitle="Belum dipilih")
        self.choose_button = Gtk.Button(label="Pilih File…")
        self.choose_button.set_valign(Gtk.Align.CENTER)
        self.choose_button.connect("clicked", self._choose_dump)
        self.file_row.add_suffix(self.choose_button)
        self.file_row.set_activatable_widget(self.choose_button)
        file_group.add(self.file_row)
        self.form_widgets.append(self.file_row)
        page.add(file_group)

        option_group = Adw.PreferencesGroup(title="Opsi")
        self.create_row = Adw.SwitchRow(
            title="Buat database jika belum ada",
            active=True,
        )
        self.clean_row = Adw.SwitchRow(
            title="Kosongkan objek lama",
            subtitle="Hapus skema public sebelum restore",
            active=False,
        )
        self.stop_row = Adw.SwitchRow(
            title="Hentikan layanan saat restore",
            subtitle="Layanan dinyalakan lagi setelah restore selesai",
            active=True,
        )
        for row in (self.create_row, self.clean_row, self.stop_row):
            option_group.add(row)
            self.form_widgets.append(row)
        page.add(option_group)

        log_group = Adw.PreferencesGroup(title="Log")
        self.log_expander = Adw.ExpanderRow(title="Log restore", subtitle="Keluaran psql")
        self.log_expander.set_expanded(True)
        log_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self.status_label = Gtk.Label(label="Siap", xalign=0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("dim-label")
        log_box.append(self.status_label)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)
        self.log_view.set_top_margin(6)
        self.log_view.set_bottom_margin(6)
        self.log_view.add_css_class("logs-view")

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_min_content_height(220)
        log_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        log_scroller.set_child(self.log_view)
        log_box.append(log_scroller)

        self.log_expander.add_row(log_box)
        log_group.add(self.log_expander)
        page.add(log_group)

    def _choose_dump(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Pilih file dump")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        dump_filter = Gtk.FileFilter()
        dump_filter.set_name("Dump database (*.sql, *.dump, *.backup, *.tar)")
        for pattern in ("*.sql", "*.dump", "*.backup", "*.tar"):
            dump_filter.add_pattern(pattern)
        filters.append(dump_filter)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("Semua file")
        all_filter.add_pattern("*")
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(dump_filter)
        dialog.open(self.window, None, self._on_dump_chosen)

    def _on_dump_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile is None or gfile.get_path() is None:
            return
        self.dump_path = Path(gfile.get_path())
        self.file_row.set_subtitle(str(self.dump_path))
        self.error_banner.set_revealed(False)

    def _service_running(self) -> bool:
        try:
            states = self.manager.states([self.service.unit_name])
        except RuntimeError:
            return False
        state = states.get(self.service.unit_name)
        return bool(state and state.running)

    def _on_start(self, _button) -> None:
        if self.running:
            return
        self.error_banner.set_revealed(False)
        if self.dump_path is None:
            self._show_error("Pilih file dump dulu.")
            return
        engines = list(ENGINE_LABELS)
        selected = self.engine_row.get_selected()
        engine = engines[selected] if 0 <= selected < len(engines) else engines[0]
        try:
            request = RestoreRequest(
                engine=engine,
                host=self.host_row.get_text().strip(),
                port=self.port_row.get_text().strip(),
                user=self.user_row.get_text().strip(),
                password=self.password_entry.get_text(),
                database=self.database_row.get_text().strip(),
                dump=self.dump_path,
                create_database=self.create_row.get_active(),
                clean=self.clean_row.get_active(),
            )
            request.validate()
        except RestoreError as exc:
            self._show_error(str(exc))
            return

        self._clear_log()
        self._append_log(
            f"Target: {request.user}@{request.host}:{request.port}/{request.database}"
        )
        self._append_log(f"File: {request.dump}")
        self._status("Berjalan…")
        self.running = True
        self._set_running(True)
        self.log_expander.set_expanded(True)
        self.log_view.grab_focus()
        was_running = self._service_running()
        stop_service = self.stop_row.get_active()
        threading.Thread(
            target=self._restore_worker,
            args=(request, was_running, stop_service),
            daemon=True,
        ).start()

    def _restore_worker(self, request, was_running: bool, stop_service: bool) -> None:
        success = False
        error = ""
        stopped = False
        try:
            if stop_service and was_running:
                self._log(f"Menghentikan layanan {self.service.name}")
                ok, message = self.manager.stop(self.service.id)
                if not ok:
                    raise RestoreError(f"Gagal menghentikan layanan: {message}")
                stopped = True
            restore_database(request, self._log)
            success = True
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            if stopped:
                self._log(f"Menyalakan kembali layanan {self.service.name}")
                ok, message = self.manager.start(self.service.id)
                if not ok:
                    self._log(f"Gagal menyalakan layanan: {message}")
        GLib.idle_add(self._on_finished, success, error)

    def _on_finished(self, success: bool, message: str) -> None:
        self.running = False
        self._set_running(False)
        if success:
            self._status("Restore selesai")
        else:
            self._status(f"Gagal: {message}")
            self._append_log(f"Gagal: {message}")
        self.window.after_restore(success, message)

    def _set_running(self, running: bool) -> None:
        for widget in self.form_widgets:
            widget.set_sensitive(not running)
        self.start_button.set_sensitive(not running)
        self.close_button.set_sensitive(not running)
        if hasattr(self, "set_can_close"):
            self.set_can_close(not running)

    def _show_error(self, message: str) -> None:
        self.error_banner.set_title(message)
        self.error_banner.set_revealed(True)

    def _status(self, text: str) -> None:
        self.status_label.set_text(text)

    def _clear_log(self) -> None:
        self.log_view.get_buffer().set_text("")

    def _append_log(self, text: str) -> None:
        buffer = self.log_view.get_buffer()
        if buffer.get_end_iter().get_offset():
            buffer.insert(buffer.get_end_iter(), "\n")
        buffer.insert(buffer.get_end_iter(), text)
        self.log_view.scroll_to_iter(buffer.get_end_iter(), 0.0, False, 0.0, 0.0)

    def _log(self, text: str) -> None:
        GLib.idle_add(self._append_log, text)


class AddServiceDialog(Adw.Dialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self.window = window
        self.manager = window.manager
        self.selected_file: Path | None = None

        self.set_content_width(560)
        self.set_content_height(700)
        self.set_title("Tambah Layanan")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Tambah Layanan"))
        cancel_button = Gtk.Button(label="Batal")
        cancel_button.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_button)
        self.save_button = Gtk.Button(label="Simpan")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        header.pack_end(self.save_button)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        app_group = Adw.PreferencesGroup(
            title="Aplikasi",
            description="Pilih file utama program. Perintah dan direktori diisi otomatis.",
        )
        self.file_row = Adw.ActionRow(title="File program", subtitle="Belum dipilih")
        choose_button = Gtk.Button(label="Pilih File…")
        choose_button.set_valign(Gtk.Align.CENTER)
        choose_button.connect("clicked", self._choose_file)
        self.file_row.add_suffix(choose_button)
        self.file_row.set_activatable_widget(choose_button)
        app_group.add(self.file_row)

        self.name_row = Adw.EntryRow(title="Nama layanan")
        self.command_row = Adw.EntryRow(title="Perintah")
        self.workdir_row = Adw.EntryRow(title="Direktori kerja")
        for row in (self.name_row, self.command_row, self.workdir_row):
            app_group.add(row)
        page.add(app_group)

        option_group = Adw.PreferencesGroup(title="Opsi")
        self.args_row = Adw.EntryRow(title="Argumen tambahan")
        option_group.add(self.args_row)

        self.restart_row = Adw.ComboRow(title="Restart otomatis")
        self.restart_row.set_model(Gtk.StringList.new(list(RESTART_LABELS.values())))
        self.restart_row.set_selected(0)
        option_group.add(self.restart_row)

        self.boot_row = Adw.SwitchRow(
            title="Mulai otomatis saat boot",
            subtitle="Membutuhkan linger agar jalan sebelum login",
            active=True,
        )
        self.start_row = Adw.SwitchRow(title="Jalankan sekarang", active=True)
        option_group.add(self.boot_row)
        option_group.add(self.start_row)
        page.add(option_group)

        env_group = Adw.PreferencesGroup(title="Environment")
        self.env_expander = Adw.ExpanderRow(
            title="Variabel environment",
            subtitle="Satu per baris: KEY=VALUE",
        )
        env_scroller = Gtk.ScrolledWindow()
        env_scroller.set_min_content_height(120)
        env_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.env_view = Gtk.TextView()
        self.env_view.set_monospace(True)
        self.env_view.set_left_margin(8)
        self.env_view.set_right_margin(8)
        self.env_view.set_top_margin(6)
        self.env_view.set_bottom_margin(6)
        env_scroller.set_child(self.env_view)
        self.env_expander.add_row(env_scroller)
        env_group.add(self.env_expander)
        page.add(env_group)

    def _choose_file(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Pilih file program")
        dialog.open(self.window, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile is None:
            return
        path = Path(gfile.get_path())
        self.selected_file = path
        self.file_row.set_subtitle(str(path))
        if not self.name_row.get_text().strip():
            self.name_row.set_text(pretty_name(path))
        try:
            command, workdir = detect_command(path)
        except ValueError as exc:
            self.command_row.set_text("")
            self.workdir_row.set_text(str(path.parent))
            self._show_error(str(exc))
            return
        self.command_row.set_text(shlex.join(command))
        self.workdir_row.set_text(str(workdir))

    def _parse_env(self) -> dict[str, str]:
        buffer = self.env_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        env: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                env[key] = value.strip()
        return env

    def _on_save(self, _button) -> None:
        if self.selected_file is None:
            self._show_error("Pilih file program dulu.")
            return
        name = self.name_row.get_text().strip()
        if not name:
            self._show_error("Nama layanan wajib diisi.")
            return
        try:
            command = shlex.split(self.command_row.get_text().strip())
            command += shlex.split(self.args_row.get_text().strip())
        except ValueError as exc:
            self._show_error(f"Perintah tidak valid: {exc}")
            return
        if not command:
            self._show_error("Perintah masih kosong.")
            return
        workdir = self.workdir_row.get_text().strip() or str(self.selected_file.parent)
        if not Path(workdir).is_dir():
            self._show_error("Direktori kerja tidak ditemukan.")
            return
        if "\n" in name:
            self._show_error("Nama layanan tidak boleh berisi baris baru.")
            return

        restart = RESTART_OPTIONS[self.restart_row.get_selected()]
        payload = {
            "name": name,
            "file": str(self.selected_file),
            "command": command,
            "workdir": workdir,
            "env": self._parse_env(),
            "restart": restart,
            "autostart": self.boot_row.get_active(),
            "start_now": self.start_row.get_active(),
        }
        self.save_button.set_sensitive(False)
        run_async(lambda: self.manager.add(**payload), self._on_saved)

    def _on_saved(self, result) -> None:
        self.save_button.set_sensitive(True)
        if isinstance(result, Exception):
            self._show_error(str(result))
            return
        service, warnings = result
        self.close()
        self.window.after_add(service, warnings)

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Tidak bisa melanjutkan", body=message)
        dialog.add_response("ok", "OK")
        dialog.present(self.window)


def main() -> int:
    app = ServiceCenterApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
