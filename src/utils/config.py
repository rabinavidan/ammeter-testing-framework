import os
from typing import Dict

import yaml


def load_config(config_path: str) -> Dict:
    """
    Load the configuration file.

    Raises FileNotFoundError if the file doesn't exist, and ValueError if it isn't valid YAML
    or isn't a mapping at the top level, each naming the file.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in {config_path}: {e}") from e
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} must contain a mapping of settings at the top level, "
                         f"got {type(config).__name__}")
    return config
