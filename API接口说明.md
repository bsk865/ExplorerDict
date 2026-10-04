# API 接口说明（DeepSeek 兼容 chat/completions）

> 文档日期：**2026-10-01**（阅读悬浮词典 UI 改造轮）
> 适用代码：`app/api_client.py`（客户端）、`app/explain_service.py`（解释）、
> `app/chat_service.py`（追问）、`app/config.py`（配置项）
> 本文只描述**代码里真实发生**的请求与解析，不描述计划中的接口。

---

## 1. 一句话总结

程序**只用一套 HTTP 接口**：`POST {base_url}/chat/completions`（OpenAI / DeepSeek 兼容）。
两种用途共用它：

| 用途 | 触发方式 | 请求特征 | 响应解析 |
| --- | --- | --- | --- |
| **解释词条** | 点「解释并记录」/「重新解释」/ 主界面「解释」 | `response_format={"type":"json_object"}`，要求模型输出 JSON | 解析 `one_line` / `detail` / `examples` |
| **按词追问** | 详情页点「发送」/「重试」 | 无 `response_format`，普通对话 | 纯文本（只去掉可能的代码围栏） |

**没有 Key 时两者都不会发出任何请求**：`ChatService.submit()` 直接返回 `None`，
`ExplainService` 在构造请求前就返回「未配置」，调用方只显示提示。

---

## 2. 配置项（设置 → 解释 API）

存在 `settings` 表（`app/config.py::DEFAULTS`），界面上可改：

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `api.base_url` | `https://api.deepseek.com/v1` | 兼容端点基址；以 `/` 结尾会被去掉 |
| `api.model` | `deepseek-chat` | 模型名 |
| `api.timeout` | `25` | 秒；下限 3 秒 |
| `chat.enabled` | `1` | 追问总开关 |
| `chat.history_turns` | `6` | 追问只带最近 N 轮（1 轮 = 用户 + 助手），上限 50 |
| `chat.max_history_chars` | `4000` | 历史总字符上限（200 ~ 20000） |
| `chat.no_key_hint` | 中文提示 | 无 Key 时给用户看的文案 |
| `capture.context_chars` | `120` | 解释 / 追问里上下文的截断长度（发送时再 ×2 字符） |
| `api.key_saved` | `0/1` | **只**是「是否已保存 Key」的标志位 |
| （`secrets` 表） | — | API Key 的 **DPAPI 密文**；明文**不允许**写进 `settings` |

**接口地址拼接**（`endpoint_for`）：
`base_url` 去掉尾部 `/`；若本身已以 `/chat/completions` 结尾就直接用，
否则追加 `/chat/completions`。地址不是 `http://` / `https://` 开头 → 配置错误，不发请求。

**鉴权**：请求头 `Authorization: Bearer <API Key>`；Key 只出现在请求头里，
绝不写日志、绝不进异常消息（错误文本统一走 `redact`，并按本次请求快照里的 Key **按值**替换）。

---

## 3. 解释接口（结构化 JSON）

### 3.1 请求

```http
POST {base_url}/chat/completions
Content-Type: application/json
Accept: application/json
Authorization: Bearer <API Key>
```

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "<固定的解释系统提示词>"},
    {"role": "user", "content": "术语：<词>\n\n上下文（用户正在阅读的原文片段，仅用于消歧，可能被截断）：\n<上下文，按 capture.context_chars × 2 字符截断>"}
  ],
  "temperature": 0.2,
  "stream": false,
  "response_format": {"type": "json_object"}
}
```

系统提示词要求模型**只输出一个 JSON 对象**（不要 Markdown 围栏）：

```json
{"one_line": "一句话解释（≤60 字）", "detail": "详细解释", "examples": ["例句或用法示例"]}
```

并要求：只依据术语与给定上下文作答；上下文不足时必须写明「仅凭该语境无法确定」并把
最可能的解释标注为「推测」；`examples` 最多 3 条；不编造出处 / 页码 / 引文。

### 3.2 响应解析（`parse_explanation`）

1. 取 `choices[0].message.content`（字符串）；
2. 去掉可能的 ``` 代码围栏后 `json.loads`；
3. 校验：必须是对象；`one_line` 必须是非空字符串；`detail` 允许空串；
   `examples` 必须是字符串数组（最多取 3 条）。
