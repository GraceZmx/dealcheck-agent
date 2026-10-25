# DeepSeek API 实验记录（M2-1）

实验日期：2026-10-25　模型：`deepseek-flash`（DeepSeek-V4.1-Flash-1017）　实验花费：< ¥0.01

## 1. 工具调用（Function Calling）

- 请求和响应都是 OpenAI 兼容格式：`tools: [{"type": "function", "function": {...}}]`，响应里 `message.tool_calls[].function.arguments` 是 JSON 字符串，`finish_reason = "tool_calls"`。
- 工具结果用 `{"role": "tool", "tool_call_id": ..., "content": ...}` 传回。
- 测试问题下，模型第一步都能正确调用 `web_search`，查询词合理。

## 2. 思考模式（Thinking Mode）

| 配置 | 第 1 步（决定调工具） | 第 2 步（给出答案） | 回答质量 |
|---|---|---|---|
| 默认开启（effort=high） | 0.8 秒，输出 55 token，思考 0 | 2.1 秒，输出 207 token，思考 43 | 合理，但**用 Markdown 代码块包住了 JSON** |
| 开启，effort=low | 0.8 秒，输出 63 token，思考 15 | 1.7 秒，输出 117 token，思考 13 | 合理 |
| **关闭**（`{"thinking": {"type": "disabled"}}`） | 0.9 秒，输出 47 token | 1.8 秒，输出 120 token | 合理，纯 JSON |

- 开关参数：`{"thinking": {"type": "enabled" | "disabled"}}`；强度：`reasoning_effort: low | high | max`。默认开启、强度 high。
- 思考 token 计入 `completion_tokens`（`completion_tokens_details.reasoning_tokens` 是其中的思考部分），按输出价格计费；`max_tokens` 也包含思考部分。
- 官方文档要求：请求带 `tools` 时，之前每一轮的 `reasoning_content` 都必须原样传回，否则返回 400。实测不传也没有报错，但仍按文档执行（适配器保留并传回 `reasoning_content`）。

**结论：开发阶段默认关闭思考模式**（`agent.yaml` 中可切换）。理由：
1. 这个任务里需要"判断"的部分（严重负面、品类、调分）最终都由规则和护栏把关，Agent 主要负责搜索、阅读和按格式汇报；小实验中关闭思考的回答质量和开启时没有可见差别；
2. 更快、更便宜，也没有"思考把 `max_tokens` 用完、没有输出答案"的风险；
3. 不用处理 `reasoning_content` 回传，出错面更小。

小实验样本太少，不能证明两种模式质量相同。**M4 在评测集上对比"关闭 / 开启 low / 开启 high"三种配置**再定线上默认值。

## 3. 结构化输出

- `response_format: {"type": "json_object"}` 可用（关闭思考时测试通过）。
- 即使要求"只输出 JSON"，开启思考时模型仍可能用 ```json 代码块包裹。**最终结论改为让模型调用 `submit_findings` 工具提交**：参数天然是 JSON，再用 Pydantic 校验。

## 4. Search 与 Fetch

- Search：0.4 秒返回 5 条结果。字段：`title`、`url`、`snippet`、`summary`、`content`、`date`、`authority`（0–4）、`rerank_score`（0–3）、`site_name`、`site_icon`。
- `summary` 实际长度 1,700–5,100 字符，**比文档写的 600–800 字符长**。喂给模型前需要截断，以控制 token。
- Fetch：第一条结果用了 **10.3 秒**，返回 602 字符的 Markdown。Fetch 明显比 Search 慢，所以提示词引导模型优先读 `summary`，只在必要时读全文。
- 响应头里没有余额或用量信息，成本需要自己按用量计算。
