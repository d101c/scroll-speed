// SPDX-License-Identifier: GPL-2.0-or-later
// Opens the real preferences window outside GNOME Shell and saves a PNG of it.
//
//   GSETTINGS_BACKEND=memory gjs -m tests/prefs_preview.js out.png [key=value…]
//
// With the memory backend nothing is written to the user's settings. Extra
// arguments preset double/boolean keys, e.g. vertical-factor=0.5.
import Adw from 'gi://Adw?version=1';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk?version=4.0';
import System from 'system';

const [output, ...presets] = ARGV;
const sourceDir = Gio.File.new_for_path(
    GLib.build_filenamev([GLib.path_get_dirname(System.programPath), '..', 'extension']));

// The preferences import GNOME Shell's own prefs module, which lives in the
// Extensions app resource bundle.
Gio.Resource.load('/usr/share/gnome-shell/org.gnome.Shell.Extensions.src.gresource')._register();
const {extensionManager} = await import('resource:///org/gnome/Shell/Extensions/js/extensionsService.js');

const [, raw] = sourceDir.get_child('metadata.json').load_contents(null);
const metadata = JSON.parse(new TextDecoder().decode(raw));

// gettext finds the calling extension from a ".../gnome-shell/extensions/<uuid>/"
// path in the stack, so the source tree is reached through such a path.
const root = GLib.dir_make_tmp('scroll-speed-preview-XXXXXX');
const parent = GLib.build_filenamev([root, 'gnome-shell', 'extensions']);
GLib.mkdir_with_parents(parent, 0o700);
const extensionDir = Gio.File.new_for_path(GLib.build_filenamev([parent, metadata.uuid]));
extensionDir.make_symbolic_link(sourceDir.get_path(), null);
metadata.dir = extensionDir;
metadata.path = extensionDir.get_path();

let prefs = null;
extensionManager.lookup = uuid => uuid === metadata.uuid && prefs ? {stateObj: prefs} : null;

const {default: Preferences} = await import(`file://${extensionDir.get_path()}/prefs.js`);
prefs = new Preferences(metadata);

const settings = prefs.getSettings();
for (const preset of presets) {
    const [key, value] = preset.split('=');
    if (value === 'true' || value === 'false')
        settings.set_boolean(key, value === 'true');
    else
        settings.set_double(key, Number(value));
}

const app = new Adw.Application({application_id: 'dev.griffit.ScrollSpeedPreview'});
app.connect('activate', () => {
    const window = new Adw.PreferencesWindow({application: app, default_width: 640, default_height: 760});
    prefs.fillPreferencesWindow(window);
    window.present();
    if (!output)
        return;
    // Leave time for the first frame and the first probe of the status group.
    GLib.timeout_add(GLib.PRIORITY_LOW, 3000, () => {
        const paintable = new Gtk.WidgetPaintable({widget: window});
        const snapshot = Gtk.Snapshot.new();
        paintable.snapshot(snapshot, window.get_width(), window.get_height());
        const texture = window.get_renderer().render_texture(snapshot.to_node(), null);
        texture.save_to_png(output);
        print(`saved ${output}`);
        extensionDir.delete(null);
        app.quit();
        return GLib.SOURCE_REMOVE;
    });
});
// runAsync, not run: this module awaits above, and a main loop started from a
// pending promise job would keep every other promise from resolving.
await app.runAsync([]);