4. 任一环节不合法 → `ApiError(kind="bad_response")`，**不写库**，词条标记为解释失败。

---

## 4. 追问接口（纯文本）

### 4.1 请求

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "<追问系统提示词>"},
    {"role": "user", "content": "【词语】<词>\n\n【阅读上下文（可能被截断）】\n<上下文>\n\n【已有解释（可能为空）】\n<一句话 + 详细解释>"},
    {"role": "user", "content": "<该词更早的提问>"},
    {"role": "assistant", "content": "<该词更早的回答>"},
    {"role": "user", "content": "<本次问题>"}
  ],
  "temperature": 0.3,
  "stream": false
}
```

消息组装规则（`app/api_client.py::build_chat_messages`，隐私边界就在这里）：

* 只包含：**当前词语 + 短上下文 + 已有解释 + 该词最近有限轮次 + 本次问题**；
* **不包含**：窗口标题、文件路径、URL、整篇文档、**其它词条**、其它词的对话；
* 历史只取该 `entry_id` 自己的记录，`status != ok` 的助手轮次（失败提示文字）**不发送**；
* 轮数上限 `chat.history_turns`，总字符上限 `chat.max_history_chars`（从最新往回装）；
* 本次问题在 `submit` 时已先落库，组装时会把结尾重复的同一问去掉，避免重复发送。

### 4.2 响应解析

追问回答按**纯文本**处理（`parse_chat_reply`）：只去掉可能的代码围栏与首尾空白；
空内容 → `ApiError("bad_response")`。

---

## 5. 错误分类（`ApiError.kind` → 用户可见文案）

| kind | 触发条件 | 显示 |
| --- | --- | --- |
| `config` | 没 Key / 没模型名 / Base URL 为空或不是 http(s) | 配置缺失 |
| `network` | `URLError`（非超时）/ `OSError` | 网络不可达 |
| `timeout` | `socket.timeout` / `TimeoutError` | 请求超时 |
| `auth` | HTTP 401 / 403 | 鉴权失败（提示去设置检查 Key） |
| `rate_limit` | HTTP 429 | 请求过于频繁（限流） |
| `bad_request` | HTTP 400 | 请求被拒绝 |
| `server` | HTTP ≥ 500，或响应体里带 `error` 且没有 `choices` | 服务端错误 |
| `bad_response` | 响应不是合法 JSON / 缺 `choices` / `content` 不是字符串 / 模型 JSON 不合法 / 空回答 | 响应非法 |
| `unknown` | 其它 HTTP 状态 | 未知错误 |

错误文本一律经过 `redact()`：`sk-` 样式 token、`Authorization` 头、以及
**本次请求快照里的 Key 值**都会被替换成 `***`。

---

## 6. 归属、重试与终态（UI 契约）

* **解释**：结果按 `entry_id` + **解释请求 token** 双重归属更新详情页；
  卡片 / 主界面仍只按 `entry_id` 刷新；迟到的结果**不会**重新展开已折叠 / 隐藏的面板。
* **追问**（`ChatService`）：
  * 同一个词条在途时重复点「发送」不会再发一次请求（`_inflight` 闸门，UI 先返回 `busy`）；
  * 每个词条**独立历史**（`chat_turns.entry_id`）；切词时 UI 会清掉上一个词的
    对话缓存 / 请求 token / 忙标记 / **输入草稿**，并同步该词的真实在途状态；
  * 用户那一问先落库；回答回来时词条已删 → 结果**丢弃**（`discarded`，不写库不显示）；
    写库失败（词条仍在但数据库关闭 / 磁盘错误）→ **`error` 终态**并允许重试，
    **绝不**把没落库的回答报成 `ok`；
  * 「重试」只重发**最后一次用户提问**（`record_user=False`），不重复记录那一问。

---

## 7. 隐私红线（代码强制，勿放宽）

1. **只有用户显式动作**才会联网：点「解释并记录」/「重新解释」/「发送」/「重试」。
   划选、自动展开、点标签、折叠、门控轮询**都不会**发请求。
2. 请求体**只有**术语 + 短上下文（+ 解释场景的已有解释 / 已有轮次 / 本次问题），
   绝不发送窗口标题、文件路径、其它词条或整篇文档。
3. **无 Key 不联网**：连客户端都不会构造（测试用「客户端工厂被调用即失败」断言）。
4. Key 只存 DPAPI 密文；`Config.set()` 直接拒绝写 `api.key` / `api_key` 之类的明文键。
5. 不读环境变量、不读全局配置里的 Key。

---

## 8. 本地自测（不碰真实 API）

```bat
:: 1) 起一个本地假服务（127.0.0.1，返回兼容 JSON）
python tools\fake_api_server.py 8801

