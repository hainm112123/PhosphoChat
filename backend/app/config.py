"""Settings from the repo-root .env.

The .env is also loaded into os.environ so provider SDKs (GROQ_API_KEY, ANTHROPIC_API_KEY,
GOOGLE_API_KEY, OPENAI_API_KEY / OPENAI_BASE_URL) find their keys without extra wiring.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

load_dotenv(ENV_FILE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    model: str = "groq:openai/gpt-oss-120b"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    vaults_dir: Path = Path("vaults")
    default_vault: str = "default"

    tool_output_chars: int = 8000
    chat_history_messages: int = 20
    chat_request_limit: int = 15
    job_request_limit: int = 40

    @property
    def vaults_path(self) -> Path:
        p = self.vaults_dir
        return p if p.is_absolute() else REPO_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
