"""PRISMA 2020 reporting checklist for saved reviews."""
from functools import lru_cache
from importlib.resources import files
import json


@lru_cache(maxsize=1)
def catalogue() -> dict:
    return json.loads(files('canary').joinpath('prisma_2020.json').read_text(encoding='utf-8'))
