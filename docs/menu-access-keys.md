# Keyboard access to localized menus

On Linux and Windows, `app_menu::OpenApplicationMenu` accepts a stable English
menu name, independent of the display language. Add an object like this to the
array in your `keymap.json`:

```json
{
  "context": "Workspace",
  "bindings": {
    "alt-f": ["app_menu::OpenApplicationMenu", "File"],
    "alt-e": ["app_menu::OpenApplicationMenu", "Edit"],
    "alt-v": ["app_menu::OpenApplicationMenu", "View"],
    "alt-h": ["app_menu::OpenApplicationMenu", "Help"]
  }
}
```

The other stable names are `Zed`, `Selection`, `Go`, `Run`, and `Window`.
Existing bindings using the current translated name (for example `ファイル`)
continue to work, but English names are recommended so bindings survive a
display-language change.

The client-side menu bar adds a hint for an available single Alt+letter binding:
for example, `ファイル (F)` or `File (F)`. Rebinding the menu to Alt+X changes
the hint to `(X)`. The suffix is only a display hint; do not include it in the
action's menu name. Menus without an applicable Alt+letter binding have no
suffix. Chords, modified combinations such as Ctrl+Alt+F, and function keys
are not represented by a single-letter hint.

The hint reflects your actual, currently active keymap, not just the binding
you wrote. If the letter you picked is already bound to something else in a
more specific context (for example Zed's default `alt-r` / `alt-w` search
toggles inside a `Pane`), that binding wins while that context is active, the
menu binding is shadowed, and the hint disappears even though the entry in
`keymap.json` is still there. Check `zed: open default keymap` for existing
Alt+letter bindings before picking one.

This does not add default Alt bindings, underline letters, implement Alt-only
activation, or add letter selection inside an open menu. Native macOS menus
retain their existing behavior.
