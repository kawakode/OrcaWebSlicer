# Projects and thumbnails

Status: Active contract; added after G5

A plate composed in the browser can be saved as a 3MF project that OrcaSlicer
desktop opens, and reopened in the browser later. The archive is written by a
disposable worker through the engine's own 3MF exporter (`store_bbs_3mf`), so
the API process still never reads or writes model geometry.

## Saving a project

`POST /api/v1/projects` takes exactly the body of a slice request, without
`thumbnails`, and starts a job whose worker operation is `export_project`. The
worker builds the plate the way it would slice it: the same objects and
transforms, the same profile chain, and the same overrides, validated the same
way. Instead of slicing, it writes the `project` artifact, served as
`model/3mf` and named `<job_id>.3mf`.

The archive holds:

- every object with its instance placements, lifted onto the bed exactly as a
  slice would lift them;
- one plate that holds every instance, with the printer model and nozzle
  diameter the desktop shows;
- the effective configuration as `Metadata/project_settings.config`;
- a 512 x 512 plate thumbnail, plus the small one the exporter derives.

The configuration names the selection the way the desktop does:
`printer_settings_id`, `print_settings_id`, and `filament_settings_id` are the
names of the selected bundled presets, and `different_settings_to_system`
lists the overridden settings in the desktop's order (process, each filament,
printer). The desktop therefore opens the project on the same system presets,
marked as modified by the overrides. `inherits`, `compatible_printers`, and
the other keys the desktop strips from a project are removed. Enum lists the engine
defaults build without their value names, such as `extruder_type`, are given
those names first, because an empty token cannot be read back by any importer.

A plate may be saved with objects outside the printable volume, as on the
desktop; only slicing refuses them. Multiple plates are not exported yet: the
browser composes one.

## Reopening a project

A saved project is uploaded like any other 3MF. Its inspection reports
`declares_plate: true`, so the plater keeps every placement, and adds a
`project` object when the archive names its presets:

```json
"project": {
  "printer": "Anycubic Kobra 0.4 nozzle",
  "process": "0.20mm Standard @Anycubic Kobra",
  "filaments": ["Anycubic Generic PLA"],
  "settings": {"layer_height": "0.28"}
}
```

`settings` holds every key `different_settings_to_system` lists, serialized as
a request submits it; a filament setting keeps the first filament's value. The
same field appears for a project saved by the desktop.

When the project is opened on an empty plate, the browser selects the bundled
presets with those names and restores the overrides that are in the settings
catalog. A printer this deployment does not bundle keeps the current
selection, and the screen says so. A project added to a plate that already
holds objects only adds its objects.

Slicing a reopened plate submits its objects with the archive as their model,
so the archive's embedded configuration is the base and the selected profiles
and overrides are applied over it, as for any project.

## G-code thumbnails

A slice request may set `thumbnails: true`. The worker then passes a thumbnail
renderer to G-code export, which embeds one image per size the printer's
`thumbnails` setting asks for, in its `thumbnails_format`. The default is off,
so existing requests produce unchanged G-code; the browser turns it on by
default. Bambu Lab printers carry their thumbnails in a different file and are
unaffected.

The renderer is the worker's own, in `src/libslic3r/Web/PlateThumbnail.cpp`.
It draws every printable part of every printable instance, flat shaded in the
first filament's colour, from a fixed isometric view fitted to the plate's
contents, on a transparent background. It needs no OpenGL context, so the
headless worker keeps its dependency audit; the desktop renders its own
thumbnails unchanged.

## Tests

`tests/libslic3r/test_project_export.cpp` saves a two-instance plate, reloads
it through `load_bbs_3mf` with the desktop's load strategy, and checks the
plate, the placements, the preset names, the overrides, and the thumbnail. It
also reopens the archive through an inspection, slices it, and checks that
G-code carries thumbnails only when asked. `tests/web/test_api.py` covers the
route, the artifact, and the `thumbnails` field, and
`web/frontend/e2e/project.spec.ts` saves a project, reopens it in a fresh
session, and slices it where it was saved.
