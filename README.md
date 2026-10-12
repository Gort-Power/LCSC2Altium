# LCSC2Altium

Build Altium Designer component libraries straight from **LCSC part numbers**.

Enter `C25804` (or a list of them), pick a destination folder, preview the schematic
symbol and the PCB footprint, and save a ready-to-use library made of three files:

```
<Name>.LibPkg     package file tying the two libraries together
<Name>.SchLib     schematic symbols
<Name>.PcbLib     footprints (optionally with embedded STEP 3D bodies)
```

---

## Install and run

```bash
pip install -r requirements.txt
python main.py
```

On Windows, double-click `main.pyw` (or run `pythonw main.pyw`) to start the
application without opening a console window. Use `python main.py` when you want
console logging for troubleshooting.

`vtk` and `cadquery` are optional — they only power the 3D viewport. Without them the
rest of the application is fully functional.

The interface uses [PyQt6-Fluent-Widgets](https://pypi.org/project/PyQt6-Fluent-Widgets/)
for Fluent-styled controls, dialogs, menus and theme support. The library is licensed
under GPLv3; commercial use requires a separate license from its author.

---

## Using the application

The project `logo.png` is shown in the application toolbar, window icon and Windows
taskbar. Windows receives a stable application ID so the taskbar groups the app
consistently.

| Area | What it does |
|------|--------------|
| **Settings** (top bar) | Selects the `Dark`, `Light` or `System` theme and configures network proxy use. |
| **LCSC part number** | One part or many. Commas, semicolons and spaces all work: `C25804, C84681 C1525`. |
| **Library path** | Destination folder, chosen with **Browse…**. |
| **Library name** | Base name for the three generated files. |
| **Content** | Check boxes for `Symbols`, `Footprints` and `3D models` — tick any combination; everything ticked is written on **Save**. |
| **Download in library** | Fetches the parts and fills the list below. |
| **Components in library** | Everything queued for the library. **Right-click → Remove** deletes an entry. |
| **Symbol** viewport | Live schematic preview. The name field below is pre-filled and freely editable. |
| **Footprint** viewport | Switches between the 2D footprint and the 3D model. The name field below is pre-filled and editable; the embedded 3D body follows it. |
| **Save** | Writes the `.SchLib`, `.PcbLib` and `.LibPkg`. |

Selecting a row in the list drives both viewports. Renaming a symbol or footprint takes
effect on the next save.

The 2D footprint preview shows copper, silkscreen and Multi-Layer geometry. Copper is
red and silkscreen is yellow; Multi-Layer pads are shown as copper on both sides.
Drill holes are preserved as white cutouts in the preview.
Every pad is labeled with a centered black vector designator sized to fit its bounds.
Equal-size pads use a consistent font scale, labels remain upright on rotated pads, and
pads without a source number use their order in the footprint.

The Settings dialog offers direct connection, system proxy, or a manually configured
proxy. Manual proxy settings use a hostname/IP address and port; the same HTTP proxy is
used for both HTTP and HTTPS requests. Settings are saved in `settings.json` in the
application's current launch directory. Existing settings from the user profile are
read as a one-time fallback and are saved to the launch directory the next time settings
are changed. Use **Check connection** in the dialog to verify the selected route to the
EasyEDA component API before downloading parts.

### What the content check boxes write

Any combination may be ticked. At least one is required.

| Ticked | `.SchLib` | `.PcbLib` |
|--------|-----------|-----------|
| `Symbols` | symbols | written, empty |
| `Footprints` | written, empty | footprints |
| `3D models` | written, empty | footprints **with the STEP body embedded** |
| any combination | whatever was ticked | whatever was ticked |

A 3D model cannot exist without the footprint that carries it, so ticking `3D models`
also fetches and writes the footprints. The default is `Symbols` + `Footprints`.

The content check boxes decide both what is **downloaded** and what is **written on
save**, so ticking `3D models` is what pulls the (large) STEP payloads down.

---

## Project layout

```
main.py                              entry point
lcsc2altium/
  constants.py                       endpoints, unit maths, layer translation
  models.py                          typed geometry records
  easyeda_client.py                  HTTP access to the EasyEDA/LCSC API
  easyeda_parser.py                  EasyEDA primitive strings -> typed geometry
  preview.py                         SVG rendering used by the viewports
  builders/
    schlib_builder.py                authors the .SchLib
    pcblib_builder.py                authors the .PcbLib, embeds the STEP bodies
    libpkg.py                        writes the .LibPkg package file
    library.py                       orchestration: fetch -> author -> write
  ui/
    theme.py                         Fluent-backed Dark / Light / System themes
    widgets.py                       Fluent list, SVG viewport and shared widgets
    step_viewer.py                   VTK + CadQuery 3D preview
    main_window.py                   the application window
tests/                               offline unit tests
```

---

## How the conversion works

### Data source

Everything comes from the public EasyEDA endpoints that back the LCSC pages:

* `GET /api/products/{lcsc}/components` — the schematic symbol, plus `packageDetail`
  with the matching footprint
* `GET /api/eda/product/search` — manufacturer, stock and price, used to enrich the entry
* `GET modules.easyeda.com/…/{uuid}` — the AP214 STEP payload for the 3D body

### Units

EasyEDA's internal unit is **0.254 mm (1/100 inch)**, i.e. **10 Altium mils**. This was
verified against a SOP-16 package, where the source reports a `5.0` pad pitch that must
come out as `50` mils. The parser keeps everything in mils from end to end and applies
that single factor once per object.

### Symbol

Pins, rectangles, polylines, polygons, ellipses and text are translated into Altium
symbol-local mils with the Y axis flipped (EasyEDA is Y-down, Altium schematics are
Y-up). Two parameters are always written, as required:

```
MPN                manufacturer part number
LCSC Part Number   the LCSC code
```

Choices worth knowing about:

* **Every pin is `Passive`.** EasyEDA stores "Input"/"Output"/"Undefined"
  inconsistently for passive parts -- a chip resistor comes back as an input -- so
  honouring the source value produced meaningless ERC reports.
* **Pin rotation is passed through unchanged.** EasyEDA and Altium use the same
  convention; an earlier 180 degree offset turned every pin on its head.
* **Pin positions are snapped to the 50 mil grid** (`SCHEMATIC_GRID_MILS`), Altium's
  default snap, so pins do not sit a few mils off the grid. The snap is applied
  *after* the unit scaling — the grid is in mils, so snapping the raw EasyEDA units
  would quantise pins onto a 500 mil pitch.

### Footprint

Pads (rectangular, round, oval and custom polygon), tracks, arcs, circles, filled
rectangles, solid regions, non-plated holes and silkscreen text are mapped onto their
Altium layers. Oval pads become 50 % rounded rectangles, which is the stadium shape
Altium can actually represent.

Four details are easy to get wrong and are verified against EasyEDA's own footprint
SVG:

* **`TRACK` stores the width first and the layer second** (`TRACK~width~layer~…`), the
  opposite of what the field order suggests. A DIP-8 silkscreen line is 1 EasyEDA unit
  = 0.254 mm and must arrive as 10 mils, not as 3 units (0.762 mm).
* **The drill is stored as a radius** and must be doubled to obtain an Altium drill
  diameter.
* **A slotted hole is two points plus a drill radius.** EasyEDA writes the ends of the
  slot axis into the polygonal field rather than setting a shape flag, so a two-point
  polygon on a pad that has a hole is read as a slot. Altium's `slot_length` is the
  overall length including the round ends, so the straight part is added to the drill
  diameter. Plain round holes carry no polygon and stay round.
* **An Altium `PcbLib` is Y-up while EasyEDA footprints are Y-down**, so every footprint
  is mirrored on Y as it is parsed; rotations follow the same reflection (angles about
  X and Z reverse, angles about Y do not). Without this the footprint arrives upside
  down, with pin 1 on the wrong corner. The pin-1 marker stays next to pad 1, which is
  how the orientation is checked.
* **Polygon pads**: when the outline is a rectangle it is emitted as an ordinary
  rectangular pad (EasyEDA traces these with 16 jittered vertices, and a region-backed
  custom pad is not placed correctly by the preview renderer); genuinely custom
  outlines become real custom pads.

Two simplifications are deliberate:

* **Copper-layer tracks are dropped** where a package stores placement helpers
  (courtyard and body outline) on copper: EasyEDA never draws them, so emitting them
  would place phantom copper on the board.
* **Paste-layer regions that mirror their own pad are dropped.** Altium derives the
  paste layer from the pad, and keeping both duplicated the stencil apertures.

### Origin and centring

The footprint and symbol origins are computed from the **converted geometry**, not from
EasyEDA's `BBox`. On several packages `BBox` describes the canvas rather than the
drawing -- the USB-C symbol declares a 92 × 130 box while its pins span 700 units --
which pushed the component off centre. Recentring runs before scaling, so pad outlines
(which are stored relative to their anchor) must not be shifted with it.

### 3D bodies

The STEP payload is embedded in the `.PcbLib` and referenced from a component-body
record. The model and body take the footprint's name, so renaming the footprint in the
UI renames its 3D body too.

The body is placed on the footprint origin with no offset, because Altium centres an
embedded model there; EasyEDA's `c_origin` is an offset inside the package rather than
a placement. Rotation is kept, since some packages ship the STEP rotated relative to
the land pattern.

The 3D viewport follows the approach of
[STEP-Viewer-Pro](https://github.com/VolkanSah/STEP-Viewer-Pro): CadQuery performs the
tessellation, VTK renders it and handles the orbit camera.

**Colours** come from the STEP file itself. The file is read through STEPCAF
(`STEPCAFControl_Reader`, which preserves the colour table) and each face is
triangulated separately so its colour can be attached per vertex; VTK then shades the
model with the part's real materials instead of a single flat tint. When a file carries
no colour information the mesh falls back to a neutral grey, and CadQuery's plain
tessellation is used if the coloured path is unavailable for any reason.

STEP parsing and tessellation run on a background thread, avoiding the startup and
mesh-serialization overhead of a separate process while keeping the UI responsive.
Selecting another component discards the previous preview result. This simplification is
preview-only: the original STEP data embedded in the exported library is unchanged.
The preview skips the normals pass and uses unlit rendering to avoid doing expensive
whole-mesh work on the UI thread.

Four VTK 9.6 API changes are handled, each of which otherwise stopped the body from
appearing:

* `vtkCellArray` and `vtkPolyData` moved from `vtkCommonCore` to `vtkCommonDataModel`
* the `InsertNextCell(npts, vtkIdList)` overload is gone, so the id list is passed alone
* `ResetCameraClippingRange()` lives on `vtkRenderer`, not on the render window
* there is no `SetColorModeToDirectScalars` in this binding; VTK picks up colour
  scalars on its own once they are on the points

The interactor is created while hidden, so the viewport re-renders (and resizes the
render window) when it is first shown; without that the canvas stays blank.

The widget is only created when a real OpenGL context exists — on headless platforms
VTK aborts the process rather than raising, so it is skipped there and the viewport
explains why. Set `LCSC2ALTIUM_DISABLE_3D=1` to switch it off explicitly.

### Known source-data limitations

Some EasyEDA library entries are themselves incomplete. C165948 (USB-C 31-pin, for
instance) ships only four silkscreen polylines, which outline roughly the lower two
thirds of the shell and leave the contact-pad edge bare; EasyEDA's own renderer draws
exactly the same four segments, so the conversion is faithful and does not invent
geometry. Such gaps are worth reviewing before placing the part.

---

## Tests

```bash
python -m pytest tests -q
```

The offline tests cover LCSC-number parsing, the EasyEDA primitive decoders, unit
conversion, layer translation, SVG rendering and the `.LibPkg` writer. They do not need
network access.

---

## Attribution

Part data is **JLCEDA / EasyEDA Official Library** (<https://lceda.cn>, <https://easyeda.com>).
That library's payload carries a licence notice that explicitly covers AI tooling and
requires the result to state the source, so every generated library carries a `Source`
parameter, and the same credit is shown in the status bar and the window footer.
