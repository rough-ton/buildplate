# Using Studio

Studio has three parts: the conversation on the left, the build plate in the middle, and the inspector on the right.

## Writing a good request

The agent works best with the same information you would give a person designing the part:

- **Sizes that matter**, in millimetres: the diameter it has to fit, the screw size, the thickness you want.
- **How it is used**: "holds a 300 g spool", "mounts under a desk", "press fit into a 12 mm hole".
- **What to leave flexible**: "make the length a parameter".

Starting points:

> A cable comb for eight 6 mm cables, 3 mm thick, with two 4 mm mounting holes.

> A drawer divider insert 120 x 80 x 40 mm with three equal compartments and 1.6 mm walls.

Press **Enter** to send. Use **Shift+Enter** for a new line.

## Attaching a reference file

Show the agent what you mean by attaching one file to a request: select the paperclip next to the model picker, drag a file onto the prompt, or paste a picture. The agent uses it as a reference to match, and still writes its own parametric design.

| File | What the agent gets |
| --- | --- |
| Picture (PNG, JPG, WebP) | The picture itself. Needs a model that accepts images; with one that does not, Buildplate says so before sending anything, and keeps your request and the file so you can pick another model. |
| STL or 3MF | Its size, volume and general shape, plus a four-view render for models that accept images. |
| SCAD | The source, marked as a reference so it is never mistaken for the design being edited, plus a render when it renders. To keep working on an existing OpenSCAD design instead, paste it into the **Code** tab. |
| G-code | The printed area, height, layer count and slicer, recovered from the toolpath (so approximate), plus a top and front drawing of the toolpath for models that accept images. |

The file, or its summary, appears in your message in the conversation. Later requests in the same conversation recall it by name and summary only, so it is not sent again. Limits: pictures 15 MB, STL, 3MF and G-code 50 MB, SCAD 512 KB.

## What happens during a request

You see each step as it runs:

1. The model writes the full OpenSCAD source.
2. Buildplate renders it and checks the mesh: it must render, be watertight, sit on the bed at Z=0, and fit your build volume.
3. If the model supports vision, it also sees a four-view preview.
4. The model fixes anything wrong and checks again, up to the step limit in Settings.
5. The final design is saved as a new version and placed on the plate.

Select **Stop** at any time to cancel. Tokens already used are still recorded.

Under each answer you see the tokens and cost for that request. The total for your current session appears below the prompt.

## Changing a design

Keep typing in the same conversation to refine it: "make it 10 mm taller", "add a lip around the top", "the holes should be 5 mm". The agent receives the current code and edits it rather than starting over.

Select **New** to start a fresh design.

## Parameters

Variables declared at the top of the code in OpenSCAD Customizer format appear in the **Parameters** tab:

```openscad
/* [Size] */
// Inside width
width = 60; // [20:1:200]
// Lid style
style = "snap"; // [snap:Snap fit, slide:Sliding]
```

Dragging a slider re-renders the part on the server. This uses no AI and no tokens.

- **Reset** returns every parameter to the value in the code.
- **Apply to code** writes the current values into the code as the new defaults. Save a version to keep them.

The values you set are saved with the design version, so they are still there when you leave Studio and come back, or open the design on another device. Each version keeps its own values.

Exports use the parameter values you have set, including downloads from the Library. A `.scad` download is the code as written; use **Apply to code** first to make your values its defaults.

When you ask the AI for a change, your values go with the request as the code's defaults, so the new version keeps them.

## Editing code

The **Code** tab is a full editor.

- **Render** (Ctrl+Enter or Cmd+Enter) renders your edits without saving.
- **Save version** (Ctrl+S or Cmd+S) validates and saves them as a new version.

If you send a chat request while you have unsaved edits, the agent starts from your edited code.

Libraries in the container can be included directly, for example `include <BOSL2/std.scad>`. To add your own, copy them into the `libraries` folder inside your data folder; they are picked up on the next render.

## Checks

The **Checks** tab shows the size, volume, an estimated PLA weight, whether the mesh is watertight, the number of separate bodies, and any problems found. When the agent used image previews, the last preview it saw is shown here too.

## History

