import os
import tomllib
from pathlib import Path
from typing import Any

from taskloom.contracts.models import ClientConfig, Contract, PlatformConfig, PoolConfig


def load_config[T: Contract](
    model: type[T], path: Path | None, overrides: dict[str, Any] | None = None
) -> T:
    values: dict[str, Any] = {}
    if path is not None:
        values.update(tomllib.loads(path.read_text(encoding="utf-8-sig")))
    environment = {
        ClientConfig: {"TASKLOOM_PLATFORM_URL": "platform_url"},
        PlatformConfig: {
            "TASKLOOM_POOL_URL": "pool_url",
            "TASKLOOM_POOL_KEY": "pool_key",
            "TASKLOOM_POOL_KIND": "pool_kind",
            "TASKLOOM_MODEL": "model",
        },
        PoolConfig: {"TASKLOOM_POOL_KEY": "key"},
    }
    for env, field in environment.get(model, {}).items():
        if env in os.environ:
            values[field] = os.environ[env]
    values.update({key: value for key, value in (overrides or {}).items() if value is not None})
    return model.model_validate(values)
