# L4D2 Map Editor

Remove invisible walls and edit doors, ladders, blockers, props and triggers on any Left 4 Dead 2 map, official or custom, **without Hammer and without recompiling**. Your changes are saved as a small `.lmp` patch that you ship in an addon; the original map is never touched.

Made by **Rune**. Free and open source: use it, change it, share it. Please credit Rune if you pass it on.

![Isometric view: invisible walls in red, removed ones in green](screenshot-walls.png)

## Download

Get the latest zip from **[Releases](../../releases/latest)**. It contains:

- `L4D2_Map_Editor.py`, the tool
- `L4D2 Map Editor - Guide.pdf`, the full 15-page guide (FAQ and troubleshooting included)

## Requirements

- Windows
- [Python 3.8 or newer](https://www.python.org/downloads/) (tick **Add Python to PATH** when installing). Nothing else to install.

## Run it

Open a command prompt in the folder with the script and run:

```
python L4D2_Map_Editor.py
```

If `python` isn't recognized, try `py L4D2_Map_Editor.py`.

## What it does

- **Opens any map:** official `.bsp` files, or custom/Workshop maps straight from their addon `.vpk`.
- **2D and isometric views:** a colored 3D render of the map, rotate, zoom, and a height slider to cut away roofs.
- **Walls mode:** click invisible walls (player clip) to remove them.
- **Entities mode:** delete, move, copy and edit entities (doors, ladders, blockers, props, triggers, logic), add new invisible walls or props, undo/redo.
- **Saves `.lmp` patches** that keep Valve's own fixes, pick the right file numbers, and also cover Valve's game-mode versions of the patch.
- **Check problems:** scans your addons and patches for about 20 kinds of problems (addon turned off, file never loads, typo in the map name, two patches cancelling each other, broken files...) and fixes most of them automatically, with backups.

![Entities mode](screenshot-entities.png)

## How it works

A `.lmp` file replaces one part ("lump") of a map when it loads. Walls mode writes the brushes lump (18) with the player-clip flag cleared on the walls you pick; Entities mode writes the entity lump (0). Valve fixes its own maps the same way (see `update/maps/` in your game folder). Tested in game: a patch saved by this tool, shipped in an addon, is loaded over Valve's own patch in co-op and versus.

The full technical details are in the guide.

## Command line

```
python L4D2_Map_Editor.py check
python L4D2_Map_Editor.py <map>.bsp write <map>_l_1.lmp all
python L4D2_Map_Editor.py <map>.bsp list X0 Y0 X1 Y1
```

## License

Free to use, change and share. Please credit Rune if you share it.
