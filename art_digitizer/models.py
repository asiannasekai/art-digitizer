"""Serializable configuration and project models shared by the UI and CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class PipelineConfig:
    background_removal: bool = True
    grayscale: bool = True
    contrast: float = 1.0
    threshold_type: str = "fixed"
    threshold: int = 142
    adaptive_block_size: int = 31
    adaptive_offset: int = 5
    invert: bool = False
    blur: int = 0
    denoise: int = 2
    morphology: int = 1
    minimum_contour_size: int = 15
    simplification: float = 0.8
    detail_preservation: str = "high"
    stroke_preservation: bool = True
    polygon_repair: bool = True
    extrusion_depth: float = 6.0
    bevel: float = 0.4
    mesh_resolution: int = 1
    scale: float = 1.0
    orientation: str = "front"
    export_png: bool = True
    export_svg: bool = True
    export_obj: bool = True
    export_stl: bool = True
    export_glb: bool = False
    random_seed: int = 12345

    def validate(self) -> None:
        if self.threshold_type not in {"fixed", "adaptive"}:
            raise ValueError("threshold_type must be 'fixed' or 'adaptive'")
        if not 0 <= self.threshold <= 255:
            raise ValueError("threshold must be between 0 and 255")
        if self.adaptive_block_size < 3 or self.adaptive_block_size % 2 == 0:
            raise ValueError("adaptive_block_size must be an odd number >= 3")
        if self.simplification < 0 or self.extrusion_depth < 0 or self.bevel < 0:
            raise ValueError("simplification, extrusion_depth, and bevel cannot be negative")
        if self.minimum_contour_size < 0 or self.denoise < 0 or self.blur < 0:
            raise ValueError("image cleanup values cannot be negative")


@dataclass
class ImageItem:
    source: str
    order: int
    status: str = "Ready"
    override: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    warning: str | None = None
    error: str | None = None
    manual_edits: bool = False

    def effective_config(self, global_config: PipelineConfig) -> PipelineConfig:
        values = asdict(global_config)
        values.update(self.override)
        config = PipelineConfig(**values)
        config.validate()
        return config


@dataclass
class Project:
    name: str = "art-digitizer-project"
    global_config: PipelineConfig = field(default_factory=PipelineConfig)
    preset_name: str | None = None
    images: list[ImageItem] = field(default_factory=list)
    processing_mode: str = "global_fixed"
    output_directory: str = "processed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "global_config": asdict(self.global_config),
            "preset_name": self.preset_name,
            "images": [asdict(image) for image in self.images],
            "processing_mode": self.processing_mode,
            "output_directory": self.output_directory,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        config_values = data.get("global_config", {})
        config_fields = {item.name for item in fields(PipelineConfig)}
        config = PipelineConfig(**{k: v for k, v in config_values.items() if k in config_fields})
        images = [ImageItem(**item) for item in data.get("images", [])]
        project = cls(
            name=data.get("name", cls.name),
            global_config=config,
            preset_name=data.get("preset_name"),
            images=images,
            processing_mode=data.get("processing_mode", "global_fixed"),
            output_directory=data.get("output_directory", "processed"),
        )
        project.global_config.validate()
        return project


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def preset_payload(config: PipelineConfig, name: str | None = None) -> dict[str, Any]:
    config.validate()
    return {
        "preset_name": name,
        "pipeline_version": "1.0",
        "parameters": asdict(config),
    }


def config_from_preset(data: dict[str, Any]) -> PipelineConfig:
    values = data.get("parameters", data)
    config_fields = {item.name for item in fields(PipelineConfig)}
    config = PipelineConfig(**{k: v for k, v in values.items() if k in config_fields})
    config.validate()
    return config


def resolve_output_dir(root: str | Path, source: str) -> Path:
    stem = Path(source).stem
    safe_stem = "".join(char if char.isalnum() or char in "-_" else "_" for char in stem).strip("._")
    return Path(root) / (safe_stem or "asset")
