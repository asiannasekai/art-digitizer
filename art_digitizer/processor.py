"""The one processing engine used by both the CLI and the desktop workspace."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from . import PIPELINE_VERSION, __version__
from .models import ImageItem, PipelineConfig, Project, resolve_output_dir, utc_timestamp

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def discover_images(input_path: str | Path) -> list[Path]:
    path = Path(input_path)
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    return [item for item in candidates if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS]


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def config_hash(config: PipelineConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def auto_config_for_image(source: str | Path, base: PipelineConfig) -> PipelineConfig:
    """Choose deterministic per-image values using grayscale Otsu thresholding."""
    Image, _, _, ImageOps = _require_pillow()
    with Image.open(source) as opened:
        grayscale = ImageOps.grayscale(opened.convert("RGB"))
        histogram = grayscale.histogram()
        total = sum(histogram)
        weighted_total = sum(value * count for value, count in enumerate(histogram))
        background_weight = 0
        background_count = 0
        best_threshold = base.threshold
        best_variance = -1.0
        for threshold in range(256):
            background_count += histogram[threshold]
            if not background_count or background_count == total:
                continue
            background_weight += threshold * histogram[threshold]
            foreground_count = total - background_count
            foreground_weight = weighted_total - background_weight
            mean_background = background_weight / background_count
            mean_foreground = foreground_weight / foreground_count
            variance = background_count * foreground_count * (mean_background - mean_foreground) ** 2
            if variance > best_variance:
                best_variance = variance
                best_threshold = threshold
        values = asdict(base)
        values["threshold_type"] = "fixed"
        values["threshold"] = max(1, min(254, best_threshold))
        values["minimum_contour_size"] = max(3, round(grayscale.width * grayscale.height * 0.0002))
        return PipelineConfig(**values)


def _require_pillow():
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except ImportError as error:
        raise RuntimeError("Pillow is required for image processing. Install with: pip install -r requirements.txt") from error
    return Image, ImageEnhance, ImageFilter, ImageOps


def _threshold(image, config: PipelineConfig):
    Image, _, ImageFilter, _ = _require_pillow()
    if config.threshold_type == "fixed":
        return image.point(lambda value: 255 if value >= config.threshold else 0, mode="L")
    radius = config.adaptive_block_size // 2
    blurred = image.filter(ImageFilter.BoxBlur(radius))
    offset = config.adaptive_offset
    source = list(image.getdata())
    local = list(blurred.getdata())
    result = Image.new("L", image.size)
    result.putdata([255 if value >= average - offset else 0 for value, average in zip(source, local)])
    return result


def _components(mask, minimum_size: int) -> list[tuple[int, int, int, int]]:
    width, height = mask.size
    pixels = mask.load()
    visited: set[tuple[int, int]] = set()
    boxes = []
    for y in range(height):
        for x in range(width):
            if (x, y) in visited or pixels[x, y] == 0:
                continue
            stack = [(x, y)]
            visited.add((x, y))
            points = []
            while stack:
                point_x, point_y = stack.pop()
                points.append((point_x, point_y))
                for neighbor in ((point_x - 1, point_y), (point_x + 1, point_y), (point_x, point_y - 1), (point_x, point_y + 1)):
                    nx, ny = neighbor
                    if 0 <= nx < width and 0 <= ny < height and neighbor not in visited and pixels[nx, ny] > 0:
                        visited.add(neighbor)
                        stack.append(neighbor)
            if len(points) >= minimum_size:
                xs = [point[0] for point in points]
                ys = [point[1] for point in points]
                boxes.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1))
    return boxes


def _mask_polygons(mask, minimum_size: int) -> list[list[tuple[int, int]]]:
    """Trace the pixel-mask boundary so geometry follows the drawing silhouette."""
    width, height = mask.size
    pixels = mask.load()
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for y in range(height):
        for x in range(width):
            if pixels[x, y] == 0:
                continue
            neighbors = ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y))
            boundary = (neighbors[0][1] < 0 or pixels[neighbors[0][0], neighbors[0][1]] == 0,
                        neighbors[1][0] >= width or pixels[neighbors[1][0], neighbors[1][1]] == 0,
                        neighbors[2][1] >= height or pixels[neighbors[2][0], neighbors[2][1]] == 0,
                        neighbors[3][0] < 0 or pixels[neighbors[3][0], neighbors[3][1]] == 0)
            cell_edges = [((x, y), (x + 1, y)), ((x + 1, y), (x + 1, y + 1)), ((x + 1, y + 1), (x, y + 1)), ((x, y + 1), (x, y))]
            for is_boundary, edge in zip(boundary, cell_edges):
                if is_boundary:
                    edges.setdefault(edge[0], []).append(edge[1])
    polygons = []
    while edges:
        start = next(iter(edges))
        current = start
        polygon = []
        while True:
            polygon.append(current)
            destinations = edges.get(current)
            if not destinations:
                break
            next_point = destinations.pop()
            if not destinations:
                del edges[current]
            current = next_point
            if current == start:
                break
        if len(polygon) >= 4 and abs(sum(polygon[i][0] * polygon[(i + 1) % len(polygon)][1] - polygon[(i + 1) % len(polygon)][0] * polygon[i][1] for i in range(len(polygon)))) >= minimum_size:
            polygons.append(polygon)
    return polygons


def _write_svg(path: Path, polygons: list[list[tuple[int, int]]], width: int, height: int) -> None:
    elements = []
    for polygon in polygons:
        points = " ".join(f"{x},{y}" for x, y in polygon)
        elements.append(f'<polygon points="{points}" />')
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"><g fill="none" stroke="black">{"".join(elements)}</g></svg>\n',
        encoding="utf-8",
    )


def _background_removed(image, foreground_mask):
    """Keep foreground pixels and make the thresholded background transparent."""
    rgba = image.convert("RGBA")
    rgba.putalpha(foreground_mask)
    return rgba


def _polygon_preview(size: tuple[int, int], polygons: list[list[tuple[int, int]]], mask):
    Image, _, _, _ = _require_pillow()
    from PIL import ImageDraw

    preview = Image.new("RGBA", size, (248, 246, 240, 255))
    draw = ImageDraw.Draw(preview)
    for polygon in polygons:
        draw.polygon(polygon, fill=(30, 92, 82, 255), outline=(15, 54, 48, 255))
    if not polygons:
        preview.paste((30, 92, 82, 255), mask=mask)
    return preview


def _mesh_preview(size: tuple[int, int], polygons: list[list[tuple[int, int]]], depth: float):
    Image, _, _, _ = _require_pillow()
    from PIL import ImageDraw

    preview = Image.new("RGB", size, (235, 238, 234))
    draw = ImageDraw.Draw(preview)
    offset = max(2, min(18, round(depth)))
    for polygon in polygons:
        extruded = [(x + offset, y - offset) for x, y in polygon]
        draw.polygon(extruded, fill=(112, 151, 140))
        draw.polygon(polygon, fill=(35, 111, 96), outline=(10, 45, 39))
        for index, point in enumerate(polygon):
            draw.line([point, extruded[index]], fill=(20, 73, 65), width=1)
    return preview


def _write_stage_folders(target: Path) -> dict[str, list[str]]:
    """Create traceable stage folders without removing the convenient flat exports."""
    stage_files = {
        "01_original": ["original.png"],
        "02_background_removed": ["background_removed.png"],
        "03_threshold": ["threshold.png"],
        "04_cleaned_mask": ["cleaned_mask.png"],
        "05_contours": ["contours.png"],
        "06_polygon": ["polygon.png", "vector.svg"],
        "07_mesh": ["mesh.png", "model.obj", "model.stl"],
        "08_final": ["final_preview.png", "settings.json"],
    }
    manifest_stages = {}
    stages_dir = target / "stages"
    for stage_name, filenames in stage_files.items():
        stage_dir = stages_dir / stage_name
        stage_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        for filename in filenames:
            source = target / filename
            if source.is_file():
                shutil.copy2(source, stage_dir / filename)
                copied.append(filename)
        manifest_stages[stage_name] = copied
    return manifest_stages


def _write_export_archive(target: Path) -> str:
    archive_name = f"{target.name}_exports.zip"
    archive_path = target / archive_name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(target.rglob("*")):
            if path.is_file() and path != archive_path and ".cache_key" not in path.parts:
                archive.write(path, Path(target.name) / path.relative_to(target))
    return archive_name


def _write_meshes(directory: Path, polygons: list[list[tuple[int, int]]], depth: float, write_obj: bool, write_stl: bool) -> list[str]:
    outputs = []
    if not polygons:
        polygons = [[(0, 0), (1, 0), (1, 1), (0, 1)]]
    vertices = []
    faces = []
    for polygon in polygons:
        base = len(vertices) + 1
        vertices.extend([(x, y, 0) for x, y in polygon] + [(x, y, depth) for x, y in polygon])
        count = len(polygon)
        faces.extend([(base + index for index in range(count)), (base + count + index for index in reversed(range(count)))])
        for index in range(count):
            next_index = (index + 1) % count
            faces.append((base + index, base + next_index, base + count + next_index, base + count + index))
    if write_obj:
        obj = "\n".join([*(f"v {x} {y} {z}" for x, y, z in vertices), *("f " + " ".join(map(str, face)) for face in faces)]) + "\n"
        (directory / "model.obj").write_text(obj, encoding="utf-8")
        outputs.append("model.obj")
    if write_stl:
        triangles = []
        for face in faces:
            face_vertices = [vertices[index - 1] for index in face]
            for index in range(1, len(face_vertices) - 1):
                triangles.append((face_vertices[0], face_vertices[index], face_vertices[index + 1]))
        stl = ["solid art_digitizer"]
        for a, b, c in triangles:
            stl.extend([" facet normal 0 0 0", "  outer loop", f"   vertex {a[0]} {a[1]} {a[2]}", f"   vertex {b[0]} {b[1]} {b[2]}", f"   vertex {c[0]} {c[1]} {c[2]}", "  endloop", " endfacet"])
        stl.append("endsolid art_digitizer\n")
        (directory / "model.stl").write_text("\n".join(stl), encoding="utf-8")
        outputs.append("model.stl")
    return outputs


def process_image(item: ImageItem, global_config: PipelineConfig, output_root: str | Path, mode: str = "global_fixed", force: bool = False, on_stage: Callable[[str], None] | None = None, effective_config: PipelineConfig | None = None, automatic_parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    Image, ImageEnhance, ImageFilter, ImageOps = _require_pillow()
    from PIL import ImageDraw
    source = Path(item.source)
    started = time.time()
    config = effective_config or item.effective_config(global_config)
    record: dict[str, Any] = {
        "source_filename": source.name,
        "source_path": str(source.resolve()),
        "source_sha256": source_hash(source),
        "processing_timestamp": utc_timestamp(),
        "application_version": __version__,
        "pipeline_version": PIPELINE_VERSION,
        "preset_name": None,
        "parameters": asdict(config),
        "processing_mode": mode,
        "per_image_overrides": item.override,
        "random_seed": item.effective_config(global_config).random_seed,
        "outputs": [],
        "warnings": [],
        "manual_edits": item.manual_edits,
        "automatic_parameters": automatic_parameters,
    }
    target = resolve_output_dir(output_root, source.name)
    target.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256((record["source_sha256"] + config_hash(config)).encode()).hexdigest()
    cache_file = target / ".cache_key"
    if not force and cache_file.exists() and cache_file.read_text(encoding="utf-8") == cache_key and (target / "settings.json").exists():
        record["stage_directories"] = _write_stage_folders(target)
        archive_name = _write_export_archive(target)
        record["export_directory"] = str(target)
        record["export_archive"] = archive_name
        record["outputs"] = sorted(path.name for path in target.iterdir() if path.is_file() and not path.name.startswith("."))
        if archive_name not in record["outputs"]:
            record["outputs"].append(archive_name)
        (target / "settings.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record["export_archive"] = _write_export_archive(target)
        record["status"] = "Completed (cached)"
        if on_stage:
            on_stage("Completed (cached)")
        return record

    image = Image.open(source)
    record["source_dimensions"] = {"width": image.width, "height": image.height}
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        background.alpha_composite(image.convert("RGBA"))
        image = background.convert("RGB")
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.save(target / "original.png")
    grayscale = ImageOps.grayscale(image) if config.grayscale else image
    if config.contrast != 1.0:
        grayscale = ImageEnhance.Contrast(grayscale).enhance(config.contrast)
    if config.blur:
        grayscale = grayscale.filter(ImageFilter.GaussianBlur(config.blur))
    mask = _threshold(grayscale, config)
    if config.invert:
        foreground_mask = mask
    else:
        foreground_mask = ImageOps.invert(mask)
    if on_stage:
        on_stage("Threshold")
    mask.save(target / "threshold.png")
    if config.denoise:
        foreground_mask = foreground_mask.filter(ImageFilter.MedianFilter(size=min(2 * config.denoise + 1, 99)))
    if config.morphology:
        foreground_mask = foreground_mask.filter(ImageFilter.MaxFilter(size=min(2 * config.morphology + 1, 99)))
        foreground_mask = foreground_mask.filter(ImageFilter.MinFilter(size=min(2 * config.morphology + 1, 99)))
    if on_stage:
        on_stage("Cleaned Mask")
    foreground_mask.save(target / "cleaned_mask.png")
    if on_stage:
        on_stage("Background Removed")
    background_removed = _background_removed(image, foreground_mask)
    background_removed.save(target / "background_removed.png")
    boxes = _components(foreground_mask, config.minimum_contour_size)
    polygons = _mask_polygons(foreground_mask, config.minimum_contour_size)
    if on_stage:
        on_stage("Contours")
    cleaned = Image.new("RGB", image.size, "white")
    cleaned.paste(image, mask=foreground_mask)
    preview_images = {
        "original.png": image,
        "background_removed.png": background_removed,
        "threshold.png": mask,
        "cleaned_mask.png": foreground_mask,
    }
    contours = image.copy()
    contour_draw = ImageDraw.Draw(contours)
    for box in boxes:
        contour_draw.rectangle(box, outline=(255, 0, 0), width=1)
    polygon = _polygon_preview(image.size, polygons, foreground_mask)
    polygon.save(target / "polygon.png")
    preview_images["contours.png"] = contours
    preview_images["polygon.png"] = polygon
    for filename, preview in preview_images.items():
        preview.save(target / filename)
        record["outputs"].append(filename)
    if config.export_png:
        cleaned.save(target / "cleaned.png")
        mask.save(target / "mask.png")
        record["outputs"].extend(["cleaned.png", "mask.png"])
    if config.export_svg:
        _write_svg(target / "vector.svg", polygons, image.width, image.height)
        record["outputs"].append("vector.svg")
    if on_stage:
        on_stage("3D Mesh")
    mesh_preview = _mesh_preview(image.size, polygons, config.extrusion_depth)
    mesh_preview.save(target / "mesh.png")
    record["outputs"].append("mesh.png")
    preview_images["final_preview.png"] = mesh_preview
    mesh_preview.save(target / "final_preview.png")
    record["outputs"].append("final_preview.png")
    record["outputs"].extend(_write_meshes(target, polygons, config.extrusion_depth, config.export_obj, config.export_stl))
    if config.export_glb:
        record["warnings"].append("GLB export is not available in the standard Pillow backend")
    record["stage_previews"] = ["Original", "Background Removed", "Threshold", "Cleaned Mask", "Contours", "Polygon", "3D Mesh", "Final Preview"]
    record["duration_seconds"] = round(time.time() - started, 3)
    if on_stage:
        on_stage("Final Preview")
    (target / "settings.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["stage_directories"] = _write_stage_folders(target)
    record["export_directory"] = str(target)
    record["export_archive"] = _write_export_archive(target)
    if record["export_archive"] not in record["outputs"]:
        record["outputs"].append(record["export_archive"])
    (target / "settings.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    cache_file.write_text(cache_key, encoding="utf-8")
    return record


def process_batch(project: Project, output_root: str | Path, force: bool = False, on_update: Callable[[ImageItem, dict[str, Any]], None] | None = None, on_stage: Callable[[ImageItem, str], None] | None = None) -> dict[str, Any]:
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "manifest_version": "1.0",
        "created_at": utc_timestamp(),
        "application_version": __version__,
        "pipeline_version": PIPELINE_VERSION,
        "preset_name": project.preset_name,
        "processing_mode": project.processing_mode,
        "global_parameters": asdict(project.global_config),
        "assets": [],
    }
    for item in sorted(project.images, key=lambda entry: entry.order):
        item.status, item.error, item.warning = "Processing", None, None
        try:
            if on_stage:
                on_stage(item, "Processing")
            effective_config = item.effective_config(project.global_config)
            if project.processing_mode == "per_image_automatic":
                effective_config = auto_config_for_image(item.source, effective_config)
            result = process_image(item, project.global_config, output_root, project.processing_mode, force, on_stage=lambda stage: on_stage(item, stage) if on_stage else None, effective_config=effective_config, automatic_parameters=asdict(effective_config) if project.processing_mode == "per_image_automatic" else None)
            item.outputs = result.get("outputs", [])
            item.warning = "; ".join(result.get("warnings", [])) or None
            item.status = "Warning" if item.warning else "Completed"
            result["status"] = item.status
        except Exception as error:  # each asset is an independent job
            result = {"source_filename": Path(item.source).name, "status": "Failed", "error": str(error), "warnings": []}
            item.status, item.error = "Failed", str(error)
        manifest["assets"].append(result)
        if on_update:
            on_update(item, result)
    manifest["summary"] = {
        "total": len(manifest["assets"]),
        "completed": sum(asset.get("status", "").startswith("Completed") for asset in manifest["assets"]),
        "warnings": sum(asset.get("status") == "Warning" for asset in manifest["assets"]),
        "failed": sum(asset.get("status") == "Failed" for asset in manifest["assets"]),
    }
    (output_root / "processing_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
