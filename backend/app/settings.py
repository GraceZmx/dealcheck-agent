"""环境变量设置：从系统环境变量和项目根目录的 .env 文件读取。

安全约定：API Key 只在内存中使用，不打印、不写日志、不出现在异常信息里。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
PLACEHOLDER_KEYS = {"", "your-key-here"}


def read_env_file(path: Path) -> dict[str, str]:
    """解析简单的 KEY=VALUE 文件；忽略空行和 # 注释。文件不存在时返回空字典。"""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "deepseek"
    llm_model: str | None = None
    search_provider: str = "deepseek"
    deepseek_api_key: str | None = field(default=None, repr=False)  # repr=False：打印 Settings 时不显示 Key
    agent_enabled: bool = True  # 总开关：AGENT_ENABLED=false 时全站只用规则引擎
    debug_ip: bool = False  # 部署诊断：DEBUG_IP=true 时开放 /api/debug/ip
    save_web_traces: bool = False  # 本地排查：SAVE_WEB_TRACES=true 时网页评估也把运行记录存进 traces/；线上不设置

    @property
    def has_deepseek_key(self) -> bool:
        return self.deepseek_api_key not in PLACEHOLDER_KEYS and self.deepseek_api_key is not None


def load_settings(env_file: Path | None = None) -> Settings:
    """系统环境变量优先，其次是 .env 文件。"""
    file_values = read_env_file(env_file or ENV_FILE)

    def get(name: str) -> str | None:
        return os.environ.get(name) or file_values.get(name) or None

    return Settings(
        llm_provider=(get("LLM_PROVIDER") or "deepseek").lower(),
        llm_model=get("LLM_MODEL"),
        search_provider=(get("SEARCH_PROVIDER") or "deepseek").lower(),
        deepseek_api_key=get("DEEPSEEK_API_KEY"),
        agent_enabled=(get("AGENT_ENABLED") or "true").lower() not in ("false", "0", "no", "off"),
        debug_ip=(get("DEBUG_IP") or "false").lower() in ("true", "1", "yes", "on"),
        save_web_traces=(get("SAVE_WEB_TRACES") or "false").lower() in ("true", "1", "yes", "on"),
    )