Every generation and every saved edit is a version. Each version shows what made it: the provider and model for AI generations, **Manual edit** for code you saved yourself, or the MCP client and model for designs saved from an external app. Open one from the **History** tab to load its code and model. Saving after that creates a new version; nothing is overwritten.

## Hardware

The **Hardware** menu under the prompt decides how a request handles screws, inserts, magnets and other hardware: ask me, use parts I have, buy parts as needed, or no hardware. The **Parts** tab next to the viewer lists the parts the design uses, what you have, and where to buy the rest. See [Parts inventory](parts.md).

## Tags

Tags sit under the design title. Type a tag and press Enter (or a comma) to add it, select the x on a tag to remove it, or press Backspace in the empty box to remove the last one. When the agent saves a design that has no tags yet, it suggests a few; after that, it leaves your tags alone.

## Library and search

The Library search box matches words anywhere in a design: its title, version descriptions, tags, the prompts that produced it, the AI provider and model, and the printer. Words match from their start, so `brack` finds "bracket". Put a phrase in double quotes to match it exactly. With search text, the best matches come first.

Each card shows the provider and model behind the current version. Hover it for the request's tokens and cost; a **+N** badge means earlier versions used other models. Cards also show when the design was last changed, up to three tags (hover **+N** for the rest), its size and any hardware. The **⋮** menu on a card downloads the current version as STL, 3MF or SCAD, or deletes the design.

The filters under the search box narrow results by source, model, tag and size, and the sort menu changes the order. Active filters appear as chips you can remove. Select a tag on a card to filter by it.

You can also type filters straight into the search box:

| Filter | Finds designs |
| --- | --- |
| `tag:garage` | Tagged `garage`. Repeat for several tags; quote tags with spaces: `tag:"wall mount"` |
| `model:qwen` | Generated by a model whose name contains `qwen` |
| `provider:ollama` | Generated through that provider |
| `source:mcp` | Created in `studio`, through `mcp`, or `manual`ly from code |
| `printer:x1c` | Whose current version was checked against that printer profile |
| `size:<100` | Whose largest side is under 100 mm. Also `>`, `>=`, `<=`, `=` and ranges like `size:20..80`; a bare number means "at most" |
| `w:<50` `d:>20` `h:20..40` | By width (X), depth (Y) or height (Z) in mm |
| `after:2026-10-01` `before:2026-11` | Created on or after, or before, a day or the first of a month |

For example, `hook tag:garage model:qwen size:<120` finds garage-tagged hooks under 120 mm that a Qwen model generated.

## Choosing the printer

The menu at the top right of the viewer selects the target printer. Switching redraws the build plate at that printer's size, re-runs the checks on the current part, and makes the agent design to that printer's build volume, nozzle and clearance from the next request on. Choose **Add printers...** to pick more printers from the catalog without leaving Studio, or **Manage printers...** to edit profiles.

Each version records the printer it was checked against, shown in the **History** and **Checks** tabs.

## Viewer controls

- Drag to orbit, right-drag or Shift+drag to pan, scroll to zoom.
- **Iso**, **Front**, **Right** and **Top** frame the part from that side.
- **Wireframe** shows the mesh triangles.

The grid matches your printer profile, with fine lines every 10 mm and bold lines every 50 mm. Delta printers get a round plate. The red and green lines mark the X and Y directions at the center of the plate. Parts are shown centered on the plate and resting on its surface, the way a slicer places them, so a design does not need `center=true` to preview well. Downloads keep the design's own OpenSCAD coordinates.

## Exporting

The buttons at the bottom of the inspector download:

- **STL** for any slicer.
- **3MF** for slicers that prefer it, such as Bambu Studio, OrcaSlicer and PrusaSlicer. Each separate body in the design becomes its own object (`part-1`, `part-2`, and so on, largest first), so a mount and its name plate import as two objects you can arrange or color independently. An STL is always a single mesh; use 3MF when a design has more than one part.

  Bambu Studio and OrcaSlicer show "The 3mf file has invalid config, load geometry data only" for any 3MF that was not saved by Bambu Studio. That is expected: the file carries geometry only, and the slicer applies your current printer, filament and process presets.
- **SCAD** to keep editing in OpenSCAD itself.
