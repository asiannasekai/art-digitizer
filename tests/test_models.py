import json

from art_digitizer.models import ImageItem, PipelineConfig, Project, config_from_preset, preset_payload


def test_preset_and_project_round_trip():
    config = PipelineConfig(threshold=142, random_seed=12345)
    assert config_from_preset(preset_payload(config, "marker_scan")) == config
    project = Project(images=[ImageItem("drawing.png", 0, override={"threshold": 132})])
    restored = Project.from_dict(json.loads(json.dumps(project.to_dict())))
    assert restored.images[0].override["threshold"] == 132
    assert restored.global_config == PipelineConfig()