:: 2) 在“设置”里把 Base URL 改成 http://127.0.0.1:8801/v1、模型名随便填、Key 填 test
::    然后点「测试连接」/「解释并记录」/ 详情页「发送」观察链路
```

自动化测试用**假客户端**（`tests/test_chat.py::FakeChatClient`、
`tests/test_unified_action.py::FakeClient`）直接替换 `make_client`，
**绝不联网**；`tests/test_api.py` 用本地 `127.0.0.1` 上的假服务覆盖成功与失败路径。

---

## 9. 未验收项（诚实声明）

* **真实 DeepSeek / 第三方端点未测试**：本机未配置 Key，真实网络、真实计费额度、
  真实模型输出格式、真实限流与错误码**均未验证**。
* **不同服务商的兼容差异未验证**：`response_format={"type":"json_object"}` 等字段
  在部分兼容端点上可能被忽略或拒绝；程序对「模型不返回合法 JSON」有兜底
  （标记失败 + 可重试），但**没有**逐家端点实测。
* **追问的模型质量未评估**：只保证「发什么 / 收什么 / 怎么归属」，不评价回答质量。

---

## 10. 本轮相关补充（界面收尾轮，2026-10-01）：接口契约没有变化

本轮改的是界面 / 主题归属 / 窗口状态（含收尾续作的按钮居中 / 排队结果），
**请求与响应格式、错误分类、隐私红线全部不变**；
与本节接口契约直接相关的只有三点澄清，均**不新增任何请求**：

1. **主题名优化复用同一条解释请求**：`ExplainResult.topic` 仍然来自解释响应里可选的
   `topic` 字段（`api_client.parse_topic` → `sanitize_topic`）。它在结果写回时只用来给
   **该 entry 自己的主题**改一个更好的名字（`db.rename_batch_auto`，只改
   `name_source='auto'` 的行），**不会**为了起名再发一次请求，也**绝不覆盖**用户改名。
2. **归属只读化不影响接口**：`CaptureService.current_page_scope()` 是纯本地只读查找
   （划选 / 翻页 / 轮询都不建主题、不写 settings、不联网）；真正的写库仍然只发生在
   用户点「解释并记录」之后，与第 6 节的归属契约一致。
3. **写回与改名同事务（收尾续作核对）**：`Database.apply_explanation` 在同一个锁 / 同一次
   `commit` 里完成「按发起请求时的快照校验 `term/context/batch_id` + 写回 + 可选改名」；
   服务写完库、UI 还没消费结果时用户再编辑 / 移动，UI 消费时也**不会**二次改名
   （`tests.test_reading_panel::TestQueuedResultAfterUserEditOrMove`，3 项）。

* **本轮验证范围**：只跑了离线 mock（temp SQLite / 假 Tk / 假 Win32 / 假 UIA / 假客户端 /
  本地假服务），命令与真实输出见 `artifacts/design_checks.txt`。
  **真实 DeepSeek / 第三方端点从未取得过真实响应**（第 9 节的结论不变）：本机没有配置
  真实 Key，没有读取任何真实密钥、没有任何真实计费调用。
* **补充披露（收尾续作，不得省略）**：新增界面用例
  `test_reading_panel::TestShortUserFacingStatus.test_short_status_for_explaining_and_answering`
  的**首次运行**漏打桩 chat，曾用占位 Key `sk-test-key-not-real`（**虚构值，不是真实 Key**）
  与 `https://api.example.com/v1` **尝试发起过一次外部请求**；**没有成功响应的证据**，
  该次尝试**是否真的出网、对端如何回应均未经核实**。随后该用例已改为 `submit=` 纯内存
  替身；只有最后一轮白名单 9 模块 323 项可以限定为「全程 mock、零外部调用」。
  完整表述见 `artifacts/design_checks.txt` 第 0 节与 `验收报告.md` 第 7 节。
