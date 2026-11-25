import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chat: str = Field(default="", max_length=256)
    source: Literal["accessibility"] = "accessibility"
    poll_interval: float = Field(default=0.5, ge=0.1, le=30.0)
    data_dir: str = "/data"
    send_mode: Literal["manual", "auto"] = "manual"
    send_key: Literal["Return", "ctrl+Return"] = "Return"
    mcp_enabled: bool = False
    accessibility_profile: str | dict | None = None
    lookup_sender_names: bool = False
    native_image_viewer: bool = False

    @classmethod
    def load(cls) -> "Settings":
        path = Path(os.getenv("BRIDGE_CONFIG", "/app/config.yaml"))
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
