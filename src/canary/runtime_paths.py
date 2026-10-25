"""Installed resources are read-only; all service state lives in one data root."""
from pathlib import Path
import os


def data_root() -> Path:
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return Path(os.environ.get('CANARY_DATA_DIR') or base / 'canary').expanduser().resolve()


def resource_root() -> Path:
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / 'pyproject.toml').is_file() and (checkout / 'scripts').is_dir():
        return checkout
    return Path(__file__).resolve().parent / '_resources'
