# DealCheck Agent

帮中小创作者评估品牌合作（Brand Deal）的 AI 收入决策助手：用规则判断报价和合同条款，用 Agent 调查品牌背景，最终给出"接受 / 谈判 / 拒绝"建议和一封谈判邮件草稿。

产品需求见 [docs/PRD.md](docs/PRD.md)。

> 开发中：M1（规则引擎）已完成，下一步是 M2（Agent 核心）。

## 本地运行

需要 Python 3.11 及以上版本。

```bash
# 1. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements-dev.txt

# 3. 配置环境变量（M1 不需要；M2 起需要填入 DeepSeek API Key）
cp .env.example .env

# 4. 运行测试
pytest

# 5. 启动后端服务，然后访问 http://localhost:8000/docs
uvicorn app.main:app --app-dir backend --reload

# 6. 用命令行评估一个样例案例（仅规则引擎，不需要 API Key）
python scripts/evaluate.py examples/deals/2_negotiate_maya.json
```

## 目录

| 路径 | 内容 |
|---|---|
| `backend/app/rules/` | 规则引擎：条款检查、报价评估、硬性规则、打分决策 |
| `backend/config/` | 所有阈值和分数（YAML），改数字不用改代码 |
| `backend/data/` | 市场价基准表 V0 及其[来源说明](backend/data/BENCHMARK_SOURCES.md) |
| `backend/tests/` | pytest 单元测试 |
| `examples/deals/` | 5 个样例合作案例 |
| `scripts/` | 基准表生成脚本、命令行评估工具 |
