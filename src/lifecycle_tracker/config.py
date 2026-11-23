"""Settings.

No list-typed fields. Pydantic Settings parses those as JSON, so `LT_REGIONS=us,jp` would
produce a decode error that reads like a bug in the tool rather than a mistake in the
environment.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LT_", env_file=".env", extra="ignore")

    #: A feed file, or a directory of them.
    feeds: Path = Path("data/feeds")

    output_dir: Path = Path("output")

    #: Width of the text gantt, in characters.
    width: int = 64

    log_level: str = "INFO"
