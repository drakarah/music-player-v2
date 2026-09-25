#!/usr/bin/env python3
"""PiMusicPlayerTray for Linux.

A GTK based system tray application that shows the music player web page in a
small popup window and lets you control playback (play/pause, next,
previous, stop, volume, ...) using global keyboard shortcuts, without having
to switch to the browser tab.

This is the Linux counterpart of the Windows PiMusicPlayerTray application.
See README.md for setup instructions and dependencies.
"""
import configparser
import os
import sys

# Wayland compositors (e.g. GNOME) ignore a client's request to position its
# own window, so the popup would end up centered instead of in the corner.
# Prefer X11 (XWayland on Wayland sessions) so window.move() works; this must
# be set before GTK is initialized. Set GDK_BACKEND yourself to override.
os.environ.setdefault("GDK_BACKEND", "x11,wayland")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3

from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2

try:
    # On pure-Wayland sessions without XWayland (or with XWayland but no
    # DISPLAY reachable, e.g. inside a Flatpak sandbox missing the X11
    # socket), pynput's Linux backend can raise as soon as it is imported
    # because it tries to connect to an X server. Import it defensively so
    # the whole application doesn't crash on startup; global hotkeys are
    # simply disabled in that case (see _register_hotkeys()).
    from pynput import keyboard
except Exception as _pynput_import_error:
    keyboard = None
    PYNPUT_IMPORT_ERROR = _pynput_import_error
else:
    PYNPUT_IMPORT_ERROR = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ID = "org.drakarah.PiMusicPlayerTray"

# Same set of hotkeys/actions as the Windows PiMusicPlayerTray application.
HOTKEY_NAMES = [
    "PlayPause",
    "Stop",
    "Next",
    "Previous",
    "VolumeUp",
    "VolumeDown",
    "Refresh",
    "Show",
]

# Maps the friendly modifier names used in config.ini to the names pynput
# expects when building a hotkey combo string.
MODIFIER_MAP = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "shift": "<shift>",
    "win": "<cmd>",
    "super": "<cmd>",
    "cmd": "<cmd>",
}

# Legacy (Version != "2") player API function names, mirroring MainForm.cs.
LEGACY_API_FUNCTIONS = {
    "playPause": "playerTogglePlay",
    "forward": "playerNext",
    "rewind": "playerPrevious",
    "stop": "playerStop",
    "volumeUp": "playerVolumeUp",
    "volumeDown": "playerVolumeDown",
}


def _default_config_path():
    """Locate the bundled config.ini with the built-in default settings.

    When run from a checkout it sits next to this script; when installed
    (e.g. via the Flatpak) it is shipped as a read-only data file instead.
    """
    local = os.path.join(SCRIPT_DIR, "config.ini")
    if os.path.exists(local):
        return local

    for data_dir in GLib.get_system_data_dirs():
        candidate = os.path.join(data_dir, "pimusicplayertray", "config.ini")
        if os.path.exists(candidate):
            return candidate

    return local


def _user_config_paths():
    """Per-user config.ini files used to override the bundled defaults.

    Inside the Flatpak sandbox GLib's user config dir points to
    ~/.var/app/<app-id>/config, so the host's ~/.config/pimusicplayertray
    (exposed read-only via the manifest's --filesystem permission) is read
    as well. Later paths take precedence over earlier ones.
    """
    host = os.path.join(os.path.expanduser("~"), ".config", "pimusicplayertray", "config.ini")
    user = os.path.join(GLib.get_user_config_dir(), "pimusicplayertray", "config.ini")
    return [host] if host == user else [host, user]


def load_config():
    parser = configparser.ConfigParser()
    # Values from the user config (if present) take precedence over the
    # bundled defaults, so users only need to set the keys they want to
    # change (e.g. just PlayerUrl or a single hotkey).
    parser.read([_default_config_path()] + _user_config_paths())
    general = parser["General"] if parser.has_section("General") else {}
    hotkeys = parser["Hotkeys"] if parser.has_section("Hotkeys") else {}
    return general, hotkeys


