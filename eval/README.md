# 评测集格式说明（M4）

> 对应 PRD 第 14 节。标注方法见 `eval/LABELING_GUIDE.md`。完整样例见 `eval/sample_case.json`（只是示意，不计入评测集）。

## 存放位置

| 内容 | 位置 | 进 GitHub |
|---|---|---|
| 格式说明、标注准则、样例 | `eval/` | 进 |
| 虚构品牌案例（5 个） | `eval/cases/` | 进 |
| 真实品牌案例（20 个） | `local/eval/cases/` | **不进**（`local/` 已被 git 忽略） |
| 每次评测的运行结果 | `local/eval/runs/<时间>/` | **不进**（结果里有品牌名） |
| 评测报告 | `docs/notes/` | 进，只写案例编号和汇总数字，不写真实品牌名 |

- 案例编号不能带品牌名：真实品牌用 `R01`–`R20`，虚构品牌用 `F01`–`F05`，文件名就是编号（例如 `R07.json`）。
- `local/eval/` 定期打包成加密压缩包备份到 iCloud（只存在本机，丢了很难重做）。

## 构成

| 类别 `category` | 数量 | 真实 | 虚构 | 说明 |
|---|---|---|---|---|
| `accept` | 5 | 5 | 0 | 虚构品牌查不到信息会触发 HR-7，永远到不了"接受" |
| `negotiate` | 8 | 8 | 0 | 虚构品牌会因 HR-7 "因为错误的理由答对"，一致率虚高 |
| `decline` | 5 | 3 | 2 | 虚构品牌只用于"报价极低 + 条款苛刻、总分低于 50"的情况 |
| `hard_rule` | 4 | 2 | 2 | 真实：HR-2 严重负面、表单品类和实际业务不符；虚构：HR-1（表单勾选拒接品类）、HR-3（要求预付），这两种预检就拒绝，Agent 被跳过 |
| `adversarial` | 3 | 2 | 1 | 真实：同名品牌混淆、负面只有单一来源；虚构：几乎没有公开信息的新品牌 |

- 虚构品牌名要先搜一下，确认现实中没有同名公司；网址一律用 `.example`。
- 真实的"单一来源负面"品牌如果找不到，改放进评测集之外的护栏专项测试（用模拟搜索结果）。
- 之前在本地测过的 5 个真实品牌不进评测集（见标注准则第四节第 3 条）。
- 校准组 `dev` 15 个、测试组 `test` 10 个，每个类别按比例分配。测试组只有 10 个案例，结果是方向性的。

## 字段

一个案例是一个 JSON 文件，分 5 块。`profile` 和 `deal` 与 `examples/deals/` 完全相同，接口会忽略其他字段，所以单个案例也能直接用 `python scripts/evaluate.py <案例> --agent` 运行。

### `case`：案例信息

| 字段 | 说明 |
|---|---|
| `id` | `R01`–`R20` / `F01`–`F05`，不带品牌名 |
| `category` | `accept` / `negotiate` / `decline` / `hard_rule` / `adversarial` |
| `brand_kind` | `real` / `fictional` |
| `description` | 一句话：这个案例在测什么 |
| `eval_date` | 标注日期。真实品牌每季度复核一次 |
| `split` | `dev`（校准组）/ `test`（测试组） |

### `profile` + `deal`：输入

和现有案例格式一致。**不放** `agent_findings`（评测跑真实 Agent），**不放** `creator_name`（隐私，且不影响结果）。

### `expected`：标准答案

| 字段 | 说明 |
|---|---|
| `decision` | `accept` / `negotiate` / `decline`，只填一个 |
| `hard_rules.must_hit` | 必须触发的硬性规则编号 |
| `hard_rules.must_not_hit` | 不该触发的硬性规则编号，用来发现误伤 |
| `brand_entity.should_confirm` | Agent 是否应该确认品牌实体（算"品牌实体识别准确率"） |
| `brand_entity.confusable_with` | 容易混淆的同名对象，用匿名描述（同名品牌案例才填） |
| `key_risks` | 必须识别出的风险点，每条 `{id, desc, check}`，`check` 见下表 |
| `must_not_claim` | 可选：Agent 不该编出来的结论 |

**分数区间不写进文件**，由评测脚本根据 `decision` 自动生成：

| 决策 | 自动生成的分数区间 |
|---|---|
| `accept` | 75–100 |
| `negotiate` | 50–74 |
| `decline` | 0–49 |

`must_hit` 里有任何硬性规则时（封顶类 HR-4~HR-8，或拒绝类 HR-1~HR-3），**不生成分数区间、不检查分数**：这时决策由规则决定，分数可能落在任何区间。

**`key_risks` 的检查方式 `check.type`：**

| 类型 | 写法 | 怎么检查 |
|---|---|---|
| `hard_rule` | `{"type": "hard_rule", "rule": "HR-7"}` | 自动：结果里有这条规则 |
| `term_light` | `{"type": "term_light", "term": "kill_fee", "light": "red"}` | 自动：条款红黄绿灯一致。`term`：`exclusivity` / `usage_rights` / `kill_fee` / `revisions` |
| `pricing_position` | `{"type": "pricing_position", "position": "below_p25"}` | 自动：报价位置一致。`below_p25` / `p25_to_p50` / `p50_to_p75` / `above_p75` |
| `severe_flag` | `{"type": "severe_flag", "flag": "nonpayment"}` | 自动：Agent 报告了这类严重负面。`scam` / `regulatory` / `nonpayment` / `safety` |
| `detected_category` | `{"type": "detected_category", "category": "alcohol"}` | 自动：Agent 识别出这个敏感品类（标准清单名称） |
| `judge` | `{"type": "judge"}` | LLM 评审判断报告或邮件是否覆盖了 `desc`，人工抽检 30% |

### `label`：标注信息（控制个人偏差，见标注准则第四节）

| 字段 | 说明 |
|---|---|
| `labeled_at` | 第一次标注日期 |
| `labeled_before_first_run` | 是否在第一次运行评测之前标完，应为 `true` |
| `confidence` | `high` / `medium` / `low` |
| `boundary` | 两个决策都说得通时为 `true` |
| `rationale` | 2–4 句话，写给创作者朋友的理由 |
| `relabel` | 2–3 天后自己盲标第二遍：`{at, decision, confidence}` |
| `second_opinion` | `deepseek-v4-pro` 盲标：`{by, at, decision, note}`，只作参考 |
| `history` | 每次修改：`{at, field, from, to, reason, after_seeing_output}` |

### `evidence`：判断依据（只有真实品牌）

`[{fact, url, checked_at}]`，3–5 条。虚构品牌没有这一块。
