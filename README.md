# Art Digitizer

A deterministic batch workspace and command-line pipeline for turning artwork images into 2D and simple extruded 3D assets.

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Batch CLI

Folders are searched recursively. Explicit files can be passed together, and all selected assets use the same preset and global settings.

```bash
python3 main.py batch \
	--input ./drawings \
	--preset ./presets/high_detail_art.json \
	--output ./exports
```

Use `--mode per_image_automatic` when you want deterministic automatic sizing. In that mode, each image gets an Otsu-derived threshold and a minimum contour size based on its dimensions; the selected values are recorded in that asset's manifest. The default `global_fixed` mode applies the same values to every image, with optional per-image overrides in a saved project. Use `--force` to ignore the cache and re-run every asset.

Each asset gets a folder named from its original filename containing `cleaned.png`, `mask.png`, `vector.svg`, `model.obj`, `model.stl`, and `settings.json` as enabled by the preset. The output root contains `processing_manifest.json` with source hashes, dimensions, parameters, statuses, warnings, and failures. Failed files are recorded and do not stop the remaining batch.

Create a starter preset with:

```bash
python3 main.py preset --name marker_scan --output presets/marker_scan.json
```

## Batch workspace

Launch the graphical workspace with:

```bash
python3 -m art_digitizer.ui
```

It supports multi-file selection, recursive folder import, native multi-file drag-and-drop when `tkinterdnd2` is available, removal, reordering, selected/all processing, fixed versus automatic mode, reset-to-global overrides, stage selection, project save/load, and reprocessing. The UI calls the same `process_batch` function as the CLI.

Pillow is used for PNG, JPG/JPEG, and TIFF decoding. Transparent PNGs are composited against white before grayscale processing. GLB export is represented in the manifest as a warning until a mesh exporter is added.