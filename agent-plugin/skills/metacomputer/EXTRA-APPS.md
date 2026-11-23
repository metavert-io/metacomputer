# Apps added with EXTRA_PACKAGES

These apps aren't in the standard VM. They were verified in VMs built with `EXTRA_PACKAGES` between 2026-11-16 and 2026-11-17 (Inkscape 1.2, Audacity 3.4, Kdenlive 23.08, pygame 2.5). The general rules, the error table and the file-dialog steps are in [DESKTOP.md](DESKTOP.md).

## Inkscape

- **Never open its dialogs through accessibility actions.** `invoke_menu {path:["File","Save As..."]}` froze it, and so did pressing the Export button by token (its "Export in progress" window stayed open, but the PNG had already been written). Use the shortcuts (Ctrl+S, Ctrl+Shift+S, Ctrl+Shift+E) and real pointer clicks. Save early, so a hang loses nothing.
- **Export:** in the Export panel, click the filename field, press Ctrl+A, type the full path, press Return. It exports straight away, and asks before replacing a file. After an export, keyboard focus stays in that field and one-key shortcuts ("5") type into it: click empty canvas first.
- **Background pixel clicks hit hidden widgets.** Canvas points, colour fields and the Fill and Stroke tabs need foreground pixel clicks: a background click on the docked notebook pressed a button on a hidden tab.
- **Selecting several objects:** Shift+click and Shift+drag never reach the canvas. Use a rubber-band drag from empty canvas, or put the objects in one group and press Ctrl+A inside it.
- **The XML Editor gives exact shapes.**
  - Its rows are table cells with no position: click them by pixel from a fresh screenshot, values at x ≈ 330. The Name and Value border moves, so look before aiming.
  - Re-read after deleting or duplicating a node: the rows shift.
  - If Return adds a newline instead of committing a value, delete the newline and click ✓.
  - The "+" (add attribute) button needs a real press: a zero-length `drag` on it.
  - Edit a star through its toolbar fields, not its `sodipodi` attributes, and change a text's `<tspan>` x/y along with the `<text>`; otherwise the saved `d` and positions go stale.
  - A floating XML Editor can cover the docked Export panel, and Alt+F4 won't close it: click its title-bar ✕ at desktop scope.
- **Pasting SVG markup:** `clipboard_write` offers only plain UTF-8, and Inkscape's Ctrl+V ignores it. Relay it through the Text tool: paste into a text object, Ctrl+A, Ctrl+C, Escape, delete the text, then Ctrl+V.
- **Units and filters.** Set the display units to px along with the document units. Inkscape 1.2 has no "Outer Glow" filter: Drop Shadow with zero offset gives the same result. Fill and Stroke's Blur % is quadratic: about 31% gives σ ≈ 7 on a 180 px shape.

## Audacity

- **The waveform, the timeline and the Selection toolbar aren't in the tree.** Select audio by pixel. For exact times, click the Selection toolbar's first digit, type the digits, then Ctrl+E to zoom to the selection.
- **Menus with the keyboard** (Alt+C for Effect, arrow keys, Return); a pointer press didn't reach an open menu. After the first effect the Effect menu gains a "Repeat …" item, so the Down count shifts by one: look before pressing Return.
- **Effect dialogs (Normalize, Amplify):** `set_value` applies the effect at once. Type into the field instead; Edit › Undo shows whether an effect was applied.
- **Export:** Ctrl+Shift+E (Export Audio) with a typed path, as in [DESKTOP.md](DESKTOP.md#file-dialogs-gtk-qt-libreoffice), and press Export with a real click: an accessibility press hung the app at the "Replace?" prompt. Ctrl+O opens files the same way.
- **Ctrl+W leaves a new window with a new id.** Run `list_windows` before the next read.
- **dB scale:** right-click the vertical scale.

## Kdenlive and other Qt apps

- **Use real clicks for menus and pop-ups.** A background click on a pop-up menu item landed on a hidden "Delete Clip" button behind it (Kdenlive asked for confirmation, so nothing was lost).
- **The timeline is QML** and isn't in the tree: select clips by pixel.
- **Window screenshots hide the area under the pointer.** Move the pointer away first, or use `zoom`.
- **`right_click` has no `scope`:** pass `coordinate_frame: "desktop"` with `pid` and `window_id`.
- **Placing the playhead exactly:** triple-click the monitor's timecode, type `HH:MM:SS:FF`, press Return.
- **Saving:** on a new project File › Save is disabled, so use Save As. Qt file dialogs take a full path typed into the name field, then Return.
- **First run** warns that `frei0r-plugins` and `mediainfo` are missing. Add both to `EXTRA_PACKAGES`.

## SDL and pygame windows

- **`get_window_state` screenshots of them are black;** use `get_desktop_state`. Their contents aren't in the tree.
- **A game that mirrors its state in the window title** can be read with `list_windows`. For turn-based play, use one `run_script` per turn: press the key, then poll `cua.listWindows` until the title says READY (about 0.55 s per turn).
- **Keys:** `press_key {scope: "desktop"}` after `bring_to_front`.
- **A game started from a terminal** by a foreground step can open behind the terminal.
- **`get_desktop_state` with `max_image_dimension`:** later `scope: "desktop"` clicks use that smaller frame's coordinates (×1.5 for 1280). Pick one capture size and stay with it.

## Viewing images

No image viewer comes with the standard VM. Open PNGs in Firefox, or add `ristretto` to `EXTRA_PACKAGES`.
