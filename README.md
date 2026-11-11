# Scroll Speed

Set the speed of the mouse wheel on GNOME, the setting that is missing next to
"Pointer speed". Slower, faster, a separate horizontal speed, optional
acceleration when the wheel spins fast. It works in every application, on
Wayland and X11.

[Version française](README.fr.md)

Status: 0.1, built and tested on GNOME 51 / Ubuntu 26.10. The whole chain is
covered by automated tests on real kernel devices, but it has not yet been
used with a physical mouse (see [What is tested](#what-is-tested)).

## Install

```bash
./pack.sh
gnome-extensions install --force scroll-speed@griffit.gmail.com.shell-extension.zip
```

Log out and back in (Wayland cannot load a new extension into a running
shell), enable *Scroll Speed*, open its preferences and:

1. click **Authorize…** once and type your password;
2. move the **Scroll speed** slider. The change applies immediately; the
   *Try it* area at the bottom is there to feel it.

To try it without logging out:

```bash
./try-now.sh 0.5
```

It asks for the same authorization if it is missing, then halves the wheel
speed until Ctrl+C.

## How it works

```
mouse ──► kernel evdev ──► helper (grab + rescale wheel) ──► uinput clone ──► libinput ──► GNOME ──► apps
```

`extension/helper/scroll_speed_helper.py` (Python, standard library only)
grabs each wheel mouse, creates a virtual clone with the same identity and
buttons, and forwards every event unchanged except the wheel. Wheel deltas are
multiplied by the factor and sent in the kernel's high-resolution format
(120 units per notch), so ×0.5 reaches applications as half a notch rather
than "one notch out of two". Applications that only understand whole notches
get one every time a full notch has accumulated.

The extension starts the helper, sends it the settings as they change, and
stops it when the extension is disabled. The helper exits by itself if the
shell goes away, which gives the mouse back. At speed 1× with no acceleration
the mice are not touched at all.

### The authorization

The helper needs to read the mouse and to create a virtual one. **Authorize…**
installs `/etc/udev/rules.d/70-scroll-speed.rules` through polkit:

- access to `/dev/uinput` and to mouse event nodes for the user logged in on
  the seat (`uaccess`, the mechanism udev uses for joysticks and sound cards);
- keyboards and pointing sticks are excluded, so the rule never exposes
  keystrokes;
- clones get the hardware-database entry of the mouse they mirror (DPI,
  trackball flag), so the pointer moves exactly as before.

Granting access to `/dev/uinput` lets programs of your session create input
devices; Steam's udev rules do the same. **Remove…** in the preferences
deletes the rule.

## Why not a plain extension?

That was the first thing tried, and it is not possible on GNOME 51:

- Mutter hands a scroll event to the Wayland client *before* extension code
  runs, and events delivered to a window never reach the shell's JavaScript at
  all. An extension can neither change nor even see them.
- libinput can scale scrolling (custom acceleration profile, scroll type), but
  Mutter only passes the *pointer motion* curve from
  `org.gnome.desktop.peripherals.mouse custom-accel-config`.
- libinput 1.30 has Lua plugins and Mutter 51 loads them from
  `~/.config/libinput/plugins`, which would need no privilege at all, but
  Ubuntu builds libinput without Lua.

So the wheel is rescaled below the compositor. If Mutter ever exposes the
scroll curve, or distributions enable Lua plugins, the helper can be replaced
without changing the preferences.

## Limits

- Mouse wheels only. Touchpad scrolling is computed inside libinput and
  pointing sticks scroll by button; neither is affected.
- A combined keyboard-and-mouse device that shows up as a single node is left
  alone (the rule gives no access to keyboards).
- A mouse grabbed by another program (an input remapper) is skipped and
  retried every 30 seconds.
- The extension is disabled on the lock screen, like any extension, so the
  wheel is back to normal there.

## What is tested

`tests/run.sh` runs everything:

- 24 unit tests of the scaling logic (remainders, direction changes,
  acceleration, low-resolution mice);
- 11 end-to-end tests of the helper against real kernel devices: a fake
  source created with uinput is grabbed, cloned and rescaled, settings change
  live, the mouse is given back on neutral settings, on exit and on SIGTERM,
  hot-unplug, a held button delays the grab, a second helper refuses to start;
- unit tests of the preferences logic, and of the exact udev rule the install
  command writes (`udevadm verify`).

Checked by hand on GNOME 51:

- a virtual mouse sending 60/120 of a notch makes a GTK 4 window scroll by
  exactly 0.5 (12/120 gives 0.1, 240/120 gives 2);
- the extension in a headless shell: helper started, settings forwarded,
  stopped on disable, restarted after a crash;
- the udev rule in `udevadm test`: mice and uinput get access, the keyboard
  does not, a clone gets its `MOUSE_DPI` back.

Not tested yet: a physical mouse end to end, and the polkit prompt.

`tests/prefs_preview.js` opens the preferences window outside the shell and
can save it as a PNG.

## Uninstall

Preferences › **Remove…**, then `gnome-extensions uninstall scroll-speed@griffit.gmail.com`.

## License

GPL-2.0-or-later.
