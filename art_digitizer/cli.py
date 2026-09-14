"""Command-line interface for reproducible batch processing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import ImageItem, PipelineConfig, Project, config_from_preset, preset_payload
from .processor import discover_images, process_batch


def _load_preset(path: str | None) -> tuple[PipelineConfig, str | None]:
    if not path:
        return PipelineConfig(), None
    preset_path = Path(path)
    data = json.loads(preset_path.read_text(encoding="utf-8"))
    return config_from_preset(data), data.get("preset_name") or preset_path.stem


def _images_from_args(inputs: list[str]) -> list[Path]:
    paths = []
    for value in inputs:
        paths.extend(discover_images(value))
    unique = list(dict.fromkeys(path.resolve() for path in paths))
    if not unique:
        raise SystemExit("No supported images found. Supported formats: PNG, JPG, JPEG, TIFF")
    return unique


def batch_command(args: argparse.Namespace) -> int:
    config, preset_name = _load_preset(args.preset)
    images = _images_from_args(args.input)
    project = Project(
        name=Path(args.output).name,
        global_config=config,
        preset_name=preset_name,
        processing_mode=args.mode,
        images=[ImageItem(source=str(path), order=index) for index, path in enumerate(images)],
    )

    def report(item, result):
        suffix = f": {item.error}" if item.error else ""
        print(f"{item.status:10} {Path(item.source).name}{suffix}")

    manifest = process_batch(project, args.output, force=args.force, on_update=report)
    print(f"\nProcessed {manifest['summary']['completed']} of {manifest['summary']['total']} images")
    if manifest["summary"]["failed"]:
        print(f"Failures: {manifest['summary']['failed']} (see {Path(args.output) / 'processing_manifest.json'})")
    return 1 if manifest["summary"]["failed"] else 0


def preset_command(args: argparse.Namespace) -> int:
    payload = preset_payload(PipelineConfig(), args.name)
    Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote preset to {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="art-digitizer", description="Deterministic batch processing for artwork")
    subparsers = parser.add_subparsers(dest="command", required=True)
    batch = subparsers.add_parser("batch", help="process a folder or explicit image list")
    batch.add_argument("--input", "-i", nargs="+", required=True, help="folders and/or image files")
    batch.add_argument("--preset", "-p", help="JSON pipeline preset")
    batch.add_argument("--output", "-o", required=True, help="output folder")
    batch.add_argument("--mode", choices=["global_fixed", "per_image_automatic"], default="global_fixed")
    batch.add_argument("--force", action="store_true", help="ignore the stage cache and reprocess")
    batch.set_defaults(function=batch_command)
    preset = subparsers.add_parser("preset", help="write a starter preset")
    preset.add_argument("--name", default="new_pipeline")
    preset.add_argument("--output", required=True)
    preset.set_defaults(function=preset_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.function(args)
