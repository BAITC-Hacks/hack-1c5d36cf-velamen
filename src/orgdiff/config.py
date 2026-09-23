import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr


def project_root() -> Path:
    for path in (Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents):
        if (path / "pyproject.toml").exists():
            return path
    return Path.cwd()


class Settings(BaseModel):
    openai_api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-6-luna"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_reasoning_effort: str = "none"
    llm_max_output_tokens: int = Field(default=8192, gt=0)
    llm_concurrency: int = Field(default=4, ge=1, le=32)
    llm_timeout_seconds: float = Field(default=120, gt=0)
    embedding_batch_size: int = Field(default=64, ge=1, le=64)
    artifact_dir: Path = Path(".artifacts")
    body_chars: int = Field(default=8000, ge=1000)
    context_chars: int = Field(default=2000, ge=100)
    evidence_chars: int = Field(default=16000, ge=1000)

    def require_key(self) -> None:
        if not self.openai_api_key.get_secret_value().strip():
            raise ValueError("Не задан OPENAI_API_KEY. Заполните .env рядом с pyproject.toml или переменную окружения.")


def load_settings() -> Settings:
    root = project_root()
    load_dotenv(root / ".env", override=False)
    values = {name: os.environ[name.upper()] for name in Settings.model_fields if name.upper() in os.environ}
    settings = Settings(**values)
    if not settings.artifact_dir.is_absolute():
        settings.artifact_dir = root / settings.artifact_dir
    return settings
