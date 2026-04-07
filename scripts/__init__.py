"""Make pipeline scripts importable as modules."""

import importlib.util
from pathlib import Path

_dir = Path(__file__).parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _dir / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


download_module = _load("download_module", "01_download.py")
categorize_module = _load("categorize_module", "02_categorize.py")
postman_module = _load("postman_module", "03_postman.py")
