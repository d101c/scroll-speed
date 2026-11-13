# Changelog

## 0.3

Changes made for the extensions.gnome.org review.

- The description now states plainly what the udev rule opens: `/dev/uinput`
  (virtual input devices) and mice, touchpads and pointing sticks, with
  physical keyboards excluded from capture; the rule stays installed until
  "Remove…" is clicked, even after uninstalling the extension.
- The helper's lock file lives only in `$XDG_RUNTIME_DIR` (opened with
  `O_NOFOLLOW`, mode 0600); the `/tmp` fallback is gone and the helper exits
  with status 4 and a message when the variable is missing.
- `GLib.Source.remove` replaces the deprecated `GLib.source_remove`.
- The LICENSE file is shipped inside the zip.
- Trimmed comments and leftover markers; `--device` and `--kind` are listed
  as development options in `--help`.

## 0.2

First published version: mouse wheel, touchpad and pointing stick speed, the
udev rule installed from the preferences.
