# Flatpak packaging for PiMusicPlayerTray

This directory contains everything needed to build PiMusicPlayerTray-linux
as a Flatpak, so it can run on any Linux distribution without manually
installing GTK/WebKit/AppIndicator system packages.

## Installing the latest build

Download `PiMusicPlayerTray.flatpak` from the repository's
[latest release](https://github.com/drakarah/music-player-v2/releases/latest),
then install and run it:

```bash
flatpak install --user PiMusicPlayerTray.flatpak
flatpak run org.drakarah.PiMusicPlayerTray
```

## Files

- `org.drakarah.PiMusicPlayerTray.yml` — the Flatpak manifest.
- `org.drakarah.PiMusicPlayerTray.desktop` — desktop entry (app menu, icon).
- `org.drakarah.PiMusicPlayerTray.metainfo.xml` — AppStream metadata.
- `python3-pynput.json` — builds `pynput` and its dependencies (`six`,
  `python-xlib`, `evdev`) from pinned PyPI sources so the build works fully
  offline once the sources are downloaded.
- `shared-modules/` — copies of the relevant modules from the
  [flathub/shared-modules](https://github.com/flathub/shared-modules)
  repository, used to build `libayatana-appindicator` (and its `intltool`
  dependency) from source, since it is not part of the GNOME runtime.

The app itself (`main.py`, `config.ini`, `playericon_2.png`) is picked up
directly from `../PiMusicPlayerTray-linux`.

## Building locally

Requires `flatpak` and `flatpak-builder`, plus the GNOME 48 runtime/SDK:

```bash
flatpak install -y flathub org.gnome.Platform//48 org.gnome.Sdk//48

flatpak-builder --user --install --force-clean build-dir \
    org.drakarah.PiMusicPlayerTray.yml
```

Then run it with:

```bash
flatpak run org.drakarah.PiMusicPlayerTray
```

## Permissions (finish-args)

- `--socket=wayland` / `--socket=x11`: needed to show the popup window.
  The app prefers X11 (XWayland on Wayland sessions), since Wayland doesn't
  let apps place their own windows and the popup would be centered instead
  of in the corner; `pynput`'s hotkey fallback also needs X11.
  `--socket=fallback-x11` is deliberately not used: on Wayland it hides the
  X11 socket.
- `--socket=pulseaudio`: audio output for playback; without it the player
  reports that the media could not be loaded.
- `--share=network`: the popup loads the MusicPlayerV2 web page, normally
  served from `localhost`.
- `--filesystem=xdg-config/pimusicplayertray:ro`: read the user's
  `~/.config/pimusicplayertray/config.ini`.
- `--talk-name=org.kde.StatusNotifierWatcher` / `--own-name=org.kde.StatusNotifierItem-*`:
  required for the AppIndicator-based tray icon to register itself.

## Configuration

The app ships `config.ini` with the same defaults as the Windows version.
Users can override any setting (e.g. `PlayerUrl` or a hotkey) by creating
`~/.config/pimusicplayertray/config.ini`. The manifest grants read-only
access to that directory (`--filesystem=xdg-config/pimusicplayertray:ro`),
since the sandbox otherwise only sees its private
`~/.var/app/org.drakarah.PiMusicPlayerTray/config/`. A config.ini placed in
that private directory is also read and takes precedence. Only the keys you
want to change need to be present; anything else falls back to the bundled
default.

## Updating pinned pip sources

If `pynput` (or one of its dependencies) needs to be updated, regenerate
`python3-pynput.json` with the new PyPI download URLs and `sha256` hashes,
e.g. using [flatpak-pip-generator](https://github.com/flatpak/flatpak-builder-tools/tree/master/pip).
