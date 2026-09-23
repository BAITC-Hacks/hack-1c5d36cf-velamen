import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(jsonable(parts), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: BaseModel | dict | list) -> None:
    write_text(path, json.dumps(jsonable(value), ensure_ascii=False, indent=2))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
