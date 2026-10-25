"""运行记录（trace）：把一次完整评估的结果和 Agent 的每一步存成 JSON，方便事后核对来源。

终端（scripts/evaluate.py --agent）和网页（SAVE_WEB_TRACES=true 时）共用这里，格式一致。
traces/ 在 .gitignore 里，不上传 GitHub（可能含真实品牌信息）。
"""

import json
import re
from datetime import datetime
from pathlib import Path

from app.agent.email import DEFAULT_SIGNATURE
from app.pipeline import FullEvaluationResult

TRACES_DIR = Path(__file__).resolve().parents[2] / "traces"


def save_trace(
    result: FullEvaluationResult,
    trace: list,
    brand_name: str,
    tag: str | None = None,
    creator_name: str | None = None,
    directory: Path | None = None,
) -> Path:
    """写入 traces/<时间>-[tag-]<品牌>.json，返回文件路径。

    隐私：创作者姓名只会出现在邮件署名里（发给模型的内容不含姓名），这里把它换回占位符再存。
    """
    if creator_name and creator_name.strip():
        name = creator_name.strip()
        email = result.email.model_copy(update={
            "subject": result.email.subject.replace(name, DEFAULT_SIGNATURE),
            "body": result.email.body.replace(name, DEFAULT_SIGNATURE),
        })
        result = result.model_copy(update={"email": email})

    directory = directory or TRACES_DIR
    directory.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", brand_name.lower()).strip("-") or "brand"
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-" + (f"{tag}-" if tag else "") + slug
    path, n = directory / f"{stem}.json", 2
    while path.exists():  # 同一秒内评估同一品牌时不覆盖
        path, n = directory / f"{stem}-{n}.json", n + 1
    path.write_text(json.dumps({"result": result.model_dump(mode="json"), "trace": trace},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path
