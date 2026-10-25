"""DealCheck Agent 后端入口。"""

from fastapi import FastAPI

app = FastAPI(
    title="DealCheck Agent",
    version="0.1.0",
    description="帮创作者评估品牌合作的 AI 助手（M1：规则引擎）",
)


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查：服务正常运行时返回 ok。"""
    return {"status": "ok"}
