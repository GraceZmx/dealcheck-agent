# DealCheck Agent

帮中小创作者评估品牌合作（Brand Deal）的 AI 收入决策助手：用规则判断报价和合同条款，用 Agent 调查品牌背景，最终给出"接受 / 谈判 / 拒绝"建议和一封谈判邮件草稿。

产品需求见 [docs/PRD.md](docs/PRD.md)。

> 开发中：当前处于 M1（规则引擎）阶段。

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

# 5. 启动后端服务，然后访问 http://localhost:8000/health
uvicorn app.main:app --app-dir backend --reload
```