def to_pynput_combo(value):
    """Convert a 'ctrl+alt+k' style combo into pynput's '<ctrl>+<alt>+k'."""
    parts = [p.strip().lower() for p in value.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey definition")

    converted = []
    for part in parts:
        if part in MODIFIER_MAP:
            converted.append(MODIFIER_MAP[part])
        elif len(part) == 1:
            converted.append(part)
        else:
            # Named keys such as function keys (f5), space, tab, etc.
            converted.append("<%s>" % part)
    return "+".join(converted)


# Maps the friendly modifier names used in config.ini to the GTK/portal
# accelerator syntax (e.g. "<Control><Alt>k") used as the "preferred_trigger"
# hint for the org.freedesktop.portal.GlobalShortcuts interface.
PORTAL_MODIFIER_MAP = {
    "ctrl": "<Control>",
    "control": "<Control>",
    "alt": "<Alt>",
    "shift": "<Shift>",
    "win": "<Super>",
    "super": "<Super>",
    "cmd": "<Super>",
}


def to_portal_trigger(value):
    """Convert a 'ctrl+alt+k' style combo into a GTK accelerator string
    such as '<Control><Alt>k', used as the preferred_trigger hint when
    binding shortcuts through the desktop portal."""
    parts = [p.strip().lower() for p in value.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey definition")

    mods = []
    key = None
    for part in parts:
        if part in PORTAL_MODIFIER_MAP:
            mods.append(PORTAL_MODIFIER_MAP[part])
        elif key is not None:
            raise ValueError(
                "hotkey %r has more than one non-modifier key" % value
            )
        else:
            key = part
    if key is None:
        raise ValueError("hotkey %r has no non-modifier key" % value)

    if len(key) == 1:
        key_name = key  # single-char keys are lowercase, per GTK accelerator
        # syntax convention (e.g. Gtk.accelerator_parse("<Control>k")).
    elif key[0] == "f" and key[1:].isdigit():
        key_name = key.upper()  # f5 -> F5
    else:
        key_name = key.capitalize()  # space -> Space, tab -> Tab, ...

    return "".join(mods) + key_name


class PortalGlobalShortcuts:
    """Global hotkeys via the xdg-desktop-portal GlobalShortcuts interface.

    Unlike X11, Wayland compositors don't let arbitrary clients grab
    keyboard shortcuts directly, so sandboxed (Flatpak) apps are expected
    to use this portal instead of talking to the display server. It also
    works on X11 sessions where the portal implementation supports it.
    Requires an xdg-desktop-portal backend that implements
    org.freedesktop.portal.GlobalShortcuts (e.g. xdg-desktop-portal-gnome
    >= 45, xdg-desktop-portal-kde, or xdg-desktop-portal-wlr with support
    enabled); otherwise CreateSession/BindShortcuts will raise and the
    caller should fall back to another backend.
    """

    BUS_NAME = "org.freedesktop.portal.Desktop"
    OBJECT_PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.GlobalShortcuts"
    REQUEST_IFACE = "org.freedesktop.portal.Request"
    CALL_TIMEOUT_SECONDS = 10

    def __init__(self, shortcuts, on_activated):
        """shortcuts: iterable of (id, description, preferred_trigger).
        on_activated: callback invoked with the shortcut id when triggered.
        Raises on any failure (portal unavailable, interface missing,
        request denied, timeout, ...); the caller is expected to catch
        this and fall back to another hotkey backend.
        """
        self._on_activated = on_activated
        self._counter = 0
        self._session_handle = None

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender = self.bus.get_unique_name()[1:].replace(".", "_")

        self._activated_sub = self.bus.signal_subscribe(
            self.BUS_NAME, self.IFACE, "Activated", self.OBJECT_PATH,
            None, Gio.DBusSignalFlags.NONE, self._on_activated_signal,
        )

        try:
            create_results = self._call_portal(
                "CreateSession",
                "(a{sv})",
                {"session_handle_token": GLib.Variant("s", self._next_token("session"))},
            )
            self._session_handle = create_results["session_handle"]

            shortcut_specs = []
            for shortcut_id, description, preferred_trigger in shortcuts:
                options = {"description": GLib.Variant("s", description)}
                if preferred_trigger:
                    options["preferred_trigger"] = GLib.Variant("s", preferred_trigger)
                shortcut_specs.append((shortcut_id, options))

            bind_results = self._call_portal(
                "BindShortcuts",
                "(oa(sa{sv})sa{sv})",
                (self._session_handle, shortcut_specs, "", {}),
            )
            # What each shortcut is actually bound to, which may differ
            # from preferred_trigger (see _register_hotkeys_via_portal).
            self.bound_triggers = {
                shortcut_id: options.get("trigger_description", "")
                for shortcut_id, options in bind_results.get("shortcuts", [])
            }
        except Exception:
            self.stop()
            raise

    def _next_token(self, prefix):
        self._counter += 1
        return "%s_%d_%d" % (prefix, os.getpid(), self._counter)

    # Response codes used by portal Request objects, per the xdg-desktop-
    # portal spec: 0 = success, 1 = user cancelled, 2 = other error/ended.
    RESPONSE_CODE_DESCRIPTIONS = {
        0: "success",
        1: "cancelled by the user",
        2: "ended/other error",
    }

    def _call_portal(self, method, extra_sig, extra_args):
        """Call a Request-based portal method and block (via a nested
        GLib main loop) until its Response signal arrives, returning the
        response's results dict. Raises RuntimeError on error/timeout.

        Note: this is only safe to call while no other GLib main loop
        iteration is running higher up the call stack (e.g. during
        __init__, before Gtk.main() has started) since it spins up its
        own nested GLib.MainLoop to wait for the asynchronous response.
        """
        token = self._next_token(method.lower())
        request_path = "/org/freedesktop/portal/desktop/request/%s/%s" % (
            self._sender, token,
        )

        result = {}
        loop = GLib.MainLoop()

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            result["code"] = code
            result["results"] = results
            loop.quit()

        sub_id = self.bus.signal_subscribe(
            self.BUS_NAME, self.REQUEST_IFACE, "Response", request_path,
            None, Gio.DBusSignalFlags.NONE, on_response,
        )

        try:
            if isinstance(extra_args, dict):
                options = dict(extra_args)
                options["handle_token"] = GLib.Variant("s", token)
                args = (options,)
            else:
                # Contract: when extra_args isn't itself the options dict,
                # it must be a tuple whose last element is the a{sv}
                # options dict expected by the portal method's signature
                # (see the CreateSession/BindShortcuts call sites below).
                if not isinstance(extra_args[-1], dict):
                    raise TypeError("extra_args must end with an options dict")
                options = dict(extra_args[-1])
                options["handle_token"] = GLib.Variant("s", token)
                args = tuple(extra_args[:-1]) + (options,)

            self.bus.call_sync(
                self.BUS_NAME, self.OBJECT_PATH, self.IFACE, method,
                GLib.Variant(extra_sig, args), None,
                Gio.DBusCallFlags.NONE, -1, None,
            )

            timed_out = {"flag": False}

            def on_timeout():
                timed_out["flag"] = True
                loop.quit()
                return False

            timeout_id = GLib.timeout_add_seconds(
                self.CALL_TIMEOUT_SECONDS, on_timeout,
            )
            loop.run()
            GLib.source_remove(timeout_id)

            if timed_out["flag"]:
                raise RuntimeError("portal %s timed out" % method)
            code = result.get("code")
            if code != 0:
                description = self.RESPONSE_CODE_DESCRIPTIONS.get(code, "unknown")
                raise RuntimeError(
                    "portal %s failed (response code %s: %s)"
                    % (method, code, description)
                )
            return result["results"]
        finally:
            self.bus.signal_unsubscribe(sub_id)

    def _on_activated_signal(self, _conn, _sender, _path, _iface, _signal, params):
        session_handle, shortcut_id = params.unpack()[:2]
        if session_handle != self._session_handle:
            return
        self._on_activated(shortcut_id)

    def stop(self):
        if getattr(self, "_activated_sub", None):
            self.bus.signal_unsubscribe(self._activated_sub)
            self._activated_sub = None
        if self._session_handle:
            try:
                self.bus.call_sync(
                    self.BUS_NAME, self._session_handle,
                    "org.freedesktop.portal.Session", "Close",
                    None, None, Gio.DBusCallFlags.NONE, -1, None,
                )
            except Exception:
                pass
            self._session_handle = None


class PiMusicPlayerTray:
    WAIT_TICKS_DEFAULT = 50
    ANIM_TICK_MS = 100
    INFO_TICK_MS = 1000
    FADE_STEP = 0.1

    def __init__(self):
        self.general, self.hotkeys_cfg = load_config()

        self.player_url = self.general.get("PlayerUrl", "http://localhost:5000/")
        self.api_version = str(self.general.get("Version", "2"))
        self.popup_on_change = str(self.general.get("PopupChange", "true")).lower() == "true"
        self.wait_ticks = int(self.general.get("WaitTicksOnAutoPopup", self.WAIT_TICKS_DEFAULT))
        self.form_width = int(self.general.get("FormWidth", 400))
        self.form_height = int(self.general.get("FormHeight", 520))

        self.wait_timeout = self.wait_ticks
        self.do_not_auto_hide = False
        self.anim_state = "idle"  # idle, fade_in, wait, fade_out
        self.last_info = ""
        self.hotkey_listener = None

        self._build_window()
        self._build_indicator()
        self._register_hotkeys()

        GLib.timeout_add(self.INFO_TICK_MS, self._on_update_info_tick)
        GLib.timeout_add(self.ANIM_TICK_MS, self._on_anim_tick)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_window(self):
        self.window = Gtk.Window(title="PiMusicPlayerTray")
        self.window.set_default_size(self.form_width, self.form_height)
        self.window.set_decorated(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.connect("delete-event", self._on_window_close)
        self.webview = WebKit2.WebView()
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.load_uri(self.player_url)
        self.window.add(self.webview)

        self.is_loaded = False

    def _build_indicator(self):
        icon = self._resolve_icon()

        self.indicator = AppIndicator3.Indicator.new(
            "pimusicplayertray",
            icon,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu()

        show_item = Gtk.MenuItem(label="Show")
        show_item.connect("activate", lambda *_: self.show_form(True))
        menu.append(show_item)

        reload_item = Gtk.MenuItem(label="Reload")
        reload_item.connect("activate", lambda *_: self.reload())
        menu.append(reload_item)

        menu.append(Gtk.SeparatorMenuItem())

        exit_item = Gtk.MenuItem(label="Exit")
        exit_item.connect("activate", lambda *_: self.quit())
        menu.append(exit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def _resolve_icon(self):
        # AppIndicator expects an icon theme name or a path to a standard
        # image format (PNG/SVG); .ico files are not reliably supported.
        # Prefer the icon installed into the icon theme (e.g. by the
        # Flatpak/package), falling back to the bundled PNG shipped next to
        # this script or as a data file when running from an installed
        # (e.g. Flatpak) location.
        theme = Gtk.IconTheme.get_default()
        if theme is not None and theme.has_icon(APP_ID):
            return APP_ID

        candidates = [os.path.join(SCRIPT_DIR, "playericon_2.png")]
        for data_dir in GLib.get_system_data_dirs():
            candidates.append(os.path.join(data_dir, "pimusicplayertray", "playericon_2.png"))

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        return "multimedia-player"

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------
    def _register_hotkeys(self):
        entries = []  # [(name, value), ...]
        for name in HOTKEY_NAMES:
            value = self.hotkeys_cfg.get(name)
            if value:
                entries.append((name, value))

        if self._register_hotkeys_via_portal(entries):
            return
        self._register_hotkeys_via_pynput(entries)

    def _register_hotkeys_via_portal(self, entries):
        """Try registering global hotkeys through the xdg-desktop-portal
        GlobalShortcuts interface. This is the native, sandbox-friendly
        way to get global hotkeys on Wayland (and works on X11 too, when
        the portal backend supports it), so it's tried first. Returns True
        if registration succeeded, False if the caller should fall back to
        another backend (e.g. the portal or its GlobalShortcuts interface
        isn't available)."""
        shortcuts = []
        for name, value in entries:
            try:
                trigger = to_portal_trigger(value)
            except Exception as ex:
                print(
                    "Unable to register hotkey %s (%s): %s" % (name, value, ex),
                    file=sys.stderr,
                )
                continue
            shortcuts.append((name, name, trigger))

        if not shortcuts:
            return False

        try:
            self.hotkey_listener = PortalGlobalShortcuts(
                shortcuts, lambda name: GLib.idle_add(self._on_hotkey, name),
            )
        except Exception as ex:
            print(
                "Global hotkeys via the desktop portal are unavailable "
                "(%s); trying pynput instead." % (ex,),
                file=sys.stderr,
            )
            self.hotkey_listener = None
            return False

        print("Global hotkeys registered via the desktop portal.", file=sys.stderr)
        # The desktop only honors the requested trigger the first time a
        # shortcut is bound; after that it keeps the user's own choice.
        for name, _description, trigger in shortcuts:
            bound = self.hotkey_listener.bound_triggers.get(name, "")
            print(
                "  %s: bound to %s (config.ini: %s)"
                % (name, bound or "nothing", trigger),
                file=sys.stderr,
            )
        return True

    def _register_hotkeys_via_pynput(self, entries):
        """Fallback hotkey backend using pynput, which listens for key
        presses directly via X11. Works on X11 sessions and on Wayland
        sessions running under XWayland, but not on pure-Wayland sessions
        without XWayland, nor when the portal-based backend above isn't
        supported by the compositor."""
        if keyboard is None:
            print(
                "Global hotkeys disabled: pynput could not be initialized "
                "(%s). This typically happens on pure-Wayland sessions "
                "without XWayland." % (PYNPUT_IMPORT_ERROR,),
                file=sys.stderr,
            )
            return

        combos = {}
        for name, value in entries:
            try:
                combo = to_pynput_combo(value)
            except Exception as ex:
                print(
                    "Unable to register hotkey %s (%s): %s" % (name, value, ex),
                    file=sys.stderr,
                )
                continue

            if combo in combos:
                print(
                    "Hotkey %s (%s) conflicts with another hotkey using the "
                    "same combo; only the first one will be used." % (name, value),
                    file=sys.stderr,
                )
                continue

            def make_callback(hotkey_name):
                return lambda: GLib.idle_add(self._on_hotkey, hotkey_name)

            combos[combo] = make_callback(name)

        try:
            self.hotkey_listener = keyboard.GlobalHotKeys(combos)
            self.hotkey_listener.start()
        except Exception as ex:
            self.hotkey_listener = None
            print(
                "Global hotkeys disabled: failed to start the pynput "
                "listener (%s). This typically happens when no X server "
                "is reachable (e.g. a pure-Wayland session without "
                "XWayland)." % (ex,),
                file=sys.stderr,
            )
        else:
            print("Global hotkeys registered via pynput (X11).", file=sys.stderr)

    def _on_hotkey(self, name):
        self._reset_wait_timeout_if_active()
        try:
            if name == "PlayPause":
                self._run_js(self._api_call("playPause"))
            elif name == "Next":
                self._run_js(self._api_call("forward"))
            elif name == "Previous":
                self._run_js(self._api_call("rewind"))
            elif name == "Stop":
                self._run_js(self._api_call("stop"))
            elif name == "VolumeUp":
                self._run_js(self._api_call("volumeUp"))
            elif name == "VolumeDown":
                self._run_js(self._api_call("volumeDown"))
            elif name == "Refresh":
                self.reload()
            elif name == "Show":
                self.show_form(False)
        except Exception as ex:
            print("Unable to apply hotkey %s: %s" % (name, ex), file=sys.stderr)
        return False  # one-shot GLib idle callback

    def _api_call(self, action):
        if self.api_version == "2":
            return "API.%s()" % action
        legacy_name = LEGACY_API_FUNCTIONS.get(action)
        if legacy_name is None:
            raise ValueError("No legacy API mapping for action %r" % action)
        return "%s()" % legacy_name

    def _run_js(self, script):
        if not self.is_loaded:
            return
        self.webview.run_javascript(script, None, None, None)

    # ------------------------------------------------------------------
    # Info polling (tray tooltip / title)
    # ------------------------------------------------------------------
    def _on_load_changed(self, webview, load_event):
        if load_event == WebKit2.LoadEvent.STARTED:
            self.is_loaded = False
        elif load_event == WebKit2.LoadEvent.FINISHED:
            self.is_loaded = True

    def _on_update_info_tick(self):
        if not self.is_loaded:
            return True

        script = "API.getInfo()" if self.api_version == "2" else "playerGetInfo()"
        try:
            self.webview.run_javascript(script, None, self._on_info_ready, None)
        except Exception as ex:
            print("Unable to update tray text: %s" % ex, file=sys.stderr)
        return True

    def _on_info_ready(self, webview, result, user_data):
        try:
            js_result = webview.run_javascript_finish(result)
            js_value = js_result.get_js_value()
            info = js_value.to_string() if js_value else ""
        except Exception as ex:
            print("Unable to update tray text: %s" % ex, file=sys.stderr)
            return
        self._apply_info(info or "")

    def _apply_info(self, info):
        if len(info) >= 64:
            info = info[:60] + "..."

        if info != self.last_info:
            was_empty = not self.last_info
            self.last_info = info
            self.indicator.set_title(info)

            if self.popup_on_change:
                if self.anim_state == "idle" and not was_empty:
                    self.show_form(False)
                else:
                    self.wait_timeout = self.wait_ticks  # reset wait timeout

    # ------------------------------------------------------------------
    # Show / hide / animation, mirrors MainForm's fade in/out behavior
    # ------------------------------------------------------------------
    def _reset_wait_timeout_if_active(self):
        if self.anim_state != "idle":
            self.wait_timeout = self.wait_ticks

    def show_form(self, auto_hide):
        self.do_not_auto_hide = auto_hide

        # Bottom-right corner of the primary monitor's work area, so the
        # popup stays clear of panels and docks.
        display = Gdk.Display.get_default()
        monitor = None
        if display is not None:
            monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is not None:
            area = monitor.get_workarea()
            x = max(area.x, area.x + area.width - self.form_width)
            y = max(area.y, area.y + area.height - self.form_height)
            self.window.move(x, y)

        self.window.resize(self.form_width, self.form_height)
        self.window.set_opacity(0)
        self.window.show_all()

        self.anim_state = "fade_in"
        self.wait_timeout = self.wait_ticks

    def reload(self):
        self.webview.reload_bypass_cache()

    def _on_anim_tick(self):
        if self.anim_state == "fade_out":
            opacity = self.window.get_opacity() - self.FADE_STEP
            if opacity <= 0:
                self.window.set_opacity(0)
                self.window.hide()
                self.anim_state = "idle"
            else:
                self.window.set_opacity(opacity)
        elif self.anim_state == "wait":
            if self.do_not_auto_hide:
                if not self.window.is_active():
                    self.anim_state = "fade_out"
            else:
                if self.wait_timeout < 0:
                    self.anim_state = "fade_out"
                else:
                    self.wait_timeout -= 1
        elif self.anim_state == "fade_in":
            opacity = self.window.get_opacity() + self.FADE_STEP
            if opacity >= 1:
                self.window.set_opacity(1)
                if self.do_not_auto_hide:
                    self.window.present()
                self.anim_state = "wait"
            else:
                self.window.set_opacity(opacity)
        return True

    def _on_window_close(self, *_args):
        # Clicking the window close button just hides it, like minimizing to
        # tray on Windows. Use the tray menu's "Exit" to actually quit.
        self.window.hide()
        self.anim_state = "idle"
        return True  # stop the default handler from destroying the window

    def quit(self):
        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        Gtk.main_quit()


def main():
    Gtk.Window.set_default_icon_name(APP_ID)
    PiMusicPlayerTray()
    Gtk.main()


if __name__ == "__main__":
    main()
