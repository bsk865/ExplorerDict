# 探索词典 — 跨应用阅读划词收藏器

日常使用请双击项目目录里的 **探索词典.exe**，无需 CMD 或另装 Python。
保留同目录的 `_runtime` 文件夹；原有词库与设置仍在 `data`。详见 [应用启动说明](应用启动说明.md)。

仓库：**https://github.com/bsk865/ExplorerDict**（Public，`main` 分支按功能模块逐条提交）。
与 AI 的完整 Prompt 链见 [AI 对话记录](AI对话记录.md)，对话附图在 `docs/对话截图/`，
离线界面预览在 `docs/设计预览/`。

> **文档日期：2026-10-02（最后一轮收口：图窗按当前库刷新 + 异步结果归属核对）**
> 状态：**可用，仍有明确未验收项**（见第 8 节与 `验收报告.md`）
> 本文只描述**代码里已经存在**的功能，不描述计划中的功能。
> 界面预览图在 `docs/设计预览/`（`ui-preview.png` 浮窗、`ui-detail-preview.png` 详情、
> `ui-concept-preview.png` 关系图等 8 张，`python tools/ui_preview.py docs/设计预览/ui-preview.png` 可重新生成）
> 是**离线设计预览**（Pillow 按真实布局参数绘制），**不是屏幕截图**，也不是实机证据。
>
> **2026-10-02 收口（短）**：参考关系图不再使用打开窗口时的旧词快照 —— 点唯一生成
> 入口先按当前主题重读库（nodes / 总数 / 内容指纹）；词条新增 / 编辑 / 删除与解释
> 完成后由主界面既有的真实刷新路径**本地**通知图窗（只更新节点、丢掉过期图与请求
> 关联、提示重新生成，**不自动发任何 LLM 请求**）；异步结果回来再核对当前数据库 +
> 当前配置指纹 + 请求归属，过期结果一律丢弃、不会解除更新请求的忙态。10 模块
> 446 项通过；未实机、未真实 API 调用的边界见 `验收报告.md` 第 0 节。
>
> **2026-10-02 第三轮小修（短）**：内容没变的刷新通知不再清掉已显示的关系图与在途
> 忙态（真变化仍强制作废）；导图提示词第 5 条只禁包含 / 属于的层级矛盾、依赖 / 因果
> 双向仅在两方向各自有材料时允许；浮窗切主题回到所选主题词表；导图离线预览示例逐条
> 自洽。**本轮实际执行**只有白名单 3 模块（88 + 157 + 22）**267 项通过** + `compileall`
> + `tools/ui_preview.py`，退出码均 0；上面「10 模块 446 项」是上一轮全套历史，不是本轮计数。

一个 Windows 桌面小工具：在**任何非游戏、非全屏的前台窗口**里划词 →
阅读面板**自动展开**（**此时不落库、不联网**）→ 点面板上唯一的按钮
**「解释并记录」** → 幂等保存词条（词 + 上下文 + 来源）并异步调用
DeepSeek 兼容 API 生成解释、写回**同一条词条**。词条按「文档批次」归档，
可搜索、可手动录入、可从剪贴板导入、可画概念关系图。

**交互四句话（最终形态）**

1. **划选**：只产生一份内存快照 + 自动展开同一个阅读面板；不写数据库、不发网络。
2. **点「解释并记录」**（面板上唯一的记录按钮）：先把关键词 + 来源 / 上下文
   **幂等**写进 SQLite（同一个选区连续点击只记一次），再异步解释并写回
   **同一个 `entry_id`**；没有 API Key 时**也先保存**，详情页显示
   「已记录 · 待解释」+ 一句**可点的**「请在主界面配置 API」（点它或顶栏
   「主界面」都直接打开主界面）。解释失败时详情页只多出一个短「重试」按钮
   （常驻的「重新解释 / 设置 API」已删除：入口统一归主界面）。
3. **点标签**：**纯本地**打开该词详情（释义 + **这个词自己的追问历史**），不联网；
   想追问就在底部输入框提问，**只有点「发送」才联网**。
4. **点别处**：面板折叠回 44x44 后台小方块，什么都不保存（待处理的划选仍留着）。

设计底线：**只做阅读辅助**。不注入任何进程、不读其它进程内存、不模拟按键、
不给游戏/反作弊添麻烦；前台是游戏 / 全屏 / 手动游戏模式时一律不取词。

---

## 1. 目录与文件

```
探索词典/
├─ 探索词典.exe              ← 日常启动（自带运行环境，无控制台）
├─ _runtime/                 ← 应用运行库，请与 EXE 一起保留
├─ 启动.cmd                  ← 旧版源码启动入口
├─ 启动-控制台.cmd            ← 排错启动（保留控制台 + 日志输出）
├─ bootstrap.py              ← 启动诊断入口（先记日志再导入 app.main；--diagnose 只检查不建窗）
├─ README.md                 ← 本文件
├─ 验收报告.md                ← 各项功能的验收状态与未验证项（诚实版）
├─ API接口说明.md             ← 解释 / 追问用的 DeepSeek 兼容 API 契约与隐私边界
├─ SPEC.md                   ← 设计与需求规格（含非目标）
├─ app/                      ← 应用代码
│  ├─ main.py                ← 入口：建窗、服务装配、选区状态机、UI 事件泵、启动自检
│  ├─ gui_preflight.py       ← 执行前 GUI 预检（**严格白名单**，只有真实阅读前台才允许建窗）
│  ├─ gate.py                ← 前台门控（默认放开普通窗口，只挡游戏/全屏/手动游戏模式/暂停）
│  ├─ permissions.py         ← 已知游戏清单 + 显示名清单（不参与运行时放行）
│  ├─ capture_service.py     ← 取词/落库/事件契约/世代作废
│  ├─ uia_bridge.py          ← 与 UIA helper 的 JSON 协议客户端（失败即降级）
│  ├─ explain_service.py     ← 解释服务（缓存、快照、异步、按 entry_id 归属）
│  ├─ chat_service.py        ← 词条级追问（按词历史、在途闸门、ok/error/discarded 终态）
│  ├─ api_client.py          ← DeepSeek 兼容 chat/completions 客户端（解释 JSON / 追问纯文本）
│  ├─ db.py / config.py / crypto_dpapi.py / paths.py / logging_setup.py
│  ├─ win32util.py           ← 纯 ctypes 的 Win32 封装（无 pywin32）
│  ├─ mouse_hook.py / hotkeys.py
│  └─ ui/                    ← reading_panel（**唯一浮窗**：Dock + 列表 + 详情 + 追问）
│                              panel_geometry（几何纯函数）/ floating（浮层基类）
│                              app_menu / main_window / settings_dialog / manual_dialog
│                              concept_map / theme / widgets
│                              （selection_bar、explain_window 为旧窗口类，保留实现与回归）
├─ scripts/uia_helper.ps1    ← Windows PowerShell 5.1 + UIAutomationClient 取词助手
├─ tools/                    ← 开发/验收辅助脚本（不参与应用运行）
│  ├─ fake_api_server.py     ← 本地假 API 服务（测试与手工连通性验证）
│  ├─ ui_preview.py          ← 离线界面设计预览（Pillow 绘制 PNG；**不建窗口、不截图**）
│  ├─ screenshot_window.py   ← 截窗口为 PNG（默认拒绝运行，需用户主动同意）
│  └─ capture_artifacts.py   ← 启动应用并截图（默认拒绝运行，需用户主动同意）
├─ tests/                    ← 测试（详见第 7 节）
├─ data/                     ← 运行期数据（数据库、日志）
│  └─ logs/                  ← app.log（业务日志）、startup.log（启动诊断日志）
└─ artifacts/                ← 验收产物（final_checks_ui.txt、ui-preview.png 设计预览等）
```

---

## 2. 源码开发依赖（EXE 使用者无需安装）

* **Python 3.11+（64 位）**，实测环境为 **CPython 3.14.5 (64bit)**；`启动.cmd` 需要
  `pythonw.exe` 在 PATH 上（或位于 `%LOCALAPPDATA%\Programs\Python\Python314\`）。
* **无第三方包**：没有 `requirements.txt`，代码只用标准库
  （`ctypes` / `tkinter` / `sqlite3` / `http.server` / `urllib` / `zlib` / `threading` …）。
  * `tkinter` 必须随 Python 一起安装（Windows 官方安装包默认包含）。
  * **不支持 pywin32**：所有 Win32 调用都是 `ctypes` 手写原型。
* **UIA 取词**依赖系统自带的 **Windows PowerShell 5.1**（`powershell.exe`）与
  `UIAutomationClient`/`UIAutomationTypes` 程序集（Windows 自带）。
  找不到 PowerShell 时程序**不报错退出**，只是取词降级为「手动录入 / 剪贴板导入」。
* 不需要管理员权限；不需要联网（只有你主动点浮条上唯一的按钮「解释并记录」
  或主界面的「解释」按钮才会联网）。

---

## 3. 启动方式

双击 **探索词典.exe**，启动只显示浮窗。首次在浮窗中选择是否允许置顶，之后可在托盘菜单更改。已有新版实例在后台时，再次双击只呼出浮窗并提示「已在运行」。主界面由浮窗入口或托盘「打开词典」打开。
游戏或全屏等受限场景下，窗口请求暂存，解除限制后再显示。
若旧版后台实例尚未退出，会提示先完全退出旧版。窗口关闭按钮只转入后台，升级前请使用应用退出功能。

`探索词典.exe` 与 `_runtime` 必须放在同一目录。现有 `data` 保持原位，包含词库、设置与日志；不要删除。
正常启动失败会显示简短错误提示，详细记录位于 `data/logs/startup.log`。

开发者可用 `探索词典.exe --diagnose` 做无窗口导入诊断；不启动取词服务、不访问词库。
旧 `.cmd` 仅作源码开发入口，不再作为日常启动方式。
构建使用 `tools/build-requirements.txt` 与 `tools/build_app.ps1`。

---

## 4. 解释 API 设置（DeepSeek 兼容）

主界面 → **「设置」** → 「解释 API（DeepSeek 兼容 chat/completions）」：

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| 服务地址 | `https://api.deepseek.com/v1` | 任意 OpenAI/DeepSeek 兼容 `chat/completions` 端点 |
| 模型名 | `deepseek-chat` | |
| API Key | 空 | 输入后点「保存」；**用 Windows DPAPI 加密**存入数据库 `secrets` 表，界面不回显明文、日志脱敏 |
| 超时 | 25 秒（下限 3 秒） | |
| 「测试连接」 | — | 用当前 Key 发一次最小请求，验证地址/Key |
| 「清除已保存的 Key」 | — | 删除密文 |

安全约定（代码强制）：明文 Key **不允许**写进 `settings` 表（`Config.set()` 会直接拒绝）；
程序**不读**环境变量或全局配置里的 Key；**未配置 Key 时不会发起任何网络请求**
（但**仍然会先落库**：词条先存下来，配好 Key 后回主界面点「解释」，或在浮窗详情页
点失败时才出现的短「重试」即可）；
请求体只包含「术语 + 上下文」，不带窗口标题/路径等其它信息。

设置入口：主界面工具条「设置」按钮（`App.open_settings`，纯本地、不联网）。
浮窗里**没有**常驻的设置入口 —— 没配 Key 时详情页那句「请在主界面配置 API」
本身可点，点它或点顶栏「主界面」都会直接打开主界面。`open_settings` 的顺序是
**先抬起主窗 → 再建设置窗 → 最后把设置窗抬到最前**（否则设置窗会被主窗压住，
用户点了像没反应）。

**接口契约（请求体 / 响应解析 / 错误分类 / 隐私边界）见 `API接口说明.md`。**

---

## 5. 取词与门控（安全核心）

### 5.1 默认放开普通窗口，只挡游戏 / 全屏 / 用户暂停

产品运行时**不再使用阅读白名单**（用户反馈「阅读白名单挡太多正常软件」）。
判定顺序（`app/gate.py`，只用标准 Win32 前台窗口元信息）：

1. 用户手动暂停 `capture.enabled=0` → 拒绝（硬阻断）
2. 手动游戏模式 → 拒绝（硬阻断）
3. 无前台窗口 → 软受限（不读取、不弹浮层）
4. 前台是本进程窗口 → 软受限
5. 桌面 / 任务栏 → 软受限
6. 已知游戏 / 游戏平台进程（`app/permissions.py` 的 `GAME_APPS`，
   含本机在玩的 War Thunder `aces.exe`）→ 拒绝（硬阻断）
7. 前台窗口全屏 → 拒绝（硬阻断）
8. **其它（包括未知普通程序：微信、QQ、公司内部工具…）→ 允许尝试一次
   标准 UIA `TextPattern` 读取**；读不到就什么都不做

`app/permissions.py` 里仍有一份「已知阅读应用 → 中文显示名」清单，
但它**只用于状态栏显示名**和 **`app/gui_preflight.py` 的自动化严格预检**，
**不参与**运行时放行。

**硬阻断 vs 软受限**（副作用必须分开，`GateController` 三态）：

* **硬阻断**（暂停 / 游戏模式 / 已知游戏进程 / 全屏）→ 收起浮层、
  卸载全局鼠标钩子、停 UIA helper、取消置顶、作废在途手势；
* **软受限**（本程序窗口 / 桌面 / 无前台）→ **只收浮层**：
  钩子与 UIA helper 照常运行、主界面置顶保持不变（切回阅读窗口立刻能取词）。

> `app/gui_preflight.py`（自动化 GUI 预检）**故意比产品运行时更严格**：
> 任何会建窗的测试 / 截图脚本，只有「已知阅读应用 + 非全屏 + 未开游戏模式」
> 才放行；用户玩游戏或前台不认识时一律 skip，**一个窗口都不建**。

### 5.2 全屏暂停

窗口矩形铺满所在显示器（容差 2px）且不是「最大化」→ 判定全屏 → 暂停取词。
全屏游戏、全屏视频、全屏演示一视同仁，这是刻意的安全默认值。
浏览器/Word 的**最大化**不算全屏（正常阅读场景）。

### 5.3 游戏模式：`Ctrl+Alt+Shift+G`

用户主动开关的**最高优先级**暂停：开启后持续禁用全局鼠标监听与取词，
**回到阅读窗口也不会自动恢复**，直到再次按 `Ctrl+Alt+Shift+G` 关闭
（主界面按钮「游戏模式：开/关」等价）。

> **已知边界**：浏览器里的「窗口化网页游戏」只靠进程名**无法可靠识别**
> （它看起来就是一个 Edge/Chrome 窗口）。这种情况**必须由用户手动开游戏模式**兜底。
> 本项目**不宣称**任何反作弊兼容性——兼容性未做任何验证。

### 5.4 全局快捷键（都要求 Ctrl+Alt+Shift 三段，避免与常见软件冲突）

| 快捷键 | 作用 |
| --- | --- |
| **`Ctrl+Alt+Shift+D`** | **呼出**：受限前台下**只打开主界面**并说明原因，不显示浮条 |
| `Ctrl+Alt+Shift+G` | 游戏模式 开/关 |
| `Ctrl+Alt+Shift+S` | 立即抓取一次选区（仍受门控限制；抓到也只弹浮条，不落库） |
| `Ctrl+Alt+Shift+P` | 暂停/恢复取词（回到阅读窗口不会自动解除） |
| `Ctrl+Alt+Shift+Q` | 退出应用 |

启动时**单独校验呼出键**是否注册成功：失败会在主界面顶部显示醒目提示并提供
「重试注册」按钮（不能因为其它热键注册成功就算通过）。

### 5.5 实时门控、晚到事件与「点别处收起」

* **所有浮层显示只有一个实时闸门**：`ReadingPanel` / 旧 `SelectionBar` / 旧
  `ExplainWindow` 的所有显示入口最终都走 `FloatingWindow.show_at()` →
  `App.overlay_display_allowed(window, already_visible, explicit)`
  → `AccessGate.evaluate()`，**不看轮询缓存**。三档策略：
  * **硬阻断**（游戏进程 / 全屏 / 游戏模式 / 用户暂停）→ 一律拒绝：
    不新映射、不移动，**已经显示的浮层立即收起**，被拒绝时**零 `deiconify`**；
    迟到的结果 / 延迟的动作都不可能在游戏前台上屏（写库不受影响）；
    **离开游戏后只恢复 44x44 小方块，绝不自动展开面板**；
  * **普通阅读前台** → 放行；
  * **软受限**（本程序自己的窗口 / 桌面 / 无前台）→ 新映射拒绝；
    **面板自己已经显示时保留**（否则用户在面板里点标签 / 输入框会「先隐藏再显示」，
    输入焦点被门控轮询吞掉）。
* **负坐标显示器用 Tk 绝对坐标写法**：`format_geometry(-1920, 0)` → `"+-1920+0"`。
  Tk 里 `-1920` 的含义是「距屏幕**右边缘** 1920 像素」，只有 `+-1920` 才是
  「绝对坐标 x = -1920」；y 同理（`+-` 是绝对负坐标，`-` 是距下边缘）。
  位置串的语义由 `app/ui/floating.py::tk_position()` 解析，测试按 Tk 文档语义
  验证「解析出来的绝对坐标」，而不是只断言字符串里有没有负号。
* **事件契约**：`selection_ready` 带 `generation`（捕获时的门控世代）、
  `hwnd`/`pid`（捕获窗口身份）与 `point`；`selection_cleared` 带 `reason`。
  UI 消费前逐项回比当前世代与当前前台：世代变了 / 前台窗口或进程变了 →
  判为**过期**：不弹浮条、不解释、不落库。
* **鼠标按下元信息（按下瞬间采集）**：全局鼠标**按下**时在钩子线程里就用
  `WindowFromPoint` + 预先缓存的浮层 HWND 集合判定 `is_overlay`，
  并给非浮层按下**立刻**让世代 +1。因此
  「点别处收起」**不依赖**面板此刻是否还在屏幕上、也**不用任何时间宽限**；
  迟到的旧按下（早于浮层显示）会被忽略，不会误收刚弹出的面板。
* **窗口身份核验（fail-closed）**：UIA 返回的 `process_id`/`hwnd` 必须与当前前台一致
  （`window_root` 比较）；拿不到可核对身份就拒绝，绝不把别处的选区算到当前文档头上。
  鼠标落点在本程序自己的浮层/窗口上时直接排除。
* **前台换窗口 / 同 HWND 换标签页**：`ForegroundWatcher` 检测到后会让世代 +1、
  把面板折叠成小方块并作废旧选区；**没有**任何「签名冷却时间」，
  用户在新文档里重新划选同一个词会立刻自动展开。

---

## 6. 功能一览（已实现）

### 6.1 阅读面板（唯一的浮窗）

* **一个窗口干两件事**：`App.selection_bar is App.explain_window is App.reading_panel`
  —— 选区角色与结果角色是同一个 `ReadingPanel`，不会互相抢位置、不会「一个收起一个还在」，
  也不存在「先缩成小方块再弹回面板」的中间态频闪。
* **44x44 后台小方块（dock）**：默认贴工作区右边缘、垂直居中；普通阅读时后台只留它，
  点它展开、拖动改位置（位置持久化到 `ui.panel.*`）；窗口圆角 10px（Win32 region）。
  四边各内缩「半径 + 描边」= 11px，所以中央字只占 22x22 —— 字号用共用的
  `geo.DOCK_GLYPH_PT`（11pt × 微软雅黑 UI）且 Label **零原生内边距**，
  离线预览用同一份常量复核墨迹确实落在 22x22 里。
* **360x460 展开面板**：可拖标题栏移动、可拖右下角手柄改尺寸；窗口圆角 12px；
  **最小 300x320**，最大不超过工作区；位置与尺寸松手即持久化，换页 / 折叠不重建界面。
  内容四边只由 `outer` 这一层内缩「半径 + 描边」（面板 13px），`panel_body` /
  `dock_body` 铺满它、**不再重复留边**；模式切换只更新这一份边距 —— 任何矩形内容
  都不会铺在圆角圆弧上，右下角手柄也整体压在最底部提示行的净空带里。
  提示 / 选词 / 标题的 `wraplength` 随**实际内容宽度**变化（360 宽 → 330，
  最小 300 宽 → 270），不再写死 322。
* **词表是两列等宽圆角卡片**（8px 圆角、暖米白底 + 浅灰边线；**词语最多两行**：
  按真实像素宽度换行、只有真的放不下才在第二行末尾省略，完整词在详情页正文可查；
  库里**已经有**一句话释义时卡片带一行短摘要，零额外联网），
  右侧是 Canvas 自绘的细滑轨（圆角滑块、无箭头无说明字，滚轮 / 拖滑块 / 点轨道都能滚）。
* **顶部常驻主题标签条**（列表页）：第一个 chip 永远是**当前页** —— 跟随时写着
  「当前页 · 本页主题（n）」（新页面还没存过词时显示占位「当前页 · 新主题」，
  **只显示不写库**，主题在用户点「解释并记录」时才建）；后面是**有词**的历史主题
  （点一个就看那一组词，被看的那个高亮），放不下的收进末尾「+N 个主题」——
  点它展开顶栏内联清单（第一项「跟随当前阅读页」，含 0 条词的空主题：
  那是**管理视图**，标签条只是归纳）。行数上限 2 行，条目多时词卡区不会被挤掉。
  列表页因此**不再单独画主题行**（那一行只在详情页出现，多显示一条来源）。
  宽英文词（如 `Machine learning (ML) algorithms`）在 360 宽面板里**完整显示**，
  不再被按字数截成 `Machine learning (…`。
* **划选自动展开，但只有点按钮才保存**：划选只挂内存快照并展开面板
  （零写库、零联网、NOACTIVATE 不抢阅读焦点）；同一个选区连续点「解释并记录」只操作一次。
  用户用「×」关闭过面板时，新的划选**不会**把它弹回来（快照保留，用顶栏
  「主界面」/ 全局快捷键找回）。
* **点别处只作废待处理选区**：面板的显隐**只由用户与硬阻断决定** —— 点外部、
  换窗口 / 换标签页、选区变空、后台轮询、迟到的解释 / 追问都不会自动折叠或收起它。
* **点标签进详情（纯本地）**：词条 + 释义 + **该词的追问历史**，不联网；
  长标签省略号，完整词在详情页正文可查；卡片分批渲染（每页 60 条，「显示更多」翻页，
  第 61 / 121 条都能点到）。
* **词表跟随当前阅读页面**：每个页面按 `doc_key` 自动成组（主题名默认取该页首个关键词，
  没有 Key 也能用）；**新页面显示空词表**，绝不拿全库顶上；点选左侧已保存的主题只影响
  **浏览**，新词的保存位置永远由页面自动归属决定（`current_page_scope` 全程只读，
  不建主题、不写 settings）。**换页本身不改浮窗正在显示的内容**（翻页不等于「要看新页面
  的词表」）：页面身份在鼠标**开始划选**那一刻就已采到（`note_foreground`），浮窗在
  用户**划选并记录**时才重新取当前页范围、重排词表；在此之前词表 / 主题 / 详情页原样
  保留。**开始划选**（还没点「解释并记录」）时只做一件事：把手工浏览的主题**交还**给
  「跟随当前阅读页」并切到本页主题视图（`show_selection` → `_follow_current_page()`；
  只改高亮与词表范围，**零写库、零请求**，新页面的主题仍然要等保存才诞生）。
  主界面列表仍在打开时跟随当前页面（`follow_current_page`）。
* **按词追问**：详情页底部输入框 + 「发送」；**只有点「发送」才联网**；
  历史**按词条归属**，切词时清掉上一个词的对话缓存 / 请求 token / 忙标记 / **输入草稿**
  并同步该词的真实在途状态 —— A 的对话永远不会显示在 B 上，返回 A 也能看到
  迟到但已落库的回答；迟到的回答**不会**把折叠 / 隐藏的面板重新弹开。
* **详情页的解释入口**：常驻按钮**已经删掉**（旧版的「重新解释 / 设置 API」两连排）；
  只有解释**失败 / 需重解释**时才出现一个短「重试」（只解释这条已保存的词条，
  不重复记词、不加词频；解释在途时禁用防重复）。没配 Key 时只显示一句可点的
  「请在主界面配置 API」，点它或点顶栏「主界面」都直接打开主界面 ——
  失败文案不要求用户回列表重新记录。
* **小尺寸可用**：详情页布局是**底部优先** —— 输入行 + 提示先按 `side="bottom"` 放置，
  中间释义 / 追问区可滚动并在空间不足时先被压缩；词条标题与来源限字数，
  **300x320 或超长标题下输入行不会被挤掉**。列表页同理：底部「当前词 +
  解释并记录」最先按 `side="bottom"` 预留，空词表时「暂无词语」居中在词卡视口正中。
* **没有 Key 也不丢词**：先落库，详情页显示「已记录 · 待解释」；
  在设置里配好 Key 后回主界面点「解释」（或在浮窗点失败时才出现的「重试」）
  沿用同一 `entry_id`，不重复记词、不加词频。

### 6.2 其它

* **解释结果双重归属**（解释服务）：结果按 `entry_id` + **解释请求 token** 同时匹配
  才更新详情页（卡片 / 主界面仍只按 `entry_id` 刷新），旧结果不会覆盖「重新解释」后的新状态。
* **主题按页面自动归档**：每个阅读页面（URL / 窗口标题）自动成组，默认名 = 该页首个
  关键词；解释结果里若带 `topic`，只在**同一条**解释请求内顺手优化组名（不额外联网），
  且**绝不覆盖**用户改的名字或旧库里的名字。左栏只提供**浏览 / 重命名 / 删除**，
  没有手工新建、也没有「固定」。
* **词条详情**：术语、上下文、来源标题、来源 URL 可编辑；编辑后原解释标记过期。
* **手动录入 / 剪贴板导入**：UIA 失败时的明确兜底；剪贴板**只读**，程序从不写剪贴板
  （`send_ctrl_c()` 直接抛异常，禁止模拟 Ctrl+C 窃取选区）。
* **参考关系图**：按当前主题的已收藏词条生成**AI 参考关系**（本地 Tk Canvas，无外部依赖）。
  请求只带本主题的 `term + 截断上下文 + 截断释义`；每条关系必须带简短依据 + 逐字证据片段，
  端点 / 类型 / 自环 / 重复 / 反向矛盾 / 层级成环 / 证据不符全部筛除，依据不足就是空关系；
  图上短标注「AI 参考」，点连线可看依据。旧的 `relations` 表（用户手动边）原样保留，AI 关系不写该表。
* **来源识别**：优先真实 URL（浏览器地址栏），退化到「仅窗口标题识别」，并如实标注置信度。
* **日志**：`data\logs\app.log`，敏感信息脱敏（含按请求快照 Key **按值**替换）。

---

## 7. 测试与「不打扰用户」的硬约束

用户经常玩游戏（War Thunder）。因此：

* **任何会创建窗口的测试/脚本，执行前都必须先过实时前台预检**
  （`app/gui_preflight.py`）：只有「**阅读白名单应用 + 非全屏 + 非游戏模式**」
  才允许创建窗口；否则**明确 skip / 退出，一个窗口都不建**。
* 预检在导入时就**冻结**真实 `win32util` 函数对象，`mock.patch("app.win32util...")`
  **无法**把预检伪造成放行；也没有任何「允许」开关能绕过它
  （`EXPLORER_DICT_ALLOW_GUI_TESTS=1` 只对**默认拒绝的工具脚本**表示「用户主动同意被打扰」，
  **不参与**预检判定）。
* 受预检保护的测试：`test_ui_smoke`、`test_gate_runtime`、`test_capture_events`、
  `test_explain_scoping`、`test_launch`（`--selftest` 建窗）、`test_win32_gate`
  的真实钩子/真实窗口用例、`test_uia_bridge` 的真实 helper 用例。
* 纯逻辑测试（门控策略、数据库、缓存、假 API、预检策略等）**不受影响**，随时可跑。

测试模块与规模（**表内数字以「最近一次真正运行该模块」为准**；本轮白名单与历史轮的
白名单不同，逐条标明。历史轮「12 个显式模块 / 305 项」的记录原样保留在下面）：

| 模块 | 内容 | 最近一次运行状态 |
| --- | --- | --- |
| `test_reading_panel` | 阅读面板：dock ↔ 面板状态机、拖动/改尺寸持久化、划选自动展开零写库、标签本地读取、**词条绑定隔离（切词不串话 / open_entry(B) 丢掉 A 的选区 / 同词不同 context 也按库重建）**、**圆角留白只由 outer 承担 / region 同步时序（请求序号 + 旧尺寸迟到事件）/ 换行宽度随实际内容宽度（hint 另减手柄净空）**、**选词区 `「词」` 含括号与省略号在内 ≤ 2 行（300 宽下按真实渲染输出量长 CJK / 宽英文）**、**解释入口按状态（解释 / 重试 / 禁用 / 隐藏）且不重复落库**、**底部优先布局（假 Tk 结构断言）**、**主界面入口的 button.invoke → 真实 App 方法 → 假依赖（退出走真实菜单定义）** | ✅ 本轮运行（144 通过） |
| `test_chat` | 词条级追问：缺 Key 零请求、按词历史、隐私边界、迟到/删除、**终态保证（写库失败 / 已删竞态 / DB 关闭）** | ⏭ 本轮未运行（历史轮 31 通过） |
| `test_unified_action` | 单按钮链路 + 几何语义 / 统一显示门控 / 请求 token 归属 / 设置往返 | ✅ 本轮运行（58 通过） |
| `test_gate` | 前台门控策略、零 UIA 调用、身份核验 | ✅ 本轮运行（伪造前台，25 通过） |
| `test_source` | 来源归一化 / 文档键 / 固定批次 | ✅ 本轮运行（31 通过） |
| `test_api` | 假 API 服务成功与失败路径（本地 127.0.0.1） | ✅ 本轮运行（20 通过） |
| `test_gui_preflight` | 预检策略 + 不可绕过性 | ⏭ 本轮未运行（历史轮 19 通过） |
| `test_db` | 持久化与只读接口 | ✅ 本轮运行（12 通过） |
| `test_security` | 密钥不明文落库 / 日志脱敏 / 路径不出项目 | ⏭ 本轮未运行（历史轮 11 通过） |
| `test_cache` | 语境缓存键与命中 | ✅ 本轮运行（9 通过） |
| `test_explain_snapshot` | 解释快照 / 缓存键 / 过期不写回 | ✅ 本轮运行（17 通过，本地假客户端） |
| `test_ui_roundrect` | 圆角 / 卡片 / 滑轨 / GDI 所有权 / 按钮字形居中 / **四张离线预览与产品常量同源（含右下角同源灰斜线手柄）** | ✅ 本轮运行（30 通过） |
| `test_hotkeys` | 热键绑定定义 | ⏭ 本轮未运行（历史轮 6 通过） |
| `test_ui_smoke` / `test_gate_runtime` / `test_launch` / `test_capture_events` / `test_explain_scoping` / `test_win32_gate`（真实钩子/真实窗口用例） | 真实建窗 / 真实启动 / 真实钩子 | ⏭ **一律不运行** |

**本轮（2026-10-01 聚焦续作轮：选词两行预算 / 同词不同 context 快照 / 缺 Key 短句）实际运行的三条命令（逐字）**：

```
python -X utf8 -m unittest tests.test_db tests.test_source tests.test_cache tests.test_api ^
  tests.test_explain_snapshot tests.test_reading_panel tests.test_unified_action ^
  tests.test_gate tests.test_ui_roundrect
python -X utf8 -m compileall -q app tools tests
python -X utf8 tools/ui_preview.py
```

* `Ran 346 tests in 14.243s ... OK`（**通过 346 / 跳过 0 / 失败 0**），退出码 0；
  `compileall` 退出码 0；`ui_preview.py` 退出码 0 并重画**四张** PNG
  （字节与 sha256 与上一轮完全一致）。
* 逐模块：`test_db` 12、`test_source` 31、`test_cache` 9、`test_api` 20、
  `test_explain_snapshot` 17、`test_reading_panel` 144、`test_unified_action` 58、
  `test_gate` 25、`test_ui_roundrect` 30（比上一轮 +1：选词区 ≤ 2 行的真实输出量测）。
* 本轮三处定点修复：① `quote_budget` 为 `「`/`」`/`…` 预留格子，最窄 300 宽下长 CJK
  与宽英文的当前词都 ≤ 2 行，截断只改显示、完整 term/context 不动；②
  `App._selection_for_entry` 复用面板手里的快照时要求 **term 与 context 同时**与库内
  完整内容一致，同词不同 context 按库重建；③ 缺 Key 正文改短句「已记录，待解释。」，
  用户可见正文 / 状态栏不再出现已删除按钮名与词条内部编号，底部可点的
  「请在主界面配置 API」保留，库里错误原文一字不动。
* 本轮**没有**跑 `test_chat` / `test_gui_preflight` / `test_security` / `test_hotkeys`、
  没有跑 unittest discover、没有跑任何旧 `.tmp` 运行器、没有跑白名单外的模块。
* 逐项 fake / static 划分见 **`artifacts/interaction_checks.txt`**；本轮的界面几何与
  预览数值见 **`artifacts/design_checks.txt`** 顶部的「最新一轮」小节。

**历史轮（更早的「最终明确测试轮」，与上面的表分开读）**：


```
python -X utf8 -m unittest tests.test_db tests.test_source tests.test_cache tests.test_api ^
  tests.test_security tests.test_gui_preflight tests.test_hotkeys ^
  tests.test_explain_snapshot tests.test_gate tests.test_unified_action ^
  tests.test_reading_panel tests.test_chat
| 取词没反应 | 看主界面状态栏的原因；确认前台是白名单阅读应用、窗口未全屏、未开游戏模式/暂停 |
| 呼出快捷键无效 | 主界面顶部会有「停用」提示，点「重试注册」，或先关掉占用该组合的程序 |
| 面板 / 小方块不见了 | `Ctrl+Alt+Shift+D` 呼出；受限前台下只打开主界面、不显示面板（这是设计） |
| 面板太小 / 输入框不见了 | 面板最小 300x320，且输入行固定在底部优先保留；若仍觉得挤，拖右下角手柄放大即可 |
| 想看日志 | 业务日志 `data\logs\app.log`；**启动**日志 `data\logs\startup.log` |

## 8. 已知未验收项（不夸大）

1. **阅读面板在真实 Windows 上的观感与交互未验收**（本节标题里的「原生外观」指的就是这件事）：
   **没有**在真实桌面上验收过布局可读性（长词条 / 长来源在小面板里的显示）、拖动 / 缩放 /
   置顶过程中的频闪、以及与划选操作的兼容性；也**没有**做过真实显示器上的肉眼观感验收
   （`artifacts/ui-preview.png` 只是按真实参数离线绘制的**设计预览**，不是截图、不能替代观感验收）。
   这**不是**在说「面板没有使用系统原生主题」—— 面板是 Tk 自绘的扁平外观（暖米色 +
   柔黑 + 细线、无阴影、无彩色；圆角由 Win32 region（窗口 10 / 12px）与 Canvas 自绘
   卡片（8px）实现），那是**刻意的设计选择**，不算未验收项。
2. **小尺寸 / 高 DPI 只有结构验证**：300x320 布局、底部优先、标题限高等只有
   **假 Tk 结构断言 + 纯函数断言**；真实拖动 / 改尺寸手感、125% / 150% / 200% DPI 下的
   肉眼效果、真实输入法（IME）候选窗行为、屏幕阅读器 / UIA 无障碍**均未验收**。
3. **UI 视觉频闪未验收**：修闪烁的显示契约（先定位后映射、可见时不重复映射、
   显隐幂等、负坐标绝对定位、合并单窗口）有**纯逻辑回归**覆盖，但**没有**在真实显示器上
   做帧级或肉眼验收 —— 「用户看到的频闪是否彻底消失」仍属**待人工验收**。
4. **全应用 UIA 覆盖未验收**：取词是尽力而为，覆盖范围取决于目标应用是否实现
   UIA `TextPattern`；**没有**做过跨应用覆盖率的实测，也没有真实第三方应用
   （浏览器 / PDF 阅读器 / WPS / Office）的端到端划词验收。
5. **真实跨应用鼠标划词未验收**：真实全局钩子下的端到端划词依赖用户手动操作，未做。
6. **真实 DeepSeek API 未测试**：本机**没有配置 Key**，只验证了本地假服务与假客户端；
   真实网络、真实计费额度、真实模型输出格式、真实限流/错误码**未验证**
   （也**不读取**任何密钥）。
7. **反作弊兼容性未验证**：项目刻意不注入、不模拟输入，但对任何具体反作弊
   是否「绝对安全」**不做承诺，也未做任何测试**。
8. **窗口化网页游戏无法自动识别**：必须用户手动开游戏模式（见 5.3）。
9. **真实 UIA / 真实 GUI 的历史必须如实披露**：
   1. 本次 UI 开发**早期**，DSH 创建并**尝试运行**过 `_tk_layout_check.py`（脚本会调用
      `show_dock` / `expand` / 输入激活 / `settings`）。**首次已知执行至少因临时 DB 权限
      失败**；那次运行**是否已经映射出窗口，未经核实**。Codex 随后中断了这次执行，
      脚本已被删除（仓库中现已不存在）。这条历史**不能**用 `_probe*.ps1` 替代，
      也**不能**据此笼统说「整个 v1.3 从未有过真实 GUI / UIA / 网络行为」。
   2. 上一轮审核发现 `test_single_action_never_collapses_the_shared_window` 未打桩，
      可能触发真实客户端；随后已补 fake。**早先那次是否真的访问到外部服务未经核实**；
      能确定的只有：**最终明确测试轮**（12 模块 305 通过 / 0 跳过）与补丁轮测试
      全部用 fake 客户端 + 临时库。
   3. `tests/test_uia_bridge.py` 在**更早的一次执行中被误跑过**（`Ran 14`，`skip=0`）；
      项目根目录也留有更早的真实 UIA 探针脚本（`_probe_uia.ps1` / `_probe2.ps1`）
      与其报错输出 `_err.txt`。
   因此**不能**声称「从未运行过真实 UIA / GUI 脚本」；只能说：
   **最终明确测试轮**明确未运行它们（见第 7 节）。
10. **真实热键按键未验收**：`Ctrl+Alt+Shift+D/S/P/G/Q` 只验证了绑定定义，
    没有真实按键验收。

---

## 9. 排错

| 现象 | 处理 |
| --- | --- |
| **双击后没有窗口、也没有进程** | 先看 `data\logs\startup.log`：里面有解释器绝对路径、`stdout/stderr` 状态、每个启动阶段和失败堆栈。只想快速判断环境是否正常，运行 `python -X utf8 bootstrap.py --diagnose`（**不建窗**，几秒出结果，末尾是 `STATUS OK` 或 `STATUS FAILED` + 失败项） |
| 双击 `启动.cmd` 没反应 | **请在「文件资源管理器」里双击这个文件**（见下方说明）；再用 `启动-控制台.cmd` 看屏幕报错；日志见 `data\logs\startup.log` 与 `data\logs\app.log` |
| 提示找不到 `pythonw.exe` | 安装 Python 3.11+（64 位）并勾选加入 PATH；`启动.cmd` 会按 PATH → `%LOCALAPPDATA%\Programs\Python\Python31x\` 的顺序找，并把实际解析到的绝对路径写进 `startup.log` |
| 取词没反应 | 看主界面状态栏的原因；确认前台是白名单阅读应用、窗口未全屏、未开游戏模式/暂停 |
| 呼出快捷键无效 | 主界面顶部会有「停用」提示，点「重试注册」，或先关掉占用该组合的程序 |
| 面板 / 小方块不见了 | `Ctrl+Alt+Shift+D` 呼出；受限前台下只打开主界面、不显示面板（这是设计） |
| 面板太小 / 输入框不见了 | 面板最小 300x320，且输入行固定在底部优先保留；若仍觉得挤，拖右下角手柄放大即可 |
| 想看日志 | 业务日志 `data\logs\app.log`；**启动**日志 `data\logs\startup.log` |

### 9.1 「双击没反应」的正确打开方式

**可靠入口：在「文件资源管理器」里双击 `启动.cmd`**（或先双击 `启动-控制台.cmd` 看报错）。

> （本节此处约 2 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

---

## 10. 上一轮（界面收尾 / 按钮居中 / 主题归属 / 严格窗口状态）检查结果 —— 仅离线 mock

> 说明：本节是**上一轮**的记录（323 项等数字属于那一轮）。**当前状态**见第 11 节；
> 本节里出现的「菜单」按钮、3 张预览图、详情页常驻按钮等描述都已被第 11 节取代，
> 原文保留备查。

时间：2026-10-01。完整命令与**真实输出**见 **`artifacts/design_checks.txt`**。

本轮改了三处**有据可查**的缺陷，并检查了相关入口（不扩大范围）：

1. **滑轨拖动换算**（`app/ui/widgets.py`）：`yview("moveto", f)` 要的是**内容**分数，
   旧实现把「滑块行程比例」直接发了出去 —— 列表后半截永远拖不到。现在
   `first = travel × (1 - span)`；`0 < span < 1` 才可滚动，**空 / 满屏一律不滚动、
   一次 `moveto` 都不发**。
2. **主题归属只读化**（`app/capture_service.py`）：新增
   `CaptureService.current_page_scope() -> (batch_id, page_known)`。划选 / 翻页 /
   后台轮询只更新内存指针，**不建主题、不写 settings、不落库**；新页面已知但还没保存过
   词时返回 `(None, True)`，面板与主窗口据此显示**空词表**（不再拿全库顶上）；
   完全没有来源信息的面板探针保持「全部词语」。同页里 UIA 确认过的 `url:` 身份
   不会被轮询的标题回退键顶掉（`_identity_keys` 只升级不降级）。
3. **严格窗口状态**（`app/main.py` + `app/ui/reading_panel.py`）：
   `App._sync_dock_after_gate` 不再无条件 `_restore_dock()`（旧行为会把用户正开着的
   面板折叠成 44x44）。现在已映射的面板只做**实时硬阻断复核**并原样保留；
   隐藏且用户没关过才补回小方块；用户「×」关闭过的**绝不复活**（新的划选也不会）。
   主窗口列表的 `follow_current_page` 真正接线：窗口收起时只记待办，
   下次打开时落地 —— 换页面后列表跟着当前页面走，浏览已保存主题仍只影响浏览。
4. **功能按钮文字居中**（`app/ui/widgets.py` + `tools/ui_preview.py`）：
   `FlatButton` 旧实现写死 `justify="left"`，多行 / 被拉宽的按钮文字会贴左；
   现在统一 `anchor="center"` + `justify="center"` + 两侧同值 `padx`/`pady`，
   任何调用点都不许给单个按钮传 `anchor`/`justify`/`ipadx`/`ipady`
   （`pack(anchor="w")` 只管**按钮整体**在父容器里的位置，与按钮内部文字居中无关）。
   离线预览里那句「按排版盒居中再 +1px」的魔数位移也删了：改成把**真实可见字形
   （含抗锯齿墨迹）的 bbox** 居中在按钮像素区域内，产品与预览同一条规则。
   之后又补了一条：图像尺寸 = **字体排版单元**（`max(墨迹, ceil(字宽))` ×
   `max(墨迹, ascent+descent)`）+ 两侧对称内边距（`glyph_text.text_cell`，预览按
   `SCALE` 折回 1x），可见墨迹仍在整张图像中心 —— 「×」在 9pt 下墨迹只有 7x6px，
   只按墨迹裁图会得到 19x8px 的按钮，现在按排版单元是 21x19px；hover 下划线只画在
   **真实墨迹底边以下**且放得下的行里。`glyph_text.load_font` 同时修了字体面：
> （本节此处约 2 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）


本轮实际执行的检查（全部离线 mock）：

```
python -X utf8 -m unittest tests.test_db tests.test_source tests.test_cache ^
  tests.test_api tests.test_explain_snapshot tests.test_reading_panel ^
  tests.test_unified_action tests.test_gate tests.test_ui_roundrect
python -X utf8 tools/ui_preview.py              # 那一轮重生成 3 张仅窗口离线图 + 按钮居中复核
python -X utf8 -m compileall -q app tools tests
```

* 白名单 9 模块（8 个原有模块 + 纯 mock 的 `test_ui_roundrect`）：**323 项通过 /
  0 跳过 / 0 失败**（`Ran 323 tests in 14.342s` → `OK`；旧 13.836s 属于前片段）；
  `compileall` **0 错误**。
* 本轮**新增**的验收：`test_reading_panel::TestQueuedResultAfterUserEditOrMove`（3 项：
  解释服务已落库、结果事件还排在 UI 队列里时用户又编辑 / 移动 / 点「×」，
  UI 消费排队结果时**不二次改名**、不碰新主题、不复活面板）；
  `test_db::TestSchemaMigration`（v2 旧库迁移后数据 / 设置 / 缓存一条不少，
  补齐 `name_source` / `topic`，版本号 = `SCHEMA_VERSION`，重开幂等）；
  `test_ui_roundrect::TestFlatButtonCentering` + `TestOfflinePreviewButtonCentering`。
* `tests/test_chat.py` 里「旧库迁移后 `schema_version == "2"`」是 v3 迁移后的**过时断言**，
  已改为跟随 `SCHEMA_VERSION`；**本轮没有重跑整个 `test_chat`**，等价的迁移保留验证
  放在了白名单 `test_db` 里（不重复造第二份）。
* `artifacts/ui-preview.png` / `ui-detail-preview.png`（360x460，圆角 12px，两列圆角卡片 +
  滑轨）/ `ui-dock-preview.png`（44x44，圆角 10px，透明背景）已重生成，
  sha256 与逐按钮居中数据记在 `artifacts/design_checks.txt`。

**最后这一轮白名单检查里明确未做（不夸大）**：没有创建任何真实 Tk / Toplevel 窗口
（即便 hidden 也没有）、没有截图或读取桌面像素、没有切换焦点、没有安装 hooks / UIA /
热键，没有真实 API / 生产数据库 / Key，没有安装或启动 / 关闭任何用户程序。
> （本节此处约 1 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

   现在统一 `anchor="center"` + `justify="center"` + 两侧同值 `padx`/`pady`，
   任何调用点都不许给单个按钮传 `anchor`/`justify`/`ipadx`/`ipady`
   （`pack(anchor="w")` 只管**按钮整体**在父容器里的位置，与按钮内部文字居中无关）。
   离线预览里那句「按排版盒居中再 +1px」的魔数位移也删了：改成把**真实可见字形
   （含抗锯齿墨迹）的 bbox** 居中在按钮像素区域内，产品与预览同一条规则。
   之后又补了一条：图像尺寸 = **字体排版单元**（`max(墨迹, ceil(字宽))` ×
   `max(墨迹, ascent+descent)`）+ 两侧对称内边距（`glyph_text.text_cell`，预览按
   `SCALE` 折回 1x），可见墨迹仍在整张图像中心 —— 「×」在 9pt 下墨迹只有 7x6px，
   只按墨迹裁图会得到 19x8px 的按钮，现在按排版单元是 21x19px；hover 下划线只画在
   **真实墨迹底边以下**且放得下的行里。`glyph_text.load_font` 同时修了字体面：
> （本节此处约 21 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）


本轮实际执行的检查（全部离线 mock）：

```
python -X utf8 -m unittest tests.test_db tests.test_source tests.test_cache ^
  tests.test_api tests.test_explain_snapshot tests.test_reading_panel ^
  tests.test_unified_action tests.test_gate tests.test_ui_roundrect
python -X utf8 tools/ui_preview.py              # 那一轮重生成 3 张仅窗口离线图 + 按钮居中复核
python -X utf8 -m compileall -q app tools tests
```

* 白名单 9 模块（8 个原有模块 + 纯 mock 的 `test_ui_roundrect`）：**323 项通过 /
  0 跳过 / 0 失败**（`Ran 323 tests in 14.342s` → `OK`；旧 13.836s 属于前片段）；
  `compileall` **0 错误**。
* 本轮**新增**的验收：`test_reading_panel::TestQueuedResultAfterUserEditOrMove`（3 项：
  解释服务已落库、结果事件还排在 UI 队列里时用户又编辑 / 移动 / 点「×」，
  UI 消费排队结果时**不二次改名**、不碰新主题、不复活面板）；
  `test_db::TestSchemaMigration`（v2 旧库迁移后数据 / 设置 / 缓存一条不少，
  补齐 `name_source` / `topic`，版本号 = `SCHEMA_VERSION`，重开幂等）；
  `test_ui_roundrect::TestFlatButtonCentering` + `TestOfflinePreviewButtonCentering`。
* `tests/test_chat.py` 里「旧库迁移后 `schema_version == "2"`」是 v3 迁移后的**过时断言**，
  已改为跟随 `SCHEMA_VERSION`；**本轮没有重跑整个 `test_chat`**，等价的迁移保留验证
  放在了白名单 `test_db` 里（不重复造第二份）。
* `artifacts/ui-preview.png` / `ui-detail-preview.png`（360x460，圆角 12px，两列圆角卡片 +
  滑轨）/ `ui-dock-preview.png`（44x44，圆角 10px，透明背景）已重生成，
  sha256 与逐按钮居中数据记在 `artifacts/design_checks.txt`。

**最后这一轮白名单检查里明确未做（不夸大）**：没有创建任何真实 Tk / Toplevel 窗口
（即便 hidden 也没有）、没有截图或读取桌面像素、没有切换焦点、没有安装 hooks / UIA /
热键，没有真实 API / 生产数据库 / Key，没有安装或启动 / 关闭任何用户程序。
> （本节此处约 23 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

   四张 PNG 是 **Pillow 离线绘制、不是实机证据**。

**历史边界记录一律保留**：本轮的说明**不删除、不改写**第 7 节「历史披露」、
第 8 节第 9 条，以及 `artifacts/final_checks_ui.txt`、`artifacts/codex_review_selection_bar.md`
里已经写明的早期真实 UIA / GUI 触碰事实。

## 12. 本轮（浮窗追问可见性 / 自由拖动 / 主题内联选择 / 导图可用 / 统一无框 chrome）结果 —— 仅离线 mock

**真实根因（都不是「异步保存」）**

1. **追问看不见 = 竖向空间被 pack 吃光**：`ChatService.submit` 在返回 `request_id`
   之前就**同步**写入用户那一问，数据一直在库里；问题是详情页把追问区放在 pack
   顺序最后，空间不足时 pack 把它压成 **0 高度**（最小面板 320 高必然如此，默认
   460 在标题换行 / 出现「重试」行时也会）。修法：追问块**先于**释义区 pack，两块
   可滚动区的高度由纯函数 `panel_geometry.detail_area_heights()` **显式分配**
   （`PANEL_HEAD_H` + `DETAIL_FIXED_H` 预算，两块各自 ≥ `DETAIL_AREA_MIN`），
   释义区 `expand` 吸收估算误差。360x460 → 释义 111 / 对话 97 设备像素。
2. **发出去「没反应」**：请求被接受后加**本地回显**（`_chat_echo`）—— 立刻画
   「你：<问题>」+「词典：正在回答…」；回答到达后重读历史并把对话**滚到最后一条**
   （`see(end)` + `yview_moveto(1.0)`）。忙态禁用「发送」且回车先提示「上一条还在
   回答中」；缺 Key / 失败保留输入并给一句内联短反馈；切词清缓存、迟到结果按
   「当前词 + 当前请求号」双重校验后丢弃。
3. **拖动**：拖动面扩到小方块 / 展开态标题带 / 标题文字 / 顶栏主题行 / 空白底，
   并加 `_is_interactive` 守卫 —— 按钮、输入框、词卡不绑拖动、按下也绝不进入
   拖动状态；松手未位移时小方块 = 展开、顶栏主题行 = 开合主题清单。
4. **主题选择**：浮窗顶栏短主题**可点开内联清单**（第一项「跟随当前阅读页」+
   最近 8 个历史主题），**不用** 在 NOACTIVATE 窗口下失效的 `tk_popup` / grab 菜单；
   浮窗与主界面侧栏共用宿主的同一份浏览范围（`App.browse_scope` /
   `set_browse_scope`），浏览只影响显示，新词仍按来源页面保存。主界面检索 = 主题
   选择 + **全库搜索**（搜索框有内容时不再被「当前这一页」限制）。
   （第 15 节起：列表页顶部多了一条**常驻主题标签条**，同一份内联清单也能从末尾
   「+N 个主题」chip 打开 —— 「管理视图」（含空主题）与「归纳」（只列有词主题）分开。）
   （第 15 节起：列表页顶部多了一条**常驻主题标签条**，同一份内联清单也能从末尾
   「+N 个主题」chip 打开 —— 「管理视图」（含空主题）与「归纳」（只列有词主题）分开。）
5. **导图**：不再「没有关系就一片空白」—— 选中的主题是中心节点，已收藏词条围绕
   排布，包含线只表示**分类结构**；语义关系完全由用户在窗口里添加 / 删除（绝不
   请求模型）。同名词靠**唯一显示名**区分（`Transformer` / `Transformer（2）`），
   界面不出现 `#id`；首次 `<Configure>` 前用兜底尺寸 + 尺寸变化重画，窗口单例
   只 lift、不再隐藏父窗口；主题 / 删除 / 缩放都只给内联一句短反馈（无 messagebox）。
6. **统一无框 chrome**：新增 `widgets.BorderlessChrome`（自绘标题带 + 拖动 +
   `×` + Esc / Alt+F4 + 可选右下角缩放），主界面 / 导图 / 设置 / 手动录入共用；
   主界面**去掉原生菜单栏**与「全部词语」「置顶」两个复选框，设置 / 暂停取词 /
   游戏模式各只剩一组，工具条右端保留**唯一**「退出」；详情页解释入口按状态合并成
   **一个**按钮（未解释→解释 / 失败→重试 / 已解释→重新解释）；浮窗详情返回改
   「← 返回词表」；校验反馈尽量内联，不再新增系统 messagebox。浮窗保持原有连续圆角。

**本轮命令结果**

* 白名单 9 模块 **365 项通过 / 0 失败**（另 `tests.test_chat` 31 项通过，
  已核实全程使用假 Tk / 假客户端，无真实窗口与网络）；
* `python -m compileall app tools tests bootstrap.py` 退出码 0；
* `python -X utf8 tools/ui_preview.py` 退出码 0，重画 **8 张** PNG：列表 / 详情 /
  空态 / 小方块 + **主题选择 / 追问问答 / 新主界面 / 导图**；按钮墨迹居中最大
  偏移 1.0px（≤ 1 设备像素），小方块字形仍落在 22x22 内。

**未实机限制（不得当成已验收）**

* 全部结论来自**真实产品代码 + 假 Tk 控件 / 假 Win32 / 临时 SQLite**，没有创建
  任何真实窗口，因此**没有**实机验证：拖动跟手感、`overrideredirect(True)` 之后
  主窗口的键盘焦点与 Alt+F4 行为、圆角 region 与自绘 chrome 的实际观感、真实
  追问请求的端到端时延。8 张 PNG 是 **Pillow 离线绘制，不是实机证据**。
* 未跑白名单外的模块；未触碰真实 Win32 / UIA / 钩子 / 热键 / 截图 / 剪贴板 /
  生产库 / 真实 Key 与外部 API。

## 13. 本轮（AI 参考关系图：证据校验 + 关系结构布局）结果 —— 仅离线 mock

**用户最新要求优先于 SPEC.md 旧 FR-11**（旧的「只允许用户手动建边」作废；`SPEC.md`
FR-11 第 4 条、FR-5 的导图条目、数据模型已按新要求改写，历史记录保留）。

1. **界面**：删掉手动起点 / 终点 / 关系名输入、添加 / 删除关系按钮、关系清单与长说明、
   界面里的 `#id`；保留同风格无框 chrome、左侧**简洁主题选择**与底部**唯一**
   「生成参考关系 / 重新生成 / 重试」入口 + 必要状态；缺 Key 时内联「打开设置配置 API Key」。
2. **链路**：打开图 / 切主题 / 点按钮是**唯一**的请求触发点。命中
   `(主题, term/context/释义 内容指纹 + 模型配置)` 的缓存就立刻显示；没有缓存先画词节点，
   再异步生成；结果经 UI 队列回窗口，窗口只接受与**当前主题 + 当前指纹**都匹配的结果
   （切主题 / 改词 / 关窗后的迟到结果一律丢弃，不串图、不复活）。
3. **依据（两道关）**：结构化 JSON 只准用本次给出的词编号作端点，类型限定
   包含 / 属于 / 依赖 / 用途 / 因果 / 对照；每条带简短依据 + 逐字证据片段。
   端点越界 / 自环 / 重复 / **层级互相包含** / 包含·属于成环 / 类型缺失 / 证据对不上
   材料（含「只把端点词抄一遍」的弱证据）一律筛除并计数；**再由第二次调用独立核对**
   这批候选（输入只含端点上下文 / 释义、具体方向、依据与证据），核对**只准按序号回填
   判定**、不许新增或改写边，只有明确 `supported` 且仍通过本地校验的边才画 / 才缓存；
   `uncertain` / `contradicted` / 缺判定 / 判定坏掉一律不画不缓存，核对调用本身失败按
   错误处理并可重试。依据不足允许空关系（不强行连线、不默认「相关」）；错误 JSON /
   schema 错误只给短状态 + 重试，**不画伪图**。模型猜测始终标注「AI 参考」，不是原文事实。
4. **布局**：包含 / 属于形成层级分组（父在上，子树带浅色底板），依赖 / 因果分前后层，
   **互相依赖 / 互为因果**这类真实反馈经核对支持后**保留**：布局用强连通分组让整个
   反馈圈同层，圈内的边画成反馈弧线（不伪造层级、也不删掉真实反馈）；只有互相包含与
   包含·属于成环才拒绝。用途 / 对照是带方向与标签的跨边，孤立词单独成行可见；
   **没有**固定圆环，也**不**按词数随机摆放 —— 位置只由关系结构决定（同一份纯函数
   `concept_map.layout_graph`，离线预览也调用它）。节点文字按宽度换行，不再砍到 6 个字；
   连线端点落在节点边界上；支持滚轮滚动 / Ctrl+滚轮缩放（0.6x–2.0x）与窗口缩放重排。
   单次分析有硬上限（`MAP_MAX_ENTRIES=30`，默认配置 24）：被截断时窗口用一句短状态
   明说「本次分析 N 词 / 该主题共 M 词」，绝不默默漏掉用户的词表。
5. **数据**：`map_graphs` 表是 v3 → v4 的**增量迁移**（只新增表 / 索引，不动既有数据），
   v4 → v5 再增列 `validation_version`：缓存里存的是**已通过本地校验 + 独立核对**的边，
   版本不一致（例如旧版只做过字串证据校验）的缓存一律不命中。AI 参考关系**只**进这张
   缓存表，`relations` 里的历史手动边原样保留。
6. **同轮界面修正**：点击输入框时键盘焦点留在被点的 Entry / Text（窗口级绑定不再无条件
   `focus_force`）；主窗口**始终普通层级**（`-topmost` 恒为 False，只有浮窗在普通阅读下
   自动置顶，游戏 / 全屏硬阻断照旧），设置页与工具条不再有「置顶」开关（旧配置字段兼容
   保留但不驱动层级）；主题行的展开箭头改成**组合式画布** chevron（不再用 `▾` 字形、也
   不再覆盖 Tkinter 保留的 `_w` 窗口路径）+ 半描边净空；追问预览默认画**完成态**一问一答；
   热键失败提示与「关闭面板」后的短状态只提界面上真实存在的入口（不再指向已删除的浮窗菜单）。

**本轮命令结果**：白名单 10 模块 **440 项通过 / 0 失败**（`tests.test_db` 14 /
`tests.test_source` 31 / `tests.test_cache` 9 / `tests.test_api` 20 /
`tests.test_explain_snapshot` 17 / `tests.test_reading_panel` 154 /
`tests.test_unified_action` 58 / `tests.test_gate` 25 / `tests.test_ui_roundrect` 81 /
`tests.test_chat` 31）；`python -m compileall app tools tests` 退出码 0；
`python -X utf8 tools/ui_preview.py` 退出码 0，重画 8 张 PNG（导图预览改为 AI 参考
关系图，位置来自产品同一份布局函数）。（上面第 12 节的 365、本节先前的 423 都是各自
临时 SQLite，**没有**真实 Tk 窗口、没有真实 Key、没有一次外部 API 请求，因此
「模型实际返回什么关系、布局在真实 DPI 下的观感、滚轮缩放手感」都未实机验证。

---

## 14. 本轮（浮窗详情页文字被裁 / 换页不再改浮窗显示）结果

**用户报告（原话）**：「解释应该是直接显示在右侧悬浮窗的，然后可以进行对话。但是现在
的问题就是文字显示不正常被边框遮挡」；「敏感度应该变化切换页面不意味着需要直接更改
悬浮窗口的显示主题，只有鼠标开始刷选词语的时候进行检测」。附带截图：词条标题只剩中段
几像素、释义区为空、对话区顶着一行「暂无对话」却占了近 400px。

1. **根因（滑轨的请求高度）**：`app/ui/widgets.py` 的 `ScrollRail` 建滑轨 Canvas 时
   没写 `height`，于是用了 Tk Canvas 的默认高度 **7cm** —— `tk scaling=2.0`（144 DPI）下
   是 **397 设备像素**。滑轨本身 `pack(side="right", fill="y")` 由容器决定真实高度，但
   那 397px 的**请求**高度照样经 pack 传播成整个 `ScrollArea.frame` 的请求高度；详情页
   对话区 `pack(side="bottom", fill="x")` 没有 `expand`，因此真的拿到 397px，把**最后
   pack** 的释义区（`expand=True`）挤成 0、词条标题挤成几像素 —— 正是截图现象。
   修复：滑轨 Canvas 显式 `height=0`（请求 1px），容器高度只由正文 Canvas
   （`_sync_detail_areas`）决定。**注意**：早先「窗口高取值过期（≈1250）」的推断是**错的**
   （用截图反解切分公式凑巧吻合 397）；教训是**先量请求高度，再反解公式**。

2. **实测证据（真实 Tk，不打扰用户）**：
   * `_check/rail_probe.py`：Canvas 不写 height → 请求 **397**；`height=0` → **1**；
     `frame[rail(旧)+body(145)]` → **397**；`frame[rail(新)+body(145)]` → **145**；
     同一窗口高下 `panel_geometry.detail_area_heights` = `(166, 145)`。
   * `_check/panel_layout_probe.py`（真实 `Database` / `Config` / `theme.init(root,144)` /
     真实 `ReadingPanel`，窗口 `-alpha 0.0` 且清空窗口 region ⇒ 不可见、不可点击；
     数据目录重定向到工作区内的库副本）：**修复后** `_detail_window_height()=690`、
     切分 `(166,145)`、`def_area.frame` 实得 220、`chat_area.frame` 实得 145、
     标题完整（41px）、释义正文有内容；**模拟旧代码**（把滑轨高度设回 397）时
     `chat_area.frame` 实得 397、`def_area.frame` 实得 **1**、面板内容自然高度 **1152**
     ⇒ 与截图一致。

3. **换页语义（用户第 2 条要求）**：`ReadingPanel.follow_page_change()` 不再
   `clear()` + `refresh_terms()` —— 换页只交还手工浏览范围、收起主题选择，
   **浮窗正在显示的内容（词表 / 主题 / 详情页 + 追问）原样保留**，显隐 / 尺寸 / 位置
   一个像素不动。刷新的真实时机是用户**划选并记录**时：`App._record_snapshot_once` →
   `_refresh_panel_terms` → `panel.refresh_terms()`（以及 `show_recorded`）；
   页面身份在鼠标**开始划选**时（`CaptureService.note_foreground`，抬手采前台）就已确定，
   所以那次刷新取到的一定是划词时所在的页面。主界面列表仍按 `follow_current_page`
   在打开时跟随当前页面（右/左两处 docstring 的旧口径「浮窗必须立刻改呈新页面词表」
   一并改写）。注意 `ReadingPanel.show_selection` 在 `app/` 内**没有调用者**（旧共用
   接口的遗留方法），真实划选只弹浮选条 `SelectionBar`。

4. **回归**：白名单 13 模块 `Ran 529 / failures=7`（`_check/tests_after_fix2.log`），
   7 条与改动前**完全相同**：`tests.test_unified_action` 4 条
   （`test_other_window_switch_keeps_bar_and_voids_selection` /
   `test_game_process_hard_blocks_and_keeps_zero_uia` /
   `test_self_window_does_not_churn_services_or_drop_topmost` /
   `test_selection_alone_writes_nothing`）与 `tests.test_reading_panel` 2 条
   （`test_game_to_normal_restores_dock_only`、`test_normal_topmost_off_keeps_panel_rect_visible_closed_pending`）
   是**本轮之前就存在**的过期断言（见第 13 节「440 项通过」的旧计数已经不准）；
   第 7 条 `test_root_is_withdrawn_before_build_app`（`AssertionError: 5 != 0`）是**环境性**
   的：该测试没有隔离进程守卫（`app/main.py` 的 `acquire_process_guard()`），本机有正在
   运行的实例时返回 5（已有实例）⇒ 与本次改动无关。被改写的
   `tests.test_reading_panel.TestPageScopedTermList` **9 项全过**；
   `python -m compileall app tools tests bootstrap.py desktop_entry.py` 退出码 0。

5. **同轮附带**：`README` 第 6.1 节「词表跟随当前阅读页面」补上**时机**（换页不改显示、
   划选并记录时才刷新）；`tests/test_reading_panel.py` 里
   `test_visible_panel_follows_the_page_switch_and_comes_back_on_a` 改名并重写为

5. **同轮附带**：`README` 第 6.1 节「词表跟随当前阅读页面」补上**时机**（换页不改显示、
   划选并记录时才刷新）；`tests/test_reading_panel.py` 里
   `test_visible_panel_follows_the_page_switch_and_comes_back_on_a` 改名并重写为
   `test_visible_panel_keeps_its_content_across_page_switch`（走真实记录链路验证刷新）。

**未实机限制（不得当成已验收）**：窗口虽然是真的，但它是**不可见**的（`-alpha 0.0` +
这几个文件（`app/ui/widgets.py`、`app/ui/reading_panel.py`、`app/main.py`、
`app/ui/main_window.py`、`tests/test_reading_panel.py`）。

---

## 15. 本轮（词卡文本排布 / 主题标签条 / 划选自动回本页）结果

**用户要求（原话）**：「针对悬浮窗口。优化一下词语标签的文本排布。主题也要有归纳，
比如默认状态下可以选择不同的主题标签，进入主题标签后可以查看对应的关键词。用户开始
刷选后自动打开对应的主题界面，如果是新的界面就创建一个新的主题标签界面。」
（附截图：两列词卡里长英文词显示成 `Machine learning (…`、摘要小灰字贴底边；
主题只能靠顶栏那一行点开内联清单。）

**三条落地内容**

1. **词卡文本排布**（`app/ui/widgets.py` `TermCard` + `app/ui/panel_geometry.py`）：
   词语 Label 现在带 `wraplength`（`_layout()` 每次按内宽重设），**按真实像素宽度换行、
   最多两行**（`geo.CARD_TERM_LINES = 2`），只有真的放不下才在第二行末尾补「…」——
   截断改由 `geo.card_text_units(col_w, term_px, lines=2, pad_x_px=…)` 算出的**宽度预算**
   决定，不再是按字符数的 `clip_tag`（旧预算 18 单位，`Machine learning (ML) algorithms`
   是 32 单位 ⇒ 必然被砍）。卡片常量 `CARD_H 64→70→84`、`CARD_PAD_X 10→11`、
   `CARD_PAD_Y 8→9`；摘要从 `CARD_SUMMARY_PT 7→8`、`TEXT_FAINT→TEXT_MUTED`（仍是一行）。
   360 宽面板下词预算 23 单位、540 宽 39 单位 ⇒ 长英文词在 360 及以上都完整显示。
   **行高 ≠ 字号**（第二轮实测发现的真 bug）：卡片内部原先按**字号**预留高度，而 Tk 的
   `linespace` 更大（96 DPI 下 13px 词语一行 21px、11px 摘要一行 18px；144 DPI 下 20px
   词语 29px、16px 摘要 23px）。旧摘要槽 `abs(device_px(8)) + 2 = 18px < 23px` ⇒
   **144 DPI 下摘要下半截被裁掉 5px**（截图里小灰字贴底边就是这个）。只加高摘要槽又会把
   词语第二行挤掉，所以两件事一起做：`geo.CARD_H = 84`（注释给出算式：96 DPI 最紧
   `2*9 + 2*21 + 2 + (18+2) = 82` 留 2px；144 DPI 实际 126px，`TermCard` 需要 113px），
   并新增 `geo.text_line_px(font_px)`（`TEXT_LINE_SPACING = 1.62`，取
   `max(字号, round(字号*1.62))`）与 `widgets.line_height(size)`（先取前者，再与真
   `tkinter.font.Font(...).metrics("linespace")` 取 max，异常退回公式值 ⇒ 假 Tk /
   离线预览同样可用）。`TermCard._summary_height()` 现在是 `line_height(8pt) + 2`、
   `_term_height()` 是 `body - 摘要槽 - 2`；离线预览 `draw_cards` 的行高也改用
   `geo.text_line_px`，两边同源。
2. **主题归纳：常驻标签条**（`app/ui/reading_panel.py`）：
   `_build_list_page()` 在操作区之后插入 `topics_row`/`topics_host`，
   `_render_topics(batch_id, page_known)` 画 chip（`_wrap_chips()` 贪心换行、
   上限 `geo.CHIP_ROWS = 2` 行，放不下的收进「+N 个主题」）。第一个 chip 永远是
   **当前页**：跟随时「当前页 · 本页主题（n）」并高亮；新页面（`page_known` 且
   `batch_id is None`）显示占位「当前页 · 新主题」，**只显示不写库**；没接
   `capture_service`（只读探针 / 旧宿主）时退化成「全部词条」。其余 chip = **有词**的
   历史主题（`strip_topic_options()`：跳过 0 条词的主题、至多 8 个）。点主题 chip 走
   原有的 `_on_topic_pick()`（浏览只影响显示，新词仍按页面归属保存）；点「当前页」chip
   走 `_on_follow_current_page()`（交还手工浏览范围）。**列表页不再单独画主题行**：
   `_render_context()` 用新的 `_topic_line()`/`_source_wanted()`，`_render_pages()` 在切页后
   重同步一次 —— 来源行只在详情页出现（详情页仍带展开箭头）。
   **内联清单仍是管理视图**：`topic_options()` 不过滤 0 条词的主题（点「+N 个主题」看得到
   空主题、点进去能确认「词被删光了」），标签条才是只列有词主题的归纳。
   第二轮在一个**真实 Tk 窗口**（`-alpha 0.0` + 空 region，不打扰用户）上量了 chip 的
   放置宽度与 Tk 的 `winfo_reqwidth()`：300x320 面板（`tags_w = 259`）下 chip 换行成两行
   （`150/101` + `111/79`，每行 ≤ 259），499 宽下 8 个 chip + 「+2 个主题」，每个 chip 的
   放置宽度都 ≥ 它自己请求的宽度（150 vs 132、145 vs 134、111 vs 101）⇒ 真字体下不裁字；
   点主题 chip ⇒ `browse_scope()` 切到该主题、chip 变高亮；点「+N 个主题」⇒ 内联清单打开
   （实测清单出现在**标签条上方**：清单 `abs_y=174`、词表页 `abs_y=392`，与离线预览
   「顶栏 → 内联清单 → 标签条 → 卡片」一致；列表页上来源行与它的分隔线都确实收起，
   `source_row mapped = False`）；
   点「当前页」chip ⇒ `browse_scope()` 回到 `None`（交还范围 1 次）。这一轮修掉两个真 bug：
   ① `_sync_wraplengths()`（改尺寸路径）调 `_render_topics(reload=False)` 时没带页面范围，
   任何一次改尺寸都会把第一个 chip 退化成「当前页 · 新主题」——现在记 `self._topics_page_scope`
   并回传；② 画布宽度回调 `_reflow_tags(width)` 只重排词卡、不重排 chip，窄窗口下 chip 会
   按旧宽度摆放（宽度只是变窄时不会自动换行）——现在连同 `row_w=width`（回调给的是**新**
   宽度，比此刻的 `winfo_width()` 可靠）一起重排。
3. **划选自动打开本页主题界面**（`show_selection()`）：划选到达时先
   `_follow_current_page()`（`browse_scope()` 非 None 才交还；`app.release_browse_scope()`
   纯内存、异常只记日志）再 `refresh_terms(force=True)`。**零写库、零请求**：新页面的
   主题对象仍然只在用户点「解释并记录」时诞生（页面身份在鼠标按下那一刻
   `note_foreground` 已采到）。`follow_page_change()` 复用同一个 `_follow_current_page()`，
   并在真的交还了范围时重排一次 chip（只改高亮 / 文案，词卡与详情一个像素不动）。

**实测与回归**

* `tests.test_reading_panel` + `tests.test_ui_roundrect`：**Ran 284 / failures=3**
  （本轮之前是 282，多出来的正是下面两项新测试），
  3 条都是本轮之前就有的（`test_root_is_withdrawn_before_build_app` 环境性：本机有实例
  运行时 `acquire_process_guard()` 返回 5；`test_game_to_normal_restores_dock_only`、
  `test_normal_topmost_off_keeps_panel_rect_visible_closed_pending` 是过期断言）。
  新增 `TestTopicChips`（6 项：chip 文案 / 空主题不进条 / 新页面占位且零写库 /
  点 chip 切主题 + 点「当前页」回来 / 溢出收进「+N 个主题」/ 划选自动回本页）**全过**；
  改写了三处旧断言的契约（`test_list_top_row_shows_only_the_short_topic`、
  `test_terms_are_read_locally_and_long_tag_is_ellipsized`、
  `test_cards_keep_size_and_detail_keeps_full_term`）。
* 新增 `TestTagDisplayBudget` 两项：`test_card_vertical_budget_fits_two_term_lines_and_one_summary`
  （对 scale 1.0 / 1.5 / 2.0 用同一套 `px()` / `font_px()` 复算 `geo.CARD_H`，把「两行词语 +
  一行摘要」的常量关系钉死）与 `test_real_card_gives_the_term_two_lines_and_the_summary_its_line`
  （真 `TermCard` 上断言 `_term_height() ≥ line_height(10pt)*2`、`_summary_height() ≥ line_height(8pt)`）。
  该套件单跑 **Ran 4 OK**。
* **13 模块白名单回归**（`tests.test_db/test_source/test_cache/test_api/test_explain_snapshot/
  test_reading_panel/test_unified_action/test_gate/test_ui_roundrect/test_chat/test_security/
  test_hotkeys/test_gui_preflight`）：**Ran 537 / failures=7**，与改动前基线
  （`Ran 535 / failures=7`）**逐条同一批**：4 条在 `tests.test_unified_action`、3 条在
  `tests.test_reading_panel`（含那条环境性的 `test_root_is_withdrawn_before_build_app`：
  本机有实例在跑时 `acquire_process_guard()` 返回 5）⇒ **没有引入新失败**；总数 +2 正是
  本轮新增的两项。
* `tests.test_ui_roundrect` 单跑 **Ran 110 OK**（离线预览的按钮墨迹居中 ≤1px、
  几何取自产品常量、七张 PNG 齐全）。
* 离线预览 `tools/ui_preview.py` 已按新布局重画，并**直接调产品纯函数**
  （`ReadingPanel._wrap_chips` / `geo.chip_width` / 新的词卡预算）：新增
  `draw_topic_chips()`，列表页与空词表页改成「顶栏 → 标签条 → …」，主题选择页改成
  「顶栏 → 内联清单 → 标签条 → 卡片」，详情页 / 追问页补回主题行的展开箭头。
  产物：`artifacts/ui-preview.png`（列表 + 两行 chip）、`ui-topic-preview.png`、
  `ui-empty-preview.png`（占位 chip）等七张。
* **这一轮的实机尺度**：chip 与词卡的排布不是只靠假 Tk 推的 —— 用两个**不可见真窗口**
  探针（`-alpha 0.0` + 空 region，不改前台、不截屏、不装钩子）量了真字体：
  `_check/chips_real_probe.py`（chip 放置宽度 vs `winfo_reqwidth()`、两行换行、点主题 /
  「+N 个主题」/「当前页」三条点击链）与 `_check/card_metrics_probe.py`（96 / 144 DPI 下
  `linespace` 实测，就是它发现了摘要被裁 5px）。两个探针都指向**临时数据目录**
  （`_check/_chipsprobe`），不碰用户的库。
* **未实机限制**：观感层面仍未验收 —— 「真实 DPI 下肉眼看到的观感、拖到别处、鼠标滚轮、
  用户手上的实例」；本轮的量测只覆盖几何（宽度 / 高度 / 换行），不覆盖视觉与交互手感。
  改动要**重启应用**才生效，`artifacts/package/探索词典/` 的打包副本也**尚未同步**。

---

## 16. 本轮续（主界面「删除后右栏残留」）结果

**用户报告（原话）**：「为什么左侧删除了右侧还有残留？」（附主界面截图：左栏「主题」
列表已经空了、中间是 `0 / 0 条（本页）` + 「这一页还没有词语」，右栏「词条详情」却还
完整显示着那条词的**词语 / 上下文 / 来源标题 / 来源链接 / 捕获元信息 / 释义全文**。）

**根因（三条，全在 `app/ui/main_window.py`）**

1. 左栏底部的「删除」绑的是 `delete_batch()`：它删主题、`refresh_batches()`、
   `refresh_entries()`，**从头到尾没有碰过详情右栏** —— 整个主题连同词条都没了，
   右栏还留着上一条词的五个字段（截图就是这条）。
2. 右栏自己的「删除词条」（`delete_selected()`）只把释义框 `exp_text` 清了，
   词语 / 上下文 / 来源 / meta **全留着**。
3. `select_entry()` 撞上「词条已不在库里」（异步解释回来时词条已被删）时只把
   `_selected_entry_id = None`，字段与卡片高亮一起留在屏幕上。

**修复**

* 新增 `MainWindow.clear_detail()`：一个**只碰控件、不写库不联网**的复位点 ——
  `_selected_entry_id = None`、`_highlight(-1)`（所有词卡回到未选中描边）、提示语改回
  模块常量 `EMPTY_DETAIL_HINT`（`"在左侧选择一条词条"`，与初始状态同一句）、
  `var_term` / `txt_context` / `var_src_title` / `var_src_url` / `var_meta` 清空、
  `exp_text` 清空并恢复 `state="disabled"`、最后 `update_explain_button()`
  （按钮文案按「没有选中」回到默认）。
* 三条路径全部收口到它：`delete_selected()` 用它替掉原来那三行只清释义框的代码；
  `select_entry()` 的 `row is None` 分支直接 `clear_detail(); return`；
  `refresh_entries()` 在写完计数文案之后加一道**按库内存在性**的守卫 ——
  `if self._selected_entry_id is not None and self.db.get_entry(...) is None: clear_detail()`
  ，于是「整个主题被删」这种不经过词条级代码的路径也自动复位。
* 守卫**故意**只问「这条词还在库里吗」，不问「它还在当前列表里吗」：搜索与手工浏览
  只换列表范围，右栏正在编辑的那条词并没有被删 ⇒ 一个像素都不许动（有专门的反例测试）。

**实测与回归**

* 新增 `tests/test_reading_panel.py` `TestDetailPaneClearsWhenItsEntryDisappears` 四项
  （真实 `MainWindow` + 假 Tk + 真实库）：删整个主题（左栏「删除」按钮 `invoke()`）、
  删单条（右栏 `delete_selected()`）、`select_entry()` 指向已删词条，
  三条都断言右栏**整块**为空（id / 提示语 / 五个字段 / 卡片描边），
  外加一条反例 `test_search_and_browsing_never_clear_a_live_entry`
  （搜索 + 换浏览范围后 `_selected_entry_id` 与**未保存的编辑**都还在）。单跑
  **Ran 4 OK**。
* **这些测试真的咬得住旧行为**：`_check/revert_check.py` 把 `MainWindow.clear_detail`
  打桩成空操作（= 修复前的行为）再跑同一组 —— 三条删除路径全部变红
  （`AssertionError: 1 is not None : 删除后不许再留着选中的词条 id`），
  反例那条仍然绿 ⇒ 测试是按行为写的，不是按实现细节凑的。
* **13 模块白名单回归**（与 §15 逐字同一条命令、同一组模块，日志
  `_check/tests_after_detailreset.log`）：**Ran 541 / failures=6** —— 总数
  = 上一轮基线 537 + 本轮 4 项新测试；6 条失败与基线**逐条同一批**
  （4 条在 `tests.test_unified_action`、2 条在 `tests.test_reading_panel`，
  都是本轮之前就有的过期断言），上一轮那条环境性的
  `test_root_is_withdrawn_before_build_app` 这次**通过**（本机实例此刻没在跑，
  `acquire_process_guard()` 不再返回 5）⇒ **没有引入新失败**。
* 单模块：`tests.test_reading_panel` **Ran 178 / failures=2**（= 上一轮 174 + 本轮 4）、
  `tests.test_ui_roundrect` **Ran 110 OK**（几何 / 预览一个字节没动）。
* 改动只碰 `app/ui/main_window.py` 与 `tests/test_reading_panel.py`，未动几何常量 /
  面板 / 离线预览；`compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* 离线预览与 `tools/ui_preview.py` **不需要**改：它画的是悬浮面板，主界面右栏的复位
  是运行时状态而非几何。

---

## 17. 本轮续（取词少最后一个字母 / 释义被裁成半句话）结果

**用户报告（两条，原话）**

1. 「为什么会出现这种不对应的情况？」（附 Edge 里 arXiv 论文截图：正文里 `Shifting`
   被整词选中，右侧悬浮面板显示的词语却是 **`shiftin`**。）
2. 「还有一个问题就是解释的展示不完整，话像没说完一样，你不用显示暂无对话这四个字。」
   （截图：详细解释断在「它表示模型性能的维持必须以遵守严格资」，下面一行占位字
   「暂无对话」，再下面是追问框。）

### A. 词语少最后一个字符 —— UIA 选区读出来就少一格

**取证**：`_check/term_offbyone_peek.py`（只读）拿库里最近 10 条词语，在各自的
`context` 里找「词语 + 一个字母」：**8 条命中** —— `shiftin`/`shifting`、
`adherenc`/`adherence`、`high computationa`/`computational`、`drift-detectio`/
`detection`、`model performanc`/`performance`、`data distribution`/`distributions`、
`resource constraint`/`constraints`、`resource-con-strained environmen`/`environment`；
两条完整的都是**带空格的短语**（`real-world environments`、`Machine learning (ML)
algorithms`）。⇒ 系统性少最后一个字符，而缺的那个字符正是 `context` 里紧跟其后的字母。

回显才算有内容）；`detail_area_heights()` 在没有对话内容时返回
`(max(theme.px(DETAIL_AREA_MIN), avail), 0)` —— 释义区拿走**全部**剩余高度（仍守
`DETAIL_AREA_MIN` 这条底线），对话区收到 0；有对话时照旧 55/45。`_render_chat()`
删掉 `if not lines: lines.append("暂无对话")`，并在写完文本后调一次
`_sync_detail_areas()`，使「第一句追问出现」时两块高度自动切回来。
### 实测与回归

* `tests/test_source.py` 新增 `TestSelectionTailCompletion` **10 条**（缺尾字母补齐、
  完整词不动、偏移 `None/-1/0/999/"abc"` 都不动、偏移错位不动、尾长 9 > limit 不动、
  尾长正好 8 补齐、不跨空格 / 不碰中文 / 词尾非字母数字不动、部分划选补到词尾，
  外加三条走 `attempt_capture()` 的真实链路：带偏移 ⇒ `shifting`、不带 ⇒ `shiftin`、
  偏移 0 ⇒ `shiftin`）。单跑 `tests.test_source` **Ran 42 OK**。
* **144 DPI（用户真实环境）**：原来 55/45 分时释义区只有 **166px**，现在 **311px**
  （+87%），对话区 0、无占位字；261 字解释此刻可见 84%（还差约一行半，需要滚一下 ——
  区域本身是可滚动的，右侧有滑轨）。
* 同一次实测还量了固定行：144 DPI 下详情页固定行**实测 162px**，而预算常量
  `theme.px(DETAIL_FIXED_H=138) = 207px`。差的 45px **故意留着**：词语换行成两行
  （+29）或出现「重试」行（+30）时固定行会吃到 221px，预算若按实测值收紧，pack 就会
  反过来裁掉释义区（正是本轮要修的病）。所以这里按「最坏情况」给预算，不按实测值。

  （仍是那 3 条过期断言，与上一轮同一批）；**13 模块白名单回归**（§15 逐字同命令，
  日志 `_check/tests_after_tailfix.log`）：**Ran 554 / failures=7** = 基线 6 条过期
  断言 + 1 条环境性的 `test_root_is_withdrawn_before_build_app`（截图时本机实例正在跑，
  `acquire_process_guard()` 返回 5）⇒ **没有引入新失败**。
* `tests.test_runtime_recovery` **Ran 27 OK**（含 `uia_helper.ps1` 必须**纯 ASCII**、
  候选预算与「先核对前台再读文字」顺序的静态检查）；helper 脚本经
  `Parser::ParseFile` 校验 **0 个语法错误**、非 ASCII 字节 **0**；直接按 stdio 协议
  喂 `{"cmd":"check"}` / `{"cmd":"quit"}` 冒烟：脚本正常加载并逐行应答
  （`expected_window_required` = 设计好的 fail-closed、`bye` 正常），说明改动没有
  破坏脚本运行；`compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **仍需一次实机取词确认**：`context_prefix` 是 helper 新增的回传字段，Python 侧的
  契约已由 10 条测试钉死，但「真实 Edge 选区 → helper 回传正确偏移 → 词语补齐」这条
  端到端链路只有用户下次取词才会走到（探针无法在不抢前台的前提下造出真实 TextPattern
  选区）。若补齐没生效，日志里会有 `选区尾部少字符，已补齐：…`（生效）或
  `选区尾部少字符但补齐后不合法，保持原样：…`（fail-closed），两条都不出现就是
  偏移对不上（= 与修复前行为一致，不会更糟）。
* 离线预览 `tools/ui_preview.py` **不需要**改：它画的详情页示例本来就带对话，
  空对话态不产生 PNG；几何常量一个字节没动。
* 改动要**重启应用**才生效（源码模式）；打包副本
  (`_runtime/scripts/uia_helper.ps1`、`artifacts/package/**`) 未同步。
**不是应用侧截的**：`capture_service.normalize_term()` 只折叠空白 + strip，
`_length_ok()` 只做 `min_len..max_len` 边界校验（默认 `max_len=80`），全仓唯一的
`text[:max_len]` 在**手工录入**路径。少字符发生在 UI Automation 读取侧
（Chromium / Edge 的 PDF 阅读器对 `TextPattern` 的 range 端点差一格）。

**修法（确定性，不靠猜）**：`scripts/uia_helper.ps1` 的 `Get-ContextText()` 现在把
「选区起点在 `context` 里的字符偏移」一并回传（`context_prefix`，由
`MoveEndpointByUnit` 的返回值减去被 `Trim()` 掉的前导空白得到；拿不到就是 `-1`）。
Python 侧新增纯函数
`capture_service.complete_word_tail(term, context, prefix, *, limit=WORD_TAIL_LIMIT)`：
只有在 ① `prefix` 是可用的非负整数、② `context.startswith(term, prefix)` 成立、
③ 词语末字符是 ASCII 字母数字、④ 向后补到**真正的词尾**（补满 `limit=8` 还在词里就
放弃）四条同时成立时，才把词补到词尾并 `log.info` 记一条补齐记录；任何一条不成立
⇒ 一个字都不补（与修复前行为完全一致）。

* 只补 **ASCII** 字母数字：中文没有词边界，多补一格就会吞掉下一个词。
* **不按手势 kind 区分**：划选到词语中间时同样补到词尾 —— 词典查的是词，不是半截
  字符串；上限 +「补到真正的边界」保证不会一路吞下去（测试里显式注明这是有意行为）。
* 被否的方案：盲补一个字母（对读全了的词就是错的）、纯 Python 启发式（同一窗口里词
  出现两次时只能 fail-closed，等于不修）。
* `context_prefix` 是**新增键**，旧 helper 不返回它 ⇒ 自动回退到原行为；`uia_bridge`
  原样透传 helper 的 JSON，无需改动。`_runtime/scripts/` 与 `artifacts/package/` 里的
  helper 是**打包副本**（源码模式用的是 `scripts/`），故意没有同步。

### B. 释义被裁成半句话 + 不要「暂无对话」

**根因**：`ReadingPanel.detail_area_heights()` 无条件按 `DETAIL_DEF_SHARE=0.55` 把剩余
高度切给「释义 / 追问」两块 —— 一句追问都没有时，空对话区照样占掉约 45%，释义区被压到
只剩几行，于是读起来「话没说完」；同时 `_render_chat()` 还给空对话区写一行占位字
「暂无对话」。**库里的解释是完整的**：`_check/entry_text_peek.py` 显示那条
`adherenc` 的 `detail` 有 201 字、结尾是「……（患者遵医嘱）区分，此处不是该义。」。

**修法**：新增 `ReadingPanel._chat_has_content()`（有正在回答、有历史轮次、或有本地
回显才算有内容）；`detail_area_heights()` 在没有对话内容时返回 `(max(2, avail), 0)` ——
释义区拿走**全部**剩余高度、对话区收到 0；有对话时照旧 55/45。`_render_chat()` 删掉
`if not lines: lines.append("暂无对话")`，并在写完文本后调一次 `_sync_detail_areas()`，
使「第一句追问出现」时两块高度自动切回来。

### 实测与回归

* `tests/test_source.py` 新增 `TestSelectionTailCompletion` **10 条**（缺尾字母补齐、
  完整词不动、偏移 `None/-1/0/999/"abc"` 都不动、偏移错位不动、尾长 9 > limit 不动、
  尾长正好 8 补齐、不跨空格 / 不碰中文 / 词尾非字母数字不动、部分划选补到词尾，
  外加三条走 `attempt_capture()` 的真实链路：带偏移 ⇒ `shifting`、不带 ⇒ `shiftin`、
  偏移 0 ⇒ `shiftin`）。单跑 `tests.test_source` **Ran 42 OK**。
* `tests/test_reading_panel.py` 的 `TestDetailAreasKeepVisibleHeight` 改成 4 条
  （空对话区把全部高度给释义 + 空对话区**一个字都不写** + 有对话时仍是 55/45 且两块
  都 ≥ `DETAIL_AREA_MIN` + 最小面板两块都活着）。单跑该类 **Ran 5 OK**。
* **这些测试真的咬得住旧行为**：`_check/revert_check2.py` 把 `complete_word_tail`
  打桩成恒等、把 `_chat_has_content` 打桩成恒真（= 修复前行为）再跑同一组 16 条：
  基线 16/0 → 打桩后 **16/3**，红的三条正是
  `test_capture_completes_the_term_from_the_helper_offset`、
  `test_empty_conversation_gives_the_whole_area_to_the_definition`、
  `test_empty_conversation_renders_no_placeholder`。
* `tests/test_reading_panel.py` + `tests/test_ui_roundrect.py`：**Ran 290 / failures=3**
  的两个名字是反的（`levels` 升序，前一个是**上行**），于是两行被传给了
  `_pair_crossings()` 的相反一侧、两次调用的 `links` 都取不到键 ⇒ `total()` **恒等于 0**、
  一个交换都不会发生。修好后同一 fixture 的层间交叉 12 → 4.5（内部按上下行各数一遍：
  24 → 9）。测试里专门加了一条「必须**严格下降**」的断言来钉死这个空转。

## 18. 本轮续（思维导图：布线规范 + 字号随缩放）结果

用户报告（原话）：

> 思维导图的布线过于杂乱，你需要规范布线排版规则。其次就是每个标签的字体大小应该是固定的，
> 我缩小视角后文字大小保持不变导致越出标签栏边界。

### A. 缩小时文字越出标签框 —— 字号没跟着缩放走

`_draw()` 里所有文字都用**固定字号**（`theme.font(9)` / `theme.font(10)` / `theme.font(7)`），
而节点框、坐标、间距全部来自 `layout_graph(..., zoom=self._zoom)` ⇒ 布局已经 ×0.6，
字还是 100%，于是两行英文直接把卡片边框顶破。

* `app/ui/theme.py` 新增 `font_px_at(pt, zoom)`（= `max(1, round(abs(device_px(pt)) * zoom))`）
  与 `font_at(pt, zoom)`；`_draw()` 的 7 处字号（空态 9 / 边标签 7 / 节点 9 / 主题 10 /
  孤立词标题 7 / 图例 8 / 提示 7）全部改为 `theme.font_at(pt, self._zoom)`。
  字号与同一缩放的 `metrics_for(zoom).em`（= `abs(device_px(9)) * 1.05 * zoom`）同源。
* 顺带修掉一个**真实的行高 bug**：`metrics_for()` 的行高原本按 `1.25em` 估算，而 Tk 的
  多行文字按字体 `linespace` 排行（实测约 1.45~1.62×字号），两行词语会被上下挤出卡片。
  现在 `line_h = widgets.line_height(9)`（真实 Tk 行距，96 DPI 下 19px 而不是 15px），
  盒高、车道间距、标签偏移随之等比放大。
* 离线复核（`_check/map_zoom_probe.py`）：`em / 1.05` 与 `abs(device_px(9)) * zoom` 在
  zoom = 0.6 / 0.75 / 1.0 / 1.5 / 2.0 全部吻合（7.20 / 9.00 / 12.00 / 18.00 / 24.00），
  卡框宽比正好 0.600 / 0.750 / 1.000 / 1.500 / 2.000；对照组（固定 12px）在 zoom=0.6 时
  墨迹 130px 而可用宽只有 87.3px —— 正是用户截图里的越界。

### B. 布线杂乱 —— 层间连线从「斜线扇出」改成正交车道

原来每条边都交给 `route_edges()` 的候选搜索（弧线 / 可见性绕行 / 直连），相邻两层的
层级 / 依赖边因此全是斜线扇出、互相穿插，读不出层次。现在加入**排版规范**（只在规范
本身干净时采用，脏了就退回候选搜索，绝不硬穿）：

1. **相邻两层的层级 / 依赖边走正交三段线**：源卡片底边出 → 空档里的水平车道 → 目标卡片
   顶边进（`_orthogonal_routes()`）；跨多层的边走「目标行上方」那条空档，垂直段撞到卡片
   就退回候选搜索；`对照` / `反馈` 保持弧线（视觉语言必须区分开）。
2. **端口分散**：同一张卡片同一侧的多条边按对端 x 排序后在卡宽内等距分端口，不再挤成一点。
3. **车道分道**：同一层间空档的水平段分道，居中排布；车道的上下次序按**起点 x 从右到左**
   排 —— 起点靠左的线放到下面的车道，它的竖直短段才不会穿过另一条线的水平段。
4. **主题母线**：`topic_links` 从「主题到每个词的斜线扇形」改成母线折线
   （主题底边 → 一条水平母线 → 各词顶边），类型也从 4 元组改成点列。
5. **层间交叉最小化**：重心法之后加一轮确定性下降（同层相邻两项只在交叉数**严格下降**时
   交换，平局不换 ⇒ 可复现）。
6. **不许退化成直连**：`对照` / `反馈` 的 2 点直连加 `1e3` 惩罚（只剩「弧线全被挡住」这
   一次机会）—— 实测反馈边弧线 22.0 vs 直连 18.8，只差 3px 就会退化成一条直线。
7. **同一对词之间的两条线不许重合**（会被读成一条粗线）：重合加 `1e5` 惩罚。
   选线代价的严格优先级：穿节点 `1e6` > 重合 `1e5` > 标签压叠 `1e4` > 退化直连 `1e3`
   > 标签位移 / 拐点（每点 2px）/ 先后次序。

实现过程中被测试抓出来的**两个真 bug**（都已修 + 已钉测试）：

* **反向依赖的箭头画反了**：源卡片在目标卡片下面时（例如「降采样 依赖 算子」），正交线
  仍按「上→下」生成点序，而 `arrow="last"` 把箭头画在**源**卡片上。修法：这类边生成后
  翻转点序（`plan["flipped"]`），保证点序永远从源到目标。
* **交叉最小化是空转**：`_reduce_crossings()` 里 `for lower, upper in zip(levels, levels[1:])`
  的两个名字是反的（`levels` 升序，前一个是**上行**），于是两行被传给了
  `_pair_crossings()` 的相反一侧、两次调用的 `links` 都取不到键 ⇒ `total()` **恒等于 0**、
  一个交换都不会发生。修好后同一 fixture 的层间交叉 12 → 4.5（内部按上下行各数一遍：
  24 → 9）。测试里专门加了一条「必须**严格下降**」的断言来钉死这个空转。

### 实测与回归

* 布线指标（`_check/routing_probe.py`，14 词 13 关系 + 1 个孤立词，画布 1700×560）：

  | | 边 | 层间交叉 | 拐点 | 轴向段边 | 穿节点 | 标签压节点 | 标签压标签 | 最深压叠 |
  |---|---|---|---|---|---|---|---|---|
  | 旧（候选搜索 / 无最小化） | 13 | 14 | 46 | 1 | 0 | 0 | 0 | -1.8 |
  | 新（正交 + 母线 + 最小化） | 13 | 14 | 44 | **7** | 0 | 0 | 0 | -1.8 |

  「轴向段边」= 含水平段的正交线（1 → 7），交叉数与旧持平、没有一条线穿过非端点卡片；
  视觉对比图 `_check/shots/routing-old.png` / `routing-new.png`（同一次布局的两种布线）。
* `tests/test_ui_roundrect.py` 新增 `TestMapRoutingRules` **8 条**：字号随每个缩放线性走
  （并断言行高不再是 1.25em 估算）、换行标签拿到「一行一真实行高」的框高、相邻层边的
  正交性质（2 点竖线 / 4 点 L 形、车道落在两行之间、点序从源到目标、不穿非端点节点）、
  同一源卡片端口分散、母线折线共线且接到第一行卡片顶边、zoom=2 时整个布局等比、
  交叉下降的单测、交叉最小化「只许减少不许增加 + 布局确定」。单跑
  `tests.test_ui_roundrect`：**Ran 118 OK**。
* 13 模块白名单回归（§15 逐字同命令，日志 `_check/tests_after_map.log`）：
  **Ran 562 / failures=7** = 6 条过期断言（`tests.test_unified_action` 4 条 +
  `tests.test_reading_panel` 的 2 条 topmost/dock） + 1 条环境性的
  `test_root_is_withdrawn_before_build_app`（`AssertionError: 5 != 0`，本机实例正在跑、
  `acquire_process_guard()` 返回 5）⇒ **没有引入新失败**；
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* 离线预览 `artifacts/ui-concept-preview.png` 已按新布线重画（母线折线 + 正交层级线 +
  向上的依赖箭头），按钮墨迹偏移 ≤ 1 设备像素。
* **仍未实机验收**：`_check/map_zoom_probe.py` 建窗那一档被 `app/gui_preflight.py` 挡下了
  —— 当时前台是全屏游戏（`aces.exe` War Thunder），预检不放行就一个窗口都不建。所以
  「真实 Tk 字体 + 真实缩放」下的观感只有用户实机能确认；离线 Pillow 档与几何常量已量。
* 改动要**重启应用**才生效（源码模式）；打包副本未同步。

## 19. 本轮续（思维导图：提示改版 + 去拥挤）结果

用户原话：

> 需要改进，红框圈的这两个不需要，提示内容：鼠标右键---平移  鼠标滚轮---缩放  点击连线查看依据 。
> 三行排开，上下保持间距，显示在窗口右下角。现在这个思维导图太拥挤了，我希望你参考一些
> 优秀的排布案例来进行改进。

### A. 两行说明跟着图一起跑 → 画布右下角三行浮层

* **改前**：`_draw()` 末尾把「AI 参考：点击连线看依据 · 细线＝主题包含（非 AI 判断）」与
  「右键拖动平移 · 滚轮缩放」画成**世界坐标**的画布图元（`(px(8), px(4))` 起、两行），
  于是右键平移一下、滚轮缩一下它们就跟着跑，还常年压在内容左上角；`_draw()` 开头还按
  `legend_h = theme.px(22)` 给它们预留了缩放的 `top_pad`。
* **改后**：常量 `HINT_LINES = ("鼠标右键---平移", "鼠标滚轮---缩放", "点击连线查看依据")` +
  `HINT_ROW_GAP = 6`（行距，设备像素）+ `HINT_INSET = 10`（离右下角的内缩）；窗口里建
  `self.hint_box = tk.Frame(self.canvas, bg=theme.PANEL)`，每行一个
  `tk.Label(fg=theme.TEXT_FAINT, font=theme.font(8), anchor="e", justify="right")`，
  逐行 `pack(side="top", pady=(0, 0 if last else theme.px(HINT_ROW_GAP)))`
  —— Tk 的多行文本没有行距参数，所以三行分开摆；最后
  `self.hint_box.place(relx=1.0, rely=1.0, anchor="se", x=-px(HINT_INSET), y=-px(HINT_INSET))`。
  它是**画布上的浮层**（不是画布图元），所以既不随平移/缩放移动，也不会被自适应缩放算进内容。
  左上角那两行图例与 `legend_h` 一并删掉，`top_pad` 只剩 `theme.px(6)`（真正的顶部留白由
  `_layout_core` 的 `max(m.pad, top_pad)` 给）。「AI 参考：连线为模型候选……」这句仍留在
  窗口底部状态行，不重复。

### B. 太拥挤（一）：先把留白比例调开

`MapMetrics` 默认值与 `metrics_for()` 同步上调（单位：设备像素）：`pad_x 11→14`、`pad_y 7→10`、
`min_w 64→72`、`max_w 196→182`、`min_h 34→42`、`h_gap 22→32`、`v_gap 40→56`、`pad 18→26`、
`topic_pad_x 16→20`、`topic_pad_y 10→12`、`group_pad 9→12`、`cross_bow 26→34`、`topic_gap 54→66`
（`line_h` 仍是 `widgets.line_height(9)` 的真实行高）。这套「卡内边距 < 卡间 `h_gap` < 层间 `v_gap`、
四周 `pad` / 母线 `topic_gap` / 分组 `group_pad` 同步放大」的口径写进了类 docstring。
既有布局测试都从 metrics 对象取期望值（`m.h_gap` / `m.v_gap`），所以抬高默认值本身不破测试。

### C. 太拥挤（二）：关系短标签垫上底色块

新增纯函数 `label_plate(pos, label, m)`（紧挨 `label_box`）：取标签避让框矩形，
`inset = max(1.0, (y1 - y0) * 0.22)`，返回 `(x0 - m.em * 0.12, y0 + inset, x1 + m.em * 0.12, y1 - inset)`
—— 与标签同中心、纵向收进避让框内（因此绝不会盖住别的标签的框）。`_draw()` 里每条边先画一块
`_kind="edge-label-plate"` 的圆角矩形（`fill=theme.PANEL`、不描边、半径 `theme.px(3)`）再画标签文字，
离线预览同步。效果：属于 / 依赖 / 用途 / 因果 都压在与画布同色的白块上，连线从白块下穿过，
不再从字上穿过去。

### D. 太拥挤（三）：试过「层内分带」，实测更糟，已回退

* **动机**：一层 6 张卡 ≈ 1252px，远超画布可用宽，首开自适应只能被 `AUTO_FIT_MIN = 0.75` 钳住。
  于是试着把太宽的一层按「均匀分带」横向拆成多行（每行仍等距、仍居中同一轴）。
* **实测**（14 词 / 13 关系、画布 662×520，`_check/crowding_probe.py`）：

  | | 内容尺寸 | 长宽比 | 行数（每行词数） | 最宽行 | 未钳制的首开缩放 |
  |---|---|---|---|---|---|
  | 分带（否决） | 661×868 | 0.76 | 6 | 585 | 0.594 |
  | 一层一行（现方案） | 1664×558 | 2.99 | 3（8/4/1） | 1612（= 2.64 × 可用宽 610） | 0.75（被 `AUTO_FIT_MIN` 钳住） |

  分带把同一层的词拆到多行之后，跨带的父子边**没法走正交车道**（车道的键是行号，
  带与带互为相邻行，竖线会穿过中间的带），只能退回候选搜索，画面反而多出一堆斜穿全图的线
  （`_check/shots/crowding-new.png` 对比 `crowding-old.png`）。
* **结论**：一层一行 + 横向平移。宽度收不住时宁可让用户拖动，也不拆散一层的拓扑。
  代码里 `bands = {lvl: [order_map[lvl]] for lvl in levels}` 留了注释指向这两张对照图。
  保留下来的两个纯函数与一个字段仍有用：`_fit_columns()` / `_balanced_rows()` 给孤立词网格
  （14 个词、一行放 4 个 ⇒ 4/4/3/3，不再出现 4/4/4/2 的秃尾行）、`LayoutNode.row` 与
  `_orthogonal_routes()` 以**行号**为键（`level + 1` 不保证相邻，`row + 1` 一定相邻）。

### 实测与回归

* `tests/test_ui_roundrect.py` 新增 `TestMapCrowdingRules` **4 条**（底色块落在避让框内且同中心、
  孤立词不出现秃尾行、`_fit_columns` 按可用宽算且画布比卡还窄也给一列、一层绝不拆行）+
  `TestConceptMapWindow` 新增 1 条（每条关系标签都垫一块画布同色的底色块、且画在字之前）。
  单跑 `tests.test_ui_roundrect`：**Ran 123 OK**。
* 13 模块白名单回归（§15 逐字同命令，日志 `_check/tests_after_crowding.log`）：
  **Ran 567 / failures=7** = 与 §18 完全同一批（`tests.test_unified_action` 4 条过期断言 +
  `tests.test_reading_panel` 的 2 条 topmost/dock + 1 条环境性的 `acquire_process_guard()`）⇒ 新增的
  5 条测试全绿、**没有引入新失败**；`compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* 离线预览 `artifacts/ui-concept-preview.png` 已重画：左上角两行说明不见了、右下角三行操作提示、
  每个关系标签下都有白底色块，按钮墨迹偏移 ≤ 1 设备像素。
* **仍未实机验收**（同 §18：建窗探针被 `app/gui_preflight.py` 挡下，前台是全屏游戏）：真实 Tk 下的
  观感、以及「一层太长要靠拖动」的实际手感只有用户实机能确认。改动要**重启应用**才生效（源码模式）；
  打包副本未同步。

## 20. 本轮续（设置页模型 / 推理强度 + 思维导图「孤立词」）结果

用户原话：

> 而且我发现了一个问题，就是这个接入的是deepseek的 API，但是没有显示所用模型和推理强度，所以这个是
> 需要加入作为调整选项的。其次就是我对于这个关键词的导图关系判定存在疑惑，明明原文中都是存在关系的，
> 为什么在绘制导图的时候缺有的变成了无关词语？

### A. 设置页：模型名 + 推理强度 + 一行「当前生效」

* **改前**：设置页只有 Base URL / 模型名（光秃秃的输入框）/ 超时 / API Key —— 界面上**任何地方**
  都看不出当前到底在用哪个模型、什么推理强度。
* **改后**（`app/config.py`）：
  * `DEFAULTS["api.reasoning_effort"] = ""`：空串 = 请求体里**根本不带** `reasoning_effort`
    这个键（默认最兼容，第三方网关不认这个字段也不会 400）。
  * `MODEL_PRESETS = ("deepseek-chat", "deepseek-reasoner", "deepseek-coder")` —— 设置页的
    **预设按钮**；模型名仍是**可手输**的输入框（自建网关 / 改名后的模型照样能用）。
  * `REASONING_EFFORTS = ("", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")`
    —— 这是**校验白名单**（老设置、手改过的配置、别家网关写的值都要能存住），
    **不是**设置页显示的档位；`effort_from_label()`（**文案与裸值都认**，认不出回 `""`：
    老设置文件里存的就是裸值，只认文案会把用户配置静默重置成「不发送」）。
  * **档位跟着模型名走**（用户指出：档位该按接入的 LLM 定，DeepSeek 只有 low / high / max）：
    `EFFORT_TIERS_BY_MODEL = (("deepseek", ("", "low", "high", "max")),)` +
    `EFFORT_TIERS_FALLBACK = ("", "low", "medium", "high")` + `effort_choices(model)` ——
    模型名里含 `deepseek`（大小写不敏感）就给三档，其它模型（OpenAI 风格网关）给通用三档，
    认不出来也给通用档位。依据 [DeepSeek《思考模式》文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)：
    思考模式默认打开、`reasoning_effort` 取值就是 `low / high / max`
    （`minimal→low`、`medium→high`、`xhigh→high`、`ultra→max`），
    `think` 的开关是 `{"thinking": {"type": "enabled/disabled"}}`。
    `REASONING_LABELS` / `REASONING_SHORT` 覆盖全部 8 个值（含 `max`），
    `Config.reasoning_effort` 不过滤模型（模型名是自由文本，过滤只会丢配置）。
  * `Config.reasoning_effort` / `set_reasoning_effort()` / `model_display`（`deepseek-chat · 推理强度 high`
    或 `deepseek-chat · 推理强度不发送`）。
  * `model_signature(model, effort)` = `f"{model}|{effort}"`；**空强度时与旧口径逐字一致**
    （`model` 原样返回）⇒ 老解释缓存 / 老导图指纹不会因为这次改动失效。
* **界面**（`app/ui/settings_dialog.py`，268 → 316 → 360 → **394** 行，**纯 tk 控件**）：模型名 = 可手输
  `tk.Entry` + 一行三个预设 `widgets.FlatButton`（点了只把名字填进输入框）；推理强度 =
  **按模型名现画的** `tk.Radiobutton` 组（绑**裸值**，DeepSeek 是 `""`/`low`/`high`/`max`，
  别的模型是 `""`/`low`/`medium`/`high`）＋ 一行说明显示当前档的完整解释；
  模型名一改（`var_model.trace_add("write", …)`，预设按钮与手输都走这条）就调
  `_rebuild_efforts()` 重画按钮：**原来那一档在新模型里不存在就退回「不发送」**，
  并在窗口内联反馈里说清哪一档被丢了（`medium` → DeepSeek 时），绝不静默发一个模型不认的值；
  grid 行序 0 Base URL / 1 模型名 / 2 预设 / 3 推理强度 / 4 说明 / 5 超时 / 6 API Key；
  grid 下方是 `「当前生效：…」` 行（`_sync_effective()` 读 `cfg.model_display`，保存后立刻刷新）
  与一段灰字说明（模型名可手输、不发送最兼容、改模型或强度后旧缓存与关系图不会命中）。
  `diagnostics_text()` 多一行「当前生效」；`test_connection()` 反馈带上强度文案；
  `_center()` 的 `except` 扩成 `(tk.TclError, AttributeError)`（无窗口测试里的假 master 没有
  `winfo_rootx`，不该因此炸掉窗口构造）。
* **贯通到请求**（`app/api_client.py`）：新增模块级 `_with_reasoning(payload, effort)` —— 只在
  **非空**时注入 `payload["reasoning_effort"]`；`build_payload` / `build_map_payload` /
  `build_map_verify_payload` 三个构造器与 `DeepSeekClient.explain / chat / map_relations / map_verify`
  全部带上它（`__init__(..., reasoning_effort="")` 归一 `strip().lower()`）。
* **贯通到缓存**：`ExplainSnapshot.reasoning_effort` 进**缓存键**（`make_cache_key` 用
  `model_signature`；落库仍是纯模型名）与解释窗底部那行 `model_config`
  （`url|model（推理强度 high）`，**空强度时逐字不变** ⇒ 既有断言与老日志照旧）；
  `map_service.content_fingerprint(..., reasoning_effort=)` 把签名写进指纹 payload。
  **顺带修掉一个真 bug**：`MapService.generate()` 算 `fp` 时原来漏传 `reasoning_effort`
  —— 一设强度，生成结果就会被 `_current_fingerprint()` 判成过期丢掉。

### B. 导图「孤立词」：不是材料里没有，而是**类型 / 方向标错**被第二道闸门否掉

* **现象复现**：主题 12「资源受限计算」10 个词，缓存行 `model='deepseek-chat'`、`raw=1090B`
  ⇒ 模型提了 **7 条候选**，最终只画了 **3 条**，另外 4 个词成了孤立词。
* **第一次排查**（`_check/map_replay_probe.py`：只读打开真库 + 跑产品函数）：7 条候选过
  `validate_relations` 是 **7/7 全过、`dropped` 全 0**；每条证据在材料里的命中位置都在规范化偏移
  **47–103**、长度 248–273 字 ⇒ **「240 字截断把证据挡住了」的假设被否掉**，也不是本地证据校验筛的。
* **第二次核对重放**（`_check/map_verify_probe.py`：**花一次真实 API 调用**，只用
  `MapService.verify_payload()` + `make_client()`，不写库、不起窗口）：核对材料 7669 字，返回
  **`supported=4 / uncertain=0 / contradicted=3 / bad=0`**（库里那次是留 3 条、这次留 4 条）：

  | 候选（原文里确实都有这句话） | 核对给出的判定与理由 |
  |---|---|
  | Machine learning (ML) algorithms --依赖--> real-world environments | **contradicted**：「材料说的是ML算法部署在真实环境中面临挑战，并未表达算法依赖真实环境作为前提。」 |
  | shiftin --依赖--> drift-detectio | **contradicted**：「材料说现有方案依赖漂移检测方法，方向是解决方案依赖漂移检测，而非shifting依赖漂移检测。」 |
  | high computationa --因果--> resource-con-strained environmen | **contradicted**：「材料说高计算开销是漂移检测方法在资源受限环境中的问题，并非高计算开销导致资源受限环境。」 |

* **结论三条**：① 杀掉这些边的**只可能是第二次独立核对**（`verify_bad=0` 排除「缺判定」，
  `apply_verdicts` 只有 `supported` 才画）；② 判定是 **contradicted 而不是 uncertain**
  ⇒ 第二道闸门**行为正确**，问题在**提出阶段的类型 / 方向精度**（把「A 在 B 中 / A 对 B 而言是个问题」
  这类场景描述写成依赖 / 因果，把「方案依赖漂移检测方法」挂到别的词上）；③ **不可复现**：同一份输入、
  `temperature=0.0`，库里那次留 3 条、重放留 4 条 ⇒ 孤立词集合有一部分是运气。
* **三处改法**：
  1. **提示词精度**（`app/api_client.py`）：`MAP_SYSTEM_PROMPT` 规则 2 之后加自检段与四条反例
     ——「A 在 B 中 / A 部署在 B 中 / A 出现在 B 里」是场景描述、「A 在 B 里的开销高 / A 对 B 而言
     是个问题」、「现有方案依赖 A」的前件不在词条里就不要挂到别的词上、同属一个话题总是结伴出现
     —— 都不是这六种关系；并写明「类型拿不准时**宁可不输出**：类型标错的候选会被核对判为不成立，
     反而让这个词变成孤立词」。`MAP_VERIFY_SYSTEM_PROMPT` 的 `contradicted` 分支把这两种常见情况
     写清（「场景描述」与「方向反了」都判 contradicted）。规则 5 一字未动，
     `tests/test_api.py:226/:240` 的既有断言继续成立。
  2. **判定落库**（`app/db.py` schema v5→**v6** + `app/map_service.py`）：`map_graphs` 新增
     `dropped TEXT NOT NULL DEFAULT ''` 与 `verdicts TEXT NOT NULL DEFAULT ''`（老库自动
     `ALTER TABLE` 补两列、幂等）；新增纯函数 `verdict_records(relations, nodes, verdicts)` ——
     与 `apply_verdicts` **同一套判据**（越界 / 非整数 / 非白名单 / 同序号重复都算「没给判定」）
     逐条回填 `{index, src, dst, type, reason, evidence, verdict, note, kept}`；`MapGraph` 新增
     `verdicts` 字段与 `verdicts_for(entry_id)`；`cached_graph()` 把 `dropped` / `verdicts` **读回来**
     （顺手修掉「二次打开显示已筛除 0 条」）。判定不落库，就永远只能靠上面那次重放猜。
  3. **界面点词看原因**（`app/ui/concept_map.py`）：`MapLayout.node_at()`（含中心词，多个命中取面积
     最小）+ `NODE_HIT_PAD = 2`，左键改成**先词后线**；`_node_text()`：已画出的关系逐条列
     「源 --类型--> 目标」并提示点连线看依据，孤立词则用 `verdicts_for()` 列「候选 + 核对判定 + 理由」，
     查不到记录时按「有没有记录」分三种说法（见第 4 条，第一版在撒谎）；`ISOLATED_TEXT` 改成
     「孤立词（候选关系都没通过核对，点词看原因）」；窗口状态行加 `_model_note()`
     （显示当前模型与推理强度，读不到 `config` 就退回缓存里的 `base|model`）。
  4. **点词没有记录时的三种说法**：判定记录是这一轮才落库的，**v6 之前的旧缓存里一个字都没有**
     —— 第一版在这里说了假话（对旧缓存也说「模型没有为这个词提出任何候选关系」＝凭空归罪模型）。
     现在按「有没有记录」分三支：`ISOLATED_WHY`（图里有该词的候选记录 ⇒ 逐条列候选与判定）、
     `ISOLATED_NONE` = 「没有这个词的候选记录：模型可能没为它提候选，也可能提的候选在证据 / 格式
     校验那一关就被筛掉了（那些不会进入核对）」、`ISOLATED_UNKNOWN` = 「这张图没有留下判定记录
     （旧缓存，或这次生成时模型一条候选都没提）：点「重新生成」按当前设置再问一次，就能逐条看到
     候选与核对结果」。两条测试钉住：旧缓存**不许**出现 `ISOLATED_NONE` / `ISOLATED_WHY`；
     图里有别人的记录、偏偏没有这个词的，要说「没有这个词的候选记录」（也不许说成旧缓存）。

### C. 「先词后线」带来的测试口径变化

折线的**端点**常常正好贴在下游卡片的边线上，而 `node_at` 带 2px 外扩 ⇒ 老写法
`edge.points[len(edge.points) // 2]` 在这些边上取到的其实是**端点**，按新规则算「点词」。
  另有一条钉住设置页用的是 `tk.Radiobutton` + `model_entry`、结果窗用的是 `thin_scrollbar`。
* **实测**：`探索词典.exe --diagnose`（**不建窗、不占互斥体**，`tests/test_desktop_packaging.py`
  有覆盖）现在走到 `import app.main: ok` → `STATUS OK (no window created, no hook installed)`
  → `DONE exit=0`。**这就是这次修复的直接验收**（同一份 frozen 运行时、同一份磁盘源码，
  崩之前是 `exit=4`）。

### 实测与回归

* 新增测试 20 条：`tests/test_api.py` `TestReasoningEffortIsOptIn` **4 条**（三个构造器默认都不发
  `reasoning_effort` / 传入时原样带上且 `response_format`·`stream` 不变 / `""`·空白·`None` 一律不发 /
  `DeepSeekClient(" High ")` 归一成 `high` 且 `explain` 的请求体带键 —— 打桩的 `_post` 必须返回
  **JSON 字符串**，返回 dict 会炸解析）；`tests/test_db.py` `TestMapVerdictsArePersisted` **4 条**
  （往返只留非 0 计数、同键重写整体替换旧判定、老调用方式读回 `{}`·`[]`、v5 假库迁移补两列 + 幂等）；
  `tests/test_ui_roundrect.py` **10 条**（`TestMapServiceWithFakeClient` 判定与缓存还原 1 条、
  `TestClickingAWordExplainsItsRelations` 5 条 —— 含上面第 4 条那两条「不许撒谎」的、
  `TestSettingsDialogModelAndReasoningEffort` 4 条 —— 设置页第一次有了无窗口构造测试）、
  `tests/test_runtime_recovery.py` `TestShippedCodeRunsInTheFrozenRuntime` **2 条**（D 段的 ttk 护栏）。
* 单跑：`tests.test_api` **Ran 26 OK**、`tests.test_db` **Ran 18 OK**、`tests.test_ui_roundrect`
  **Ran 133 OK**（其中 `test_small_content_pans_freely_in_all_four_directions` 按 C 段改了点击点）、
  `tests.test_runtime_recovery tests.test_ui_roundrect tests.test_api` 合跑 **Ran 188 OK**。
* 白名单回归（§15 逐字同命令 + 新增的 `tests.test_runtime_recovery`，共 **14 模块**，日志 `_check/tests_after_ttk_fix.log`）：
  **Ran 614 / failures=6** = `tests.test_unified_action` 4 条过期断言 + `tests.test_reading_panel`
  的 2 条 topmost/dock，与 §18/§19/§20 完全同一批 ⇒ 新增测试全绿、**没有引入新失败**
  （上一次多出的第 7 条是环境性的 `acquire_process_guard()`，本次用户实例没在跑，所以只有 6 条）。
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* 离线预览重画（`artifacts/ui-concept-preview.png`；按钮墨迹偏移 ≤ 1 设备像素）。设置页**没有**离线
  预览，只有上面那条无窗口构造测试。
* **仍未实机验收**：真实 Tk 下设置页新控件的观感（预设按钮 / 单选组 / 说明行）与导图「点词看原因」
  的命中手感，只有用户实机能确认。schema 升到 v6、新增设置项 ⇒ 改动要**重启应用**才生效。
  exe 侧的导入链已用 `--diagnose` 验收（D 段），打包副本仍未重建。
* **老主题要「重新生成」才有判定记录**：v6 之前生成的图（例如库里的主题 #12，`verdicts` 列为空）
  点孤立词只会说「这张图没有留下判定记录 …… 点『重新生成』再问一次」—— 这是**如实**的说法，
  不是新 bug；重新生成一次（新指纹 / 旧库已自动补两列）之后就能逐条看到候选与核对理由。
> （本节此处约 2 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

  的命中手感，只有用户实机能确认。schema 升到 v6、新增设置项 ⇒ 改动要**重启应用**才生效。
  exe 侧的导入链已用 `--diagnose` 验收（D 段），打包副本仍未重建。
* **老主题要「重新生成」才有判定记录**：v6 之前生成的图（例如库里的主题 #12，`verdicts` 列为空）
  点孤立词只会说「这张图没有留下判定记录 …… 点『重新生成』再问一次」—— 这是**如实**的说法，
  不是新 bug；重新生成一次（新指纹 / 旧库已自动补两列）之后就能逐条看到候选与核对理由。
> （本节此处约 1 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

  另有一条钉住设置页用的是 `tk.Radiobutton` + `model_entry`、结果窗用的是 `thin_scrollbar`。
* **实测**：`探索词典.exe --diagnose`（**不建窗、不占互斥体**，`tests/test_desktop_packaging.py`
  有覆盖）现在走到 `import app.main: ok` → `STATUS OK (no window created, no hook installed)`
  → `DONE exit=0`。**这就是这次修复的直接验收**（同一份 frozen 运行时、同一份磁盘源码，
  崩之前是 `exit=4`）。

### 实测与回归

* 新增测试 20 条：`tests/test_api.py` `TestReasoningEffortIsOptIn` **4 条**（三个构造器默认都不发
  `reasoning_effort` / 传入时原样带上且 `response_format`·`stream` 不变 / `""`·空白·`None` 一律不发 /
  `DeepSeekClient(" High ")` 归一成 `high` 且 `explain` 的请求体带键 —— 打桩的 `_post` 必须返回
  **JSON 字符串**，返回 dict 会炸解析）；`tests/test_db.py` `TestMapVerdictsArePersisted` **4 条**
  （往返只留非 0 计数、同键重写整体替换旧判定、老调用方式读回 `{}`·`[]`、v5 假库迁移补两列 + 幂等）；
  `tests/test_ui_roundrect.py` **10 条**（`TestMapServiceWithFakeClient` 判定与缓存还原 1 条、
  `TestClickingAWordExplainsItsRelations` 5 条 —— 含上面第 4 条那两条「不许撒谎」的、
  `TestSettingsDialogModelAndReasoningEffort` 4 条 —— 设置页第一次有了无窗口构造测试）、
  `tests/test_runtime_recovery.py` `TestShippedCodeRunsInTheFrozenRuntime` **2 条**（D 段的 ttk 护栏）。
* 单跑：`tests.test_api` **Ran 26 OK**、`tests.test_db` **Ran 18 OK**、`tests.test_ui_roundrect`
  **Ran 133 OK**（其中 `test_small_content_pans_freely_in_all_four_directions` 按 C 段改了点击点）、
  `tests.test_runtime_recovery tests.test_ui_roundrect tests.test_api` 合跑 **Ran 188 OK**。
* 白名单回归（§15 逐字同命令 + 新增的 `tests.test_runtime_recovery`，共 **14 模块**，日志 `_check/tests_after_ttk_fix.log`）：
  **Ran 614 / failures=6** = `tests.test_unified_action` 4 条过期断言 + `tests.test_reading_panel`
  的 2 条 topmost/dock，与 §18/§19/§20 完全同一批 ⇒ 新增测试全绿、**没有引入新失败**
  （上一次多出的第 7 条是环境性的 `acquire_process_guard()`，本次用户实例没在跑，所以只有 6 条）。
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* 离线预览重画（`artifacts/ui-concept-preview.png`；按钮墨迹偏移 ≤ 1 设备像素）。设置页**没有**离线
  预览，只有上面那条无窗口构造测试。
* **仍未实机验收**：真实 Tk 下设置页新控件的观感（预设按钮 / 单选组 / 说明行）与导图「点词看原因」
  的命中手感，只有用户实机能确认。schema 升到 v6、新增设置项 ⇒ 改动要**重启应用**才生效。
  exe 侧的导入链已用 `--diagnose` 验收（D 段），打包副本仍未重建。
* **老主题要「重新生成」才有判定记录**：v6 之前生成的图（例如库里的主题 #12，`verdicts` 列为空）
  点孤立词只会说「这张图没有留下判定记录 …… 点『重新生成』再问一次」—— 这是**如实**的说法，
  不是新 bug；重新生成一次（新指纹 / 旧库已自动补两列）之后就能逐条看到候选与核对理由。
> （本节此处约 2 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

  的命中手感，只有用户实机能确认。schema 升到 v6、新增设置项 ⇒ 改动要**重启应用**才生效。
  exe 侧的导入链已用 `--diagnose` 验收（D 段），打包副本仍未重建。
* **老主题要「重新生成」才有判定记录**：v6 之前生成的图（例如库里的主题 #12，`verdicts` 列为空）
  点孤立词只会说「这张图没有留下判定记录 …… 点『重新生成』再问一次」—— 这是**如实**的说法，
  不是新 bug；重新生成一次（新指纹 / 旧库已自动补两列）之后就能逐条看到候选与核对理由。
> （本节此处约 13 行内容在 2026-10-03 的 README 误覆盖事故中丢失，磁盘与会话快照都没有留存。）

### D. 实测（修复后，走产品完整管线）

`_check/carry_debug.py`（先把真库 `sqlite3` backup 成副本 `_check/_carry_debug/copy.sqlite3`，
所有写入都落在副本上；主题 #12、`deepseek-chat`、`map_thinking='disabled'`、
指纹 `89b473f6883d…`）连续 **3 轮** `generate(force=True)`：

* 每轮都是 `2 条参考关系（沿用上次 1 条）`，关系集合三轮**完全相同** =
  `{26 --属于--> 25, 30 --因果--> 31}`；第 2 / 3 轮模型多提的候选被判 `uncertain`，
  画面上没有任何变化（`_check/map_stability_after.py` 是同一实验的两轮版本）。
* **修复前**同一条命令：第 1 轮 2 条、第 2 轮 1 条、`第二次沿用上次：0 条`、
  `两次关系集合完全相同：False` —— 因为**被这轮核对改判**的那条边当时会被抹掉
  （C 段第 ② 条路径就是为此加的）。
* `_check/stability_smoke.py`（纯函数 + 配置，不联网、不写库）：默认请求体里 `thinking`
  不出现、`temperature = 0.0`；`map_thinking` 对 `deepseek-chat` / `gpt-4o` / `qwen-max` /
  `always` / `never` 的取值全对；证据不在材料里、端点已删 ⇒ 沿用 0 条。

### 实测与回归

* 新增测试 6 条（都在 `tests/test_ui_roundrect.py`）：
  `TestMapServiceWithFakeClient.test_regenerating_the_same_material_keeps_the_edges_it_already_drew`
  （第二轮模型漏提一条 ⇒ 图上仍是 2 条、`carried == 1`、`"沿用上次 1 条" in summary()`、
  沿用的边**不重问核对**（`len(verify_calls[0]["items"]) == 1`）、判定记录里有一行 note 含
  「沿用上次」且 `kept` 为真）；`TestMapDeterminismAndCarryOver` **5 条**（两个构造器默认
  不带 `thinking` 且 `temperature=0.0`、`"disabled"/" DISABLED "/"Disabled"` 都注入成
  `{"type": "disabled"}`、`DeepSeekClient` 真的把它发进请求体、`Config.map_thinking` 跟着
  网关走、沿用只补仍然成立的、沿用绝不动到现有边、**又提了但被改判 ⇒ 以上次为准**）。
* 单跑：`tests.test_api tests.test_db` **Ran 44 OK**；`tests.test_ui_roundrect` **Ran 141 OK**；
  `tests.test_api tests.test_db tests.test_ui_roundrect tests.test_runtime_recovery` 合跑
  **Ran 214 OK**。
* 白名单回归（14 模块，与 §20 逐字同命令，日志 `_check/tests_after_map_stability.log`）：
  **Ran 622 / failures=7** —— 7 条全是 §18–§20 的同一批（`tests.test_unified_action` 4 条 +
  `tests.test_reading_panel` 2 条 + 环境性的
  `TestLaunchHidesMainWindowFirst.test_root_is_withdrawn_before_build_app`，用户实例在跑时
  `acquire_process_guard()` 返回 5）⇒ 新增测试全绿、没有引入新失败。
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **仍未实机验收**：真实 Tk 下「连点两次『重新生成』图不变」的观感（源码模式重启后即可试；
  关掉思考模式后提出阶段更快）。老主题第一次「重新生成」时也会沿用旧缓存里已画出的边
  （这正是想要的效果），但**逐条判定记录要这次生成之后才有**。


## 21. 本轮续（思维导图「每次重新生成都不一样」）结果

* 用户原话：「我发现思维导图每次重新生成都不一样，不要求每个词语必须有专属的评判依据，
  思维导图应该是直接确定好的正确的，而不是反复变化的。」
* 做法是**两道保障**：请求侧确定化（`temperature=0.0` + DeepSeek 系显式关掉思考模式）
  ＋ 结果侧「沿用上次」（同一内容指纹下，上次已核对通过的边只增不减）。

### A. 先量清楚：不稳的是**提出阶段**，而且思考模式开着时 `temperature` 根本不起作用

`_check/map_stability_probe.py`（只读真库 + 产品构造器 / 解析器，主题 #12 十个词、
`deepseek-chat` @ `api.deepseek.com`、`reasoning_effort=""`，每次调用 1.3–1.7s）：

| 段 | 设置 | 三次条数 | 三次完全一致 | 交集 / 并集 |
| --- | --- | --- | --- | --- |
| A | `temperature=0.2`（产品原默认） | 6 / 6 / 5 | 否 | 2 / 10 |
| B | `temperature=0.0` | 6 / 5 / 5 | 否 | 4 / 7 |
| C | **`thinking={"type":"disabled"}` + `temperature=0.0`** | 5 / 5（两次） | **是** | — |

* D 段（`map_verify` 固定输入三次）：判定**完全一致**（0–3 `contradicted`、4 `supported`、
  5 `contradicted`）⇒ 之前看到的「同一条边在不同代之间 `supported` / `contradicted` 翻转」
  不是核对本身随机，而是**候选集合每次都不一样**，而核对是**整批一起判**的：换一批邻居，
  同一条边的结论就会抖。
* 结合 DeepSeek 官方文档（`https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/`）：
  思考模式**默认打开**，而思考模式下 `temperature` **不生效**（传了不报错也不起作用）
  —— 这正是「设了 0 也没用」的原因；要让导图确定，必须显式 `{"thinking": {"type": "disabled"}}`。

### B. 请求侧：`api.map_thinking` + 两个导图调用 `temperature=0.0`

* `app/api_client.py`：新增模块级 `_with_thinking(payload, thinking)`（**非空才注入**
  `payload["thinking"] = {"type": mode}`，归一 `strip().lower()`）；`build_map_payload` /
  `build_map_verify_payload` 的 `temperature` 默认值由 0.2 改成 **0.0**；两个构造器与
  `DeepSeekClient.__init__ / map_relations / map_verify` 都支持 `thinking=`。
* `app/config.py`：新增设置项 `api.map_thinking`（默认 `"auto"`），`Config.map_thinking` 返回
  `"disabled"` 或 `""`：`always|1|true|yes|on` ⇒ 总是发；`never|0|false|no|off` ⇒ 从不发；
  其它 ⇒ 只在 `looks_like_deepseek(model, base_url)`（名字或地址里含 `deepseek`）时才发 ——
  **别的网关不认识这个字段，绝不乱发**。没有放进设置界面（改配置即可）。
* 解释 / 追问**没动**：它们是逐条问答、用户自己看得到每次的历史，不需要跨次一致，
  而思考模式对解释质量有帮助。

### C. 结果侧：沿用上次（`carry_over_relations`，纯函数）

* 只认**同一内容指纹**（材料没变、模型 / 强度没换；换了就一趟都不用，从头算，
  绝不把旧模型的结论拖过来），且要同时满足：端点还在本主题、本地证据校验
  （`relation_locally_valid`）照样通过、不与现有边构成层级矛盾或层级环
  （`carried_is_compatible` 直接复用 `_drop_direction_conflicts` / `_drop_layer_cycles`
  的判据，不另写一套规则）。
* **两条路径都堵**：① 这次没提 ⇒ 直接把边补回来（合成 `supported` 判定，落库、
  点词能看到「沿用上次核对通过的判定（同一份材料，这次没重新提）」）；
  ② 这次**又提了**、但这一轮核对改判不成立 ⇒ **以上次为准**
  （「……这次核对改判了，以上次为准」）—— 已经画出来的边不能因为「这次问出来不一样」就消失。
* `MapGraph.carried` 与 `summary()` 的 `（沿用上次 N 条）`：N 是**核对之后真正落在图上**的条数
  （`_worker` 拿 `carried_keys` 再数一遍，不吹牛）。

### D. 实测（修复后，走产品完整管线）

`_check/carry_debug.py`（先把真库 `sqlite3` backup 成副本 `_check/_carry_debug/copy.sqlite3`，
所有写入都落在副本上；主题 #12、`deepseek-chat`、`map_thinking='disabled'`、
指纹 `89b473f6883d…`）连续 **3 轮** `generate(force=True)`：

* 每轮都是 `2 条参考关系（沿用上次 1 条）`，关系集合三轮**完全相同** =
  `{26 --属于--> 25, 30 --因果--> 31}`；第 2 / 3 轮模型多提的候选被判 `uncertain`，
  画面上没有任何变化（`_check/map_stability_after.py` 是同一实验的两轮版本）。
* **修复前**同一条命令：第 1 轮 2 条、第 2 轮 1 条、`第二次沿用上次：0 条`、
  `两次关系集合完全相同：False` —— 因为**被这轮核对改判**的那条边当时会被抹掉
  （C 段第 ② 条路径就是为此加的）。
* `_check/stability_smoke.py`（纯函数 + 配置，不联网、不写库）：默认请求体里 `thinking`
  不出现、`temperature = 0.0`；`map_thinking` 对 `deepseek-chat` / `gpt-4o` / `qwen-max` /
  `always` / `never` 的取值全对；证据不在材料里、端点已删 ⇒ 沿用 0 条。

### 实测与回归

* 新增测试 6 条（都在 `tests/test_ui_roundrect.py`）：
  `TestMapServiceWithFakeClient.test_regenerating_the_same_material_keeps_the_edges_it_already_drew`
  （第二轮模型漏提一条 ⇒ 图上仍是 2 条、`carried == 1`、`"沿用上次 1 条" in summary()`、
  沿用的边**不重问核对**（`len(verify_calls[0]["items"]) == 1`）、判定记录里有一行 note 含
  「沿用上次」且 `kept` 为真）；`TestMapDeterminismAndCarryOver` **5 条**（两个构造器默认
  不带 `thinking` 且 `temperature=0.0`、`"disabled"/" DISABLED "/"Disabled"` 都注入成
  `{"type": "disabled"}`、`DeepSeekClient` 真的把它发进请求体、`Config.map_thinking` 跟着
  网关走、沿用只补仍然成立的、沿用绝不动到现有边、**又提了但被改判 ⇒ 以上次为准**）。
* 单跑：`tests.test_api tests.test_db` **Ran 44 OK**；`tests.test_ui_roundrect` **Ran 141 OK**；
  `tests.test_api tests.test_db tests.test_ui_roundrect tests.test_runtime_recovery` 合跑
  **Ran 214 OK**。
* 白名单回归（14 模块，与 §20 逐字同命令，日志 `_check/tests_after_map_stability.log`）：
  **Ran 622 / failures=7** —— 7 条全是 §18–§20 的同一批（`tests.test_unified_action` 4 条 +
  `tests.test_reading_panel` 2 条 + 环境性的
  `TestLaunchHidesMainWindowFirst.test_root_is_withdrawn_before_build_app`，用户实例在跑时
  `acquire_process_guard()` 返回 5）⇒ 新增测试全绿、没有引入新失败。
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **仍未实机验收**：真实 Tk 下「连点两次『重新生成』图不变」的观感（源码模式重启后即可试；
  关掉思考模式后提出阶段更快）。老主题第一次「重新生成」时也会沿用旧缓存里已画出的边
  （这正是想要的效果），但**逐条判定记录要这次生成之后才有**。

## 22. 本轮续（配置与上手：服务商预设 / 首次向导 / 真·测试连接）结果

用户给了一份改进意见（`改进清单.md` 的 A 批 P0，对应意见图 1 的三条）：
新用户要自己搞 API Key + Base URL，**大量潜在用户卡在配置阶段**。本批就治这一步，
底层逻辑一行没动：仍然由用户自备密钥，软件**不内置任何密钥**。

### A. 服务商一键预设（意见 1）

`app/config.py` 新增：

```python
PROVIDER_PRESETS: tuple[tuple[str, str, str], ...] = (
    ("DeepSeek 官方", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("硅基流动", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    ("通义千问（兼容模式）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("本地 Ollama", "http://127.0.0.1:11434/v1", "qwen2.5:7b"),
)
LOCAL_ENDPOINT: tuple[str, str] = ("http://127.0.0.1:11434/v1", "qwen2.5:7b")
```

* 设置页最上面新增 **「服务商」一行按钮**（`self.provider_buttons`）：点一下只把
  **Base URL + 模型名**填进编辑框（`_pick_provider`），**不落库、不联网、不碰 Key** ——
  用户仍然只粘自己的 Key，按「保存」才算数（反馈行会明说这一点）。
* 地址里**不许有任何密钥痕迹**（测试断言 `"key" not in base.lower()`）；
  「本地 Ollama」只是模板，不需要 Key，用户自己决定。
* 模板只填地址与模型名，**不改变底层请求逻辑**：模型名变了，下面「推理强度」的档位
  也跟着重画（`_rebuild_efforts`，见 §20）—— DeepSeek 系是 `不发送 / low / high / max`，
  其它家是 `不发送 / low / medium / high`。

### B. 首次配置向导（意见 2）

新文件 `app/ui/setup_wizard.py`（纯 tk，`widgets.BorderlessChrome`，**不发任何网络请求**）：

* 正文第一句就把误会拆掉：**「这不是『划词就出翻译』的词典。」** 接着说清
  「划选一个词只会弹出一个小浮条；**只有你点『解释并记录』，它才会**……」
  「它的价值在于把读到的术语连同当时的原文上下文攒起来」。
* 两个选择：**用云端 API（推荐）** / **用本地推理服务（Ollama）**；选哪个，
  「将使用：<地址> · <模型>」实时跟着变。云端 = 保持用户现有地址（为空才退默认端点），
  本地 = 写 `LOCAL_ENDPOINT`。按钮：**去填 API Key（打开设置）** / **先随便看看**。
* 「×」关闭 ==「先随便看看」：都写 `ui.wizard_done=1`（`DEFAULTS` 新增，默认 `"0"`），
  **永不再弹**；`should_show(cfg, *, gate_locked=False)` 是模块级纯函数（门控硬阻断 =
  游戏 / 全屏 / 暂停时**不弹**，等下次打开主界面再说）。
* **挂在哪里（这一条踩过坑）**：一开始直接放在 `open_main_window()` 末尾，回归立刻抓到
  5 条新失败 —— 设置页和「解释并记录」都要靠 `open_main_window()` 把父窗口亮出来，
  于是「点一次设置」会顺带弹出向导，`deiconify` 也从 1 次变 2 次。改成显式参数：

```python
def open_main_window(self, *, wizard: bool = False) -> None:   # 默认不弹
def show_on_startup(self) -> None:                             # 启动入口
    self.open_main_window(wizard=True)
```

  只有「启动 / 双击 exe / 用户显式呼出」那条路径传 `wizard=True`
  （`request_open_main` 与 `recall_ui`），设置页、手动录入后追解释、顶栏「主界面」
  一律走默认值。`open_setup_wizard()` **绝不调用** `open_main_window()`（会递归），
  也不再重复 `deiconify/lift` 主窗（调用方刚做过）。

### C. 真·「测试连接」（意见 3）

原「检查配置」只做本地格式检查、**从不联网**，用户根本看不出配置通不通（这一点连
《软件说明》都只能照实写）。所以两个按钮明确分工，docstring 与界面灰字都写清：

| 按钮 | 做什么 | 联网 |
| --- | --- | --- |
| 检查配置 | 本地格式检查（地址 / 模型名 / Key 有没有填） | 否 |
| **测试连接** | 发一条极轻量请求，直接给成功或**报错原因** | 是 |

* `DeepSeekClient.ping()` 的请求体**恰好四个键**：
  `{"model", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8, "stream": False}`
  —— 刻意**不带** `temperature` / `reasoning_effort` / `thinking` / `response_format`：
  只验证「地址 + Key + 网络」，不掺任何推理参数（免得某家网关因为不认识的字段直接 400）。
* 模块级 `ping_endpoint(*, base_url, model, api_key, timeout=12.0) -> tuple[bool, str]`
  **永不抛异常**：`ApiError` 直接转成「连接失败：…」字符串，其它异常走 `redact()` 截断。
  401/403、429、400、≥500、超时、连不上主机的脱敏文案全部复用 `_post` 既有那条链。
* 界面上：没 Key / 地址不带 `http(s)://` / 模型名为空 ⇒ **一个请求都不发**，直接给原因；
  在途时按钮禁用（`set_enabled(False)`）并提示「正在测试连接，请稍候…」；超时上限
  15 秒（`min(用户超时, 15)`）；回来后在反馈行写「✓ / ✗ …」。线程结果用
  `self.win.after(0, …)` 回主线程，假 Tk 没有 `after` 就同步调用（回归里跑得通）。
* 可以先粘 Key 再点「测试连接」**先测再保存**：`_connection_targets()` 优先用编辑框里
  刚粘的 Key，没有才用已存的那把。

### 实测与回归

* 新增测试 **19 条**：`tests/test_api.py` 的 `TestPingConnection` **6 条**（请求体键集合恰好
  四个、带 `reasoning_effort`/`thinking` 也不许混进去、服务端换了模型名要报出来、
  真 `FakeApiServer` 上的成功与 401、连不上主机不许抛、空 Key 拒发）；
  `tests/test_ui_roundrect.py` 的 `TestProviderPresetsAndLiveConnection` **6 条** +
  `TestSetupWizard` **7 条**（预设合法且无密钥痕迹、点「硅基流动」只改编辑框不改 cfg、
  无 Key 不发请求、先粘 Key 再测能走通、坏地址 / 空模型名的反馈、成功与失败写进反馈行、
  在途禁用按钮、`should_show` 四态、向导文案含「不是『划词就出翻译』的词典」、
  选本地写 Ollama、`finish` 后不再弹、`×` 也算看过、
  **亮主窗不许顺带弹向导（默认路径）/ `wizard=True` 才弹**）。
* 单跑：`tests.test_api` **Ran 32 OK**（26 → +6）；`tests.test_ui_roundrect`
  **Ran 154 OK**（141 → +13）。
* 白名单回归（14 模块，同一命令，日志 `_check/tests_after_batch_a2.log`）：
  **Ran 641 / failures=6** —— 6 条全是 §18–§21 的同一批（`tests.test_reading_panel` 2 条 +
  `tests.test_unified_action` 4 条），**没有一条新失败**；
  中途那一版（向导挂在 `open_main_window()` 里）是 **Ran 640 / failures=11**
  （日志 `_check/tests_after_batch_a.log`，多出来的 5 条正是上面 B 段那个坑）。
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **仍未实机验收**：真实 Tk 下新增的两行按钮（7 个服务商 + 测试连接）在 144 DPI 的排版、
  向导窗的实际观感与居中、以及「点服务商 → 档位跟着模型重画」的即时性。
  源码模式重启 `启动.cmd` 即可试；**exe 需要重新打包**（`tools\build_app.ps1`）才会带上
  新模块 `app/ui/setup_wizard.py`（`bootstrap.py` 的 `_DIAGNOSE_MODULES` 已加它，
  但 `_runtime\bootstrap.py` 是刻意不同步的打包副本）。
* 下一批：清单里的 B 批（标签 / 主题合并 / 导出 / 二次上下文 / 搜索 / 来源补全）。

## 23. 本轮续（词库组织：标签 / 合并·拆分 / 导出 / 二次上下文 / 搜索 / 来源补全）结果

用户审核过的改进意见里，**B 批（意见图 2 的六条）治的是「词攒下来之后没法用」**：
一条词只能待在一个主题里、上下文只有第一处、想批量带走只能一条条复制。
本批**不做重型笔记、不做内置闪卡**，只做轻量组织 + 数据出口（这是意见里明确的边界）。

### A. 数据层（`app/db.py`，SCHEMA_VERSION 6 → 7）

```sql
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
    PRIMARY KEY (entry_id, tag_id)
);
```

* 建表走既有 `_MIGRATIONS` / `_COLUMN_MIGRATIONS`（**幂等**，老库打开即升级，
  并把 `settings.schema_version` 写成 7）；老版本号的 `map_graphs` 行照样不命中缓存。
* 新方法（都在 `Database` 上，测试逐条点名）：
  `normalize_tags(text) -> list[str]`（逗号 / 顿号 / 斜杠 / 分号 / 空白都算分隔，
  去重不分大小写、单个标签最多 24 字、最多 12 个）、`set_entry_tags(entry_id, names)`
  （**整体替换**）、`tags_for(entry_id)`、`tags_for_entries(ids) -> dict[int, list[str]]`
  （批量，卡片一次查完）、`list_tags() -> [{'name', 'entry_count'}]`、`rename_tag(old, new)`
  （改成已有名字 = **并成一条**）、`delete_tag(name)`（**只解标签，不删词条**）、
  `merge_batches(target_id, source_ids) -> int`、`move_entries(ids, batch_id) -> int`、
  `find_entry_by_term(batch_id, term, doc_key)`、`append_entry_context(entry_id, piece, *, captured_at, invalidate_explanation=True)`。
* `list_entries(...)` / `count_entries(...)` 增加 `tag=` 与 `query=` 两个入口：
  `tag` 是**跨主题**的精确匹配（`tag="经济学"` 命中，`tag="经济"` 不命中 —— 不做前缀猜词），
  `query` 会扫上下文 / 释义 / 例子 / 来源，**也扫标签名**。
* `merge_batches` 连源主题的 `map_graphs` 一起清掉（图谱是按主题生成的，主题没了就不该留）。

### B. 二次上下文（意见 4）

* `DEFAULTS` 新增 `capture.duplicate_action`（默认 `"new"`），设置页「取词与游戏」里多一行
  **「同一个概念再次被划到（上下文不同）：新建词条 / 追加上下文」**。`Config.duplicate_action`
  只有恰好 `"append"` 才走追加，**其它任何值都退回 `"new"`**（配置被手改坏不会静默改行为）。
* `capture_service.bookmark()` 在去重未命中之后、建新词条之前多一个分支：开着 append 且
  选中内容带上下文 ⇒ `find_entry_by_term()` 找到同一主题·同一文档里的同名旧词就**追加**，
  不新建；事件也从 `("bookmarked", {"created": True})` 变成
  `{"created": False, "appended": True, "context": …}`（浮条 / 状态行照旧有反馈）。
* `append_entry_context()` 的合并语义是**一整段一个语境**（分隔符 `CONTEXT_JOINER = " ／ "`）：
  完全重复 / 被已有段落包含 ⇒ 只 `repeat_count + 1`；**和已有段落部分重合就连起来就地升级**
  （`prev in piece` ⇒ `parts[i] = piece`），真正的新段落才追加；总长仍受 `MAX_CONTEXT_CHARS`
  兜住，且只在**整段落**边界上截断（宁可不要这半句，也不写半截内容）。
* 追加会让**旧解释失效**：`explain_status='ok'` ⇒ `'stale'`，卡片与列表都显示
  **「需重新解释」**，点开解释框先看到一行「（上下文已补充，建议重新解释）」。

### C. 标签与主题合并·拆分（意见 1、2）

* 左栏底部新增**标签区**（`tag_head` + `tag_list` + `标签改名 / 标签删除 / 全部`）：
  标签清单按名字排序并显示条数，点一个标签 ⇒ 列表切成**全库范围**、
  计数行追加 `· 标签「经济学」`；再点主题 = 结束标签筛选（否则「点了像没反应」）。
* 中栏每张词卡左侧多了**勾选框**，中栏标题右侧是 `已勾选 N 条`；勾选跨刷新保留，
  词条被删掉时自动清掉勾选（`_drop_missing_checks`）。
* 主题区按钮扩成 `重命名 / 删除 / 合并… / 拆分…`：
  「合并…」= `batch_dialogs.MergeBatchesDialog`（选目标主题 + 勾来源主题，
  来源主题消失、词条全搬过去，状态行写「已合并 N 条词语」）；
  「拆分…」= `MoveEntriesDialog`（把**勾选的词条**搬进新主题或另一个已有主题，
  新主题名前后空白会被压掉，搬完自动跟随过去）。
  **两个对话框都是纯 tk**（`tk.Radiobutton` / `tk.Checkbutton`，没有 `ttk`）。
* 详情面板新增**标签行**（输入框 + 保存时写库 + 已有标签的 `+标签` 芯片，
  点芯片只填输入框、**不落库**，还要按「保存修改」）；改标签会被 `save_detail` 一并写下去。

### D. 导出（意见 3）

新文件 `app/export_service.py`：**只写文件、不需要对话框**（冻结运行时没有
`tkinter.filedialog`，所以没有「另存为」）。落点 `app/paths.py` 的 `exports_dir()`
= `data/exports/`，界面导出后提示完整路径并问一句「打开文件夹？」，用 `os.startfile` 打开。

* 一次导出**同时给两份**：`探索词典-<范围>-YYYYmmdd-HHMMSS.csv`（`utf-8-sig` BOM，
  直接双击 Excel 不乱码，`\r\n` 换行）与同名 `.md`（Markdown 表格，`|` 转义）。
* 列固定十项：词语 / 上下文 / 来源标题 / 来源链接 / 来源应用 / 释义 / 例子 / 标签 /
  捕获时间 / 重复次数 —— 够导进 Anki、Obsidian 这类外部工具，**软件本身不做复习**。
* 文件名用范围命名（主题名 / 搜索词 / 标签 / 全部），非法字符剥掉、超长截断；
  **同名绝不覆盖**（第二次导出自动变 `…-2.csv`）。范围跟着当前界面走：
  浏览某主题 ⇒ 只导该主题；开着标签筛选或搜索词 ⇒ 导筛选结果（并告诉你是哪个范围）。
* 空范围也算成功（只写表头），脏数据（例如不是 JSON 的 examples）**原样导出**，
  不因为一条数据长得怪就整个导出失败。

### E. 搜索（意见 5）

新文件 `app/search_service.py`：`highlight()`（大小写不敏感、命中用 `【】` 包住）、
`snippet()`（带 `…` 的短片段）、`match_field()` / `describe()`。
字段顺序固定为 词语 → 上下文 → 释义 → 例子 → 来源 → 来源链接 → 来源应用 → **标签**，
所以卡片上的命中行会**明说是哪个字段命中**，例如
`命中 上下文：当消费者多消费一单位商品时，边际效用【递减】。`、
`命中 标签：#【微观】` —— 不用点开词条猜「它为什么被搜出来」。

### F. 来源补全（意见 6）

新文件 `app/source_enrich.py`（纯函数，**不新增任何 win32 / UIA 调用**）：
真正去抓标题这一半**早就有**（`capture_service.enrich_source_with_uia()` 会把 UIA 的
页面标题与真实 URL 补进来源），本批补的是「抓回来的字符串太脏」：

* `clean_document_title(raw, *, app="")` 剥掉浏览器的尾巴
  （`机器学习入门 - Google Chrome` ⇒ `机器学习入门`，
  `(12) 什么是边际效用？ - YouTube and 8 more pages - 个人 - Microsoft Edge`
  ⇒ `(12) 什么是边际效用？ - YouTube`）、阅读器的尾巴与文档扩展名
  （`第 3 章 概率论.pdf - Adobe Acrobat Reader` ⇒ `第 3 章 概率论`），
  多级标题里的正文段一律保留，洗空了才退回程序名。
* **只清洗「存进库里的那个字符串」**：`SourceInfo.title` 与 `doc_key` 一个字都不动
  （`doc_key` 变了会改主题归属，那是另一个话题）。
* 隐私边界照旧写在 docstring 里：**只在用户点「解释并记录」那一刻取一次**，
  没有后台窗口采集、没有常驻监听；这也和 D 批的剪贴板监听设计一致。

### 实测与回归

* 新增测试 **74 条**：`tests/test_db.py` 的 `TestTagsMergeAndContext` **19 条**
  （`Ran 37 OK`，原来 18 条）；新文件 `tests/test_export_service.py` **9 条**、
  `tests/test_search_service.py` **11 条**、`tests/test_source_enrich.py` **16 条**
  （B4 / B6 走**真实 `bookmark()`**，从 `CapturedSelection` 落库到读回标题全链路）；
  `tests/test_ui_roundrect.py` 末尾新增 `TestLibraryOrganizationControls` **17 条** +
  `TestSettingsDialogDuplicateAction` **2 条**（在**真实 `MainWindow` + 假 Tk** 上跑，
  一个窗口都不建；`Ran 173 OK`，原来 154 条）。
* 白名单回归（**17 模块** = 原 13 + `test_db` 扩编 + 3 个新服务测试模块）：
  **Ran 679 / failures=7** → 修完 → **Ran 715 / failures=6**；
  6 条全是 §18–§22 的同一批老失败（`tests.test_reading_panel` 2 条 +
  `tests.test_unified_action` 4 条），**没有一条新失败**；
  `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
  计数关系：A 批基线 641/6 + `test_db` 新增 19（18→37）+ `test_ui_roundrect` 新增 19
  （154→173）= 679；再加 `test_export_service` 9 + `test_search_service` 11 +
  `test_source_enrich` 16 = **715**。
* **回归抓到的一个真问题（值得记下）**：`tests/test_reading_panel.py` 里有一条
  「工具条方法审计」——它按**按钮文字**建字典再断言每个按钮绑在哪个方法上。
  我一开始把标签区的按钮也叫「重命名 / 删除」，与主题区同名 ⇒ 字典互相覆盖，
  审计读到 `'rename_tag' != 'rename_batch'`。改成
  **`标签改名 / 标签删除 / 全部`** 之后互不重名，同时把
  `合并… / 拆分… / 导出 / 标签改名 / 标签删除 / 全部` **一并写进审计表**
  （`main_bound`），B 批的每个入口都被这条测试盯着。
* **仍未实机验收**：144 DPI 下左栏（主题列表 + 标签列表 + 两组按钮）与中栏卡片
  （勾选框 + 标签行 + 命中行）的实际排版、`合并…/拆分…` 两个对话框的观感、
  导出后弹「打开文件夹」的体验、以及真机上的 append 追加上下文。
  源码模式重启 `启动.cmd` 即可试；**exe 需要重新打包**（`tools\build_app.ps1`）才会带上
  `app/export_service.py`、`app/search_service.py`、`app/source_enrich.py`、
  `app/ui/batch_dialogs.py`（`bootstrap.py` 的 `_DIAGNOSE_MODULES` 已加这四个）。
* 下一批：清单里的 C 批（导图导出 / 孤立词汇总面板 / 局部生成 / 点节点跳编辑 /
  人工关系最高优先级 / AI 关系与人工关系在界面上区分）。

## 24. 文档事故与恢复（README 被整体覆盖）

**事故**：写 §23 时我用整文件写入的方式改 `README.md`，把这个 1747 行的文档整体
覆盖成了 61 行的 §23 草稿。项目里没有 git，也没有 `README.md.bak`；卷影副本不可用，
VS Code 本地历史 / PyCharm LocalHistory / 其它编辑器的草稿库里都没有这个文件。

**恢复**：素材来自 DSH 自己的会话记录
`%USERPROFILE%\.dsh\sessions\--D-~63A2~7D22~5DE5~5177--\session-<id>\session.v3.jsonl.zstd`
（zstd 压缩的 JSONL，CPython 3.14 自带 `compression.zstd` 可解）。做法是**重放**：
把会话里对该文件的每一次 `read` 结果（按内容锚点拼回去）与每一次 `edit`（精确替换，
失败时退回「空白归一化」的模糊匹配）按时间顺序重放，遇到那次整文件写入就停下，
再把磁盘上的 §23 接回去。脚本在 `_check\recover_readme4.py`、`_check\assemble_readme.py`、
`_check\finalize_readme.py`（非交付物），中间产物在 `_check\readme_recover\`。

**结果**：1822 行、§1–§10 与 §12–§24 齐全。**仍然缺两处**（正文里用
「本节此处约 N 行内容在 README 误覆盖事故中丢失」标出）：§10 尾部共约 44 行
（会话里最后一次快照早于这段，之后再没读过），§20 的「先词后线」小节附近约 18 行。
**没有 §11** —— 在所有快照里都没出现过这个标题，无法判断它当时是否存在。

**教训**：追加小节要用「在文件末尾插入」的方式，绝不能用整文件写入；长文档在
没有版本控制的情况下必须留副本（后续 E 批的数据库自动备份同理，但文档也该有一份）。

## 25. 批次 C：参考关系图（导出 / 诊断面板 / 局部生成 / 跳转 / 人工关系 / 来源区分）

《改进清单.md》C 批六条一次做完。三条硬约束贯穿全批：
**不引入任何第三方库**（PNG 用 `zlib` + `struct` 手写、抓图走 `ctypes`）、
**新代码全部能在假 Tk 上跑完**（本轮测试一个真窗口都没建）、
**人工关系优先于 AI 且绝不被重新生成覆盖**。

### C1 关系图导出：完整 PNG + 可编辑 SVG（重写后的 `app\map_export.py`，~1120 行）

* **口径（用户 m06968 + m07063 定死）**：导出**只有图片**——「要么是可编辑的图片，要么就是
  PNG 图片，但是要完整」，**不要 Excel / JSON**。所以 CSV / JSON / `graph_payload()` /
  `relation_rows()` 全部删除，`RELATION_COLUMNS` 等常量一并去掉。
* 入口：导图窗底部第二条按钮行的「导出关系图…」→ `ConceptMapWindow.export_map_files()` →
  `map_export.export_map(layout=…, style=export_style(), canvas=…)`。
* 落盘位置固定 `data\exports\`（`paths.exports_dir()`）——**冻结运行时没有
  `tkinter.filedialog`**，「另存为」不可用，所以沿用 B 批导出的那套口径：
  写固定目录 + 反馈里给文件名 + `os.startfile` 打开所在文件夹。
* 文件名 `探索词典-关系图-<主题>-<时间戳>.svg` + 同 stem 的 `.png`，
  `unique_stem` 对**两个后缀整体退避** ⇒ 第二次导出是 `…-2.svg`，不覆盖上一份。
* **完整 SVG**：`svg_document()` 手写矢量，`box` 默认取 `content_box(layout)` —— 把分组底衬、
  主题细线、连线（含折线拐点）、标签底板、卡片、词条标题、孤立词说明**全部**并进外框
  （只按节点算就会把绕在外面的连线和标签裁掉），`x0/y0` 夹到 ≥ 0（画布原点就是内容左上角，
  请求负坐标会被滚动位置悄悄夹住）。文字用 `<text><tspan>` = **真文字**，浏览器 / Inkscape /
  Illustrator / Figma 里都能直接改；对称边不出箭头；人工边恒为**主色实线加粗**（`edge_style()`
  忽略 `EDGE_STYLES["manual"]` 里的颜色与虚线）；页脚三行 = 主题 / 词数与关系数 + 模型 + 导出时间 / 图例。
  `write_svg()` 同样是 `.part` + `os.replace`。
* **完整 PNG（C1 的 bug 修复点）**：旧实现第一条就是 `GetClientRect(hwnd)` —— 那是**画布控件的客户区**，
  也就是「当前看得见的那一块」，图比窗口大时导出的 PNG 只有视口（用户说的「不完整」）。
  现在 `capture_canvas_png(canvas, path, *, box=None, max_pixels=…)`：
  `plan_tiles()` / `tile_origins()`（纯函数；相邻块重叠 `TILE_OVERLAP = 2` px、最后一块贴齐右 / 下边）
  算出抓图起点 → 逐块 `xview_moveto`/`yview_moveto` + `update_idletasks()` →
  `PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)`（返回 0 退回 `BitBlt(SRCCOPY)`）→
  `BitBlt` 拼进目标位图（落点按 `canvasx(0)`/`canvasy(0)` 的**实际**位置算，不信请求值）→
  抓完恢复原滚动位置 → `GetDIBits`（32 位、顶向下）→ `bgra_to_rgb(..., step=…)` → `write_png()`。
  `write_png()` 仍是手写编码（`zlib` + `struct`，冻结运行时里没有 Pillow），先写
  `<名字>.<pid>.part`，全部成功才 `os.replace`；GDI 对象在异常路径也释放。
* **两个上限**：`MAX_NATIVE_PIXELS = 40_000_000`（超过直接拒绝：
  「关系图太大（W×H 像素），先缩小视图或减少词条再导出」，绝不申请几 GB 位图）；
  `MAX_CAPTURE_PIXELS = 8_000_000`（超过按整数倍**抽稀**，`summary()` 里写明「图很大已按 1/k 抽稀」）。
* **图片失败不拖累矢量图**：抓不到 PNG 时只把原因写进 `MapExportResult.png_error` +
  `log.warning("关系图 PNG 导出失败（SVG 已写出）：%s")`，**SVG 一定已经落盘**，
  反馈写「…<stem>.svg（可编辑矢量图）；PNG 没抓到：…」。
* **`map_export` 不 import `map_service`**：那条链会拉进 `config → db → crypto_dpapi`
  （进程启动即 `WinDLL("crypt32")`），而导出模块要能在没有数据库、没有 Win32 的环境里被单独
  import（有测试守着）。人工来源标记因此是本地常量 `MANUAL_ORIGIN = "user"` +
  一条「必须与 `map_service.MANUAL_ORIGIN` 一致」的守卫测试。
* **可见代价（已写进《软件说明》）**：导出 PNG 时画布会**快速滚动一遍**（逐块抓图），
  抓完自动回到原位。

### C2 孤立词诊断汇总面板

* 导图窗底部新增「孤立词诊断」按钮，展开 `self.diag_text`（只读 `tk.Text`，默认不 pack，
  右下角与依据区共用同一块位置，`pack(before=self.evidence)`）。
* `_diag_summary()` 的内容：首行「本次分析 N 个词：X 条关系，Y 个孤立词（其中人工添加 Z 条）」；
  再一行「被否掉的候选：核对认为不成立 n 条　核对不确定（材料不足） n 条」；
  然后**每个孤立词一行**「· 词名 —— 判定（连向 目标）」；最后一行提示
  「（双击某一行可以在图上定位这个词；人工关系不算 AI 判断）」。
* 文案诚实：判定标签直接用 `VERDICT_LABELS`；没有判定记录时说
  「这次没有留下判定记录（旧缓存 / 模型一条候选都没提）」，**不说「没有关系」**；
  候选行只报候选的终点（`连向 <dst>`），不替模型编方向；没有孤立词时明确写
  「没有孤立词：每个词都至少有一条画出来的关系。」
* 双击某行 → `_on_diag_open` 用 `_diag_lines`（**0 基行号 → entry_id**）反查 →
  `_focus_node(entry_id)` 选中 + `xview_moveto/yview_moveto` 居中；点标题行/空白不误跳。

### C3 局部生成（只分析选中的词）

* 左栏主题列表下面新增「词条（可多选，只分析选中的）」`Listbox`
  （`selectmode="extended"`，双击一条也能打开它 —— C4 的第二条路径）+
  「只分析选中的词」/「分析全部词条」两个按钮。
* `_scope_nodes()` 是**唯一口径**：`_subset_ids` 决定这次分析哪些词；
  `_current_fingerprint()` 也走它 ⇒ **子集结果不会被误判成过期**；
  `_draw_key` 里也带 `tuple(sorted(_subset_ids))`。
* 子集视图的提示行写「本次只分析选中的 N 词（该主题共 M 词）」；没选词就点按钮时
  只给「先在左边选几个词（Ctrl / Shift 可多选），再点『只分析选中的词』」，不发请求；
  「分析全部词条」一键切回全量（提示「已切回全部词条…」）。库里词条被删时子集自动收缩。

### C4 图里跳转编辑

* 三条路径：双击词卡、双击左栏词条、「打开选中的词条」按钮（先在图上点一个词）。
  没点词就按按钮只给提示，不乱开主界面。
* `_open_entry()` 的顺序是 **`app.open_main_window()` → 主界面 `set_browse_scope(topic_id)`
  → `main.select_entry(entry_id)`**。中间这一步是必须的：只调 `select_entry` 的话，
  主界面若正停在别的主题（或跟着阅读页面走），右栏会显示这条词、左边列表里却找不到它，
  看起来像「跳过去没反应」。反馈写「已在主界面打开这条词条（改完回这里点『重新生成』）」。
* 双击空白 / 双击连线什么都不做（不误跳窗口）。

### C5 人工关系（最高优先级，复用历史遗留的 `relations` 表）

* **数据层（schema v8）**：`relations` 加 `topic_id`、`note` 两列 + `idx_relations_topic`
  索引。迁移走 `_COLUMN_MIGRATIONS` + `_INDEX_MIGRATIONS`（幂等，老库补齐列后再建索引）——
  **索引绝不能写进 `_DDL`**：老库还没有 `topic_id` 列时会直接建索引失败。
  新 API：`set_manual_relation(topic_id, src, dst, rel_type, note="")`
  （自环 → `ValueError`；label 空 → 「相关」；同一 `(topic, src, dst)` 先删后插 ⇒
  **重新添加即「改类型 / 改依据」**；`origin='user'`）、`list_manual_relations(topic_id)`、
  `delete_manual_relation(rid)`（返回是否真删到）。
* **服务层**：`MANUAL_ORIGIN = "user"`；`ManualRelation(MapRelation)` 多带
  `relation_id` / `note` / `origin`（`as_dict()` 里有 `id`/`origin`/`note`）；
  `is_manual(rel)`；纯函数 `manual_relations(rows, nodes)` 丢掉自环与端点不在本次节点表里的行，
  按 `(src, dst, type)` 去重后排序。
* **出图合并（界面层）**：`relations_for_draw()` = 过了核对的 AI 边 + 人工边，
  **同一对端点人工优先**（对称类型连反向的 AI 边一起挡掉），人工边与 AI 边**一起参与布线**；
  `_drop_expired()` / `_draw()` 从不碰 `_manual_rels` ⇒ **重新生成 / 换指纹 / 切子集都不会
  覆盖人工关系**（测试直接断言：force 清理后 `_graph is None`、人工边仍在）。
  人工关系不发任何网络请求、也不参与模型核对（它没有 evidence）。
* **新对话框** `app\ui\relation_editor.py`（纯 tk，复用 `widgets.BorderlessChrome`）：
  左选起点 / 右选终点 / 类型单选（AI 的 6 种 + 「相关」）/ 备注输入框 / 已添加列表 +
  「添加 / 更新这条关系」「删除选中的关系」。**画布窗口上仍然没有**裸的起点/终点/关系名
  输入框（那条旧契约测试保留，只改成新名字）。
* 本轮**没做**、留给后续的两条：删掉某条 AI 边后「不再自动回来」的黑名单；
  人工边的类型白名单只在**界面**层限制（`db.set_manual_relation` 不校验类型）。

### C6 AI / 人工关系视觉区分（**线上外观已被 F7 撤回**）

* ~~`EDGE_STYLES` 新增 `"manual": {"fill": theme.ACCENT, "dash": ()}`（主色实线）；
  人工边线宽 `base + px(1)`，标签文字加前缀 `人工·` 并染主色~~ —— 2026-10-04 用户看过实机截图后
  否掉了这一套（原话「不要出现『人工-xx』的线标注」「指向线粗细颜色都不同」），
  线上改为**全图同一个颜色、同一个粗细、同一个「线上只写类型名」**，只留虚实线区分关系类型；
  做法见 §26 的 **F7**。**「人工」这个词现在只出现在依据面板、点词行、对话框与菜单里**，
  画布与导出的图上刻意看不出来。
* 依据面板（**保留**）：人工边显示「人工·因果：卷积 → 过拟合」+「人工添加的关系（你自己的判断，
  重新生成不会覆盖）」+ 有备注时「你写的依据：…」（**不假装有证据片段**）；
  点词时的人工边行写成「　· 起点 --人工·类型--> 终点」，并提示「带『人工·』的是你自己加的
  关系，不受重新生成影响」；页脚仍统计「（其中人工 k 条）」。底部状态行那条常驻图例**已删除**。
* 顺手修掉一条真 bug：`_highlight_edges()` 原来给**所有**边覆写同一个基准宽度，
  把刚画粗的人工边又抹回 1px（画出来 2px、读回来 1px，实机上「更粗」根本看不出来）。
  F7 之后人工边不再加粗，这条覆写只剩「选中加粗 2px」一种作用，线宽读回来必然等于画下去的值。

### 实测与回归

* 新增测试 **59 条**：`tests/test_db.py` 37 → **55**（+18，主体是新的
  `TestManualRelations`：v8 版本号 / 两列 / 索引 / 老 v7 库补列且幂等 / 增删改查 /
  同 `(topic, src, dst)` 覆盖 / 自环报错 / 空类型兜底 / `topic_id=None` 存 0 /
  删词条带走它的人工关系 / 删主题不影响别的主题 / 外键挡住不存在的端点）；
  新文件 `tests/test_map_export.py` **48 条**（**不 import tkinter、不建任何窗口**：
  文字宽度估算、分块与抽稀的纯函数、`content_box` 的并集与夹边、SVG 的 XML 合法性 /
  真文字 / 人工边样式 / XML 转义 / 页脚、`write_png` 的字节结构、
  `export_map` 端到端「目录里只能有图片」、`winfo_id()==0` 时报错且不留半截文件、
  「先算尺寸再申请位图」的顺序）；
  `tests/test_ui_roundrect.py` 173 → **188**（+15，`TestConceptMapManualAndTools`：
  人工边落库/画布样式与线宽、人工优先、清理后仍在、依据面板文案、
  子集生成把哪些 nodes 交给服务层（把 `service.generate` 换成记录器 ⇒ 无线程、确定性）、
  诊断面板汇总与双击定位、双击词卡跳主界面、导出可编辑图且**没有** csv/json +
  PNG 失败反馈、还没画图时的提示、`RelationEditor` 表单校验 / 添加 / 删除）。
* 18 模块白名单回归（原 17 + `tests.test_map_export`）：
  **Ran 798 / failures=7**，7 条 = §18–§24 的同一批老失败
  （`tests.test_reading_panel` 2 条 + `tests.test_unified_action` 4 条）
  **+ 1 条环境性失败**：`test_root_is_withdrawn_before_build_app` 断言
  `app_main.main([]) == 0`，而桌面上正跑着一份 `探索词典.exe`（PID 8892）持有
  `Local\ExplorerDict.SingleInstance.v2.<data_dir_digest>` ⇒ 返回
  `EXIT_ALREADY_RUNNING = 5`。关掉那个实例就恢复 0，**与代码改动无关**；
  日志 `_check\tests_after_export_images.log`。
  计数关系：715（B 批基线）+ 18 + 14 + 25 = 772（§23 少记 2 条）→ C 批 774
  → 导出改版 +24（`test_map_export` 25 → 48、`test_ui_roundrect` 187 → 188）= **798**。
* `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0；
  `bootstrap.py` 的 `_DIAGNOSE_MODULES` 补了 `app.map_service`、`app.map_export`、
  `app.ui.relation_editor`（冻结运行时缺文件时 `--diagnose` 能直接点名）。
* 改写了一条过时契约：`test_no_manual_relation_controls_and_a_single_entry` →
  `test_the_canvas_window_keeps_its_own_controls_minimal`（保留「画布窗口上没有裸输入框 /
  只有一个生成入口 / 界面不出现内部编号」，新增四个新入口、`selectmode == "extended"`、
  诊断面板默认收起三条）。
* **仍未实机验收**：PNG 抓图在真窗口 + 144 DPI 下的效果（`PrintWindow` 在硬件加速画布上
  是否给全内容、被别的窗口遮挡时会抓到什么）、导出后自动开文件夹的体感、
  人工关系对话框的观感、子集分析的 token 感受、真机上双击节点跳主界面
  （尤其主界面此刻处于「跟随阅读页面」状态时）。
* exe 需要重新打包（`tools\build_app.ps1`）才会带上 `app\map_export.py` 与
  `app\ui\relation_editor.py`。
* 下一批：清单 D 批（悬浮窗关闭后新划词行为 / 门控自定义黑白名单 / 多显示器安全 /
  剪贴板监听 / OCR 截图取词可选组件）。

## 26. 批次 F：借鉴 Project Graph（拖拽连线 / 卡片位置固定 / 双击连线改删）

《改进清单.md》批次 F 共 6 条，用户只点单 **F1 + F2 + F3**，这三条本轮做完；F4–F6 待定。
关于「去掉比较花哨的功能」，用户顺手把口径说清了：**不是外观花哨** —— 原话是
「要的是功能，比如那个 project 的思维导图有什么旋转之类的那种实用性偏低的功能我们就可以不要。
**外观后面还要进行美化**。」⇒ 要砍的是子树旋转 / 质点 / 斩断线 / 双链 /
扩展系统 / 音效 / 透明窗口 / 多标签页 / Mermaid 互导 / WASD 飞行 / 循环空间 / 自动计算引擎 /
LaTeX 与图片节点 / 自定义节点类型与主题这类**实用性偏低的功能**（清单里的「明确不学」一串）。
另外 ProjectGraph 的「右键拖拽连线」不能照搬 —— 我们的右键早就是拖拽平移
（`app\ui\concept_map.py`:1861-1866），所以 F1 先改成「卡片上的连线柄 + 左键拖拽」，
**F7 又把柄撤掉、换成 `Alt` + 左键**（见下面的 F7）。

### F1 从卡片拖出连线（新建人工关系）（**做法已按 F7 改成 `Alt` + 左键**）

* ~~每张词卡右侧中点画一个 `_kind="node-handle"` 的小圆点（`HANDLE_RADIUS = 3.2`、
  命中放宽 `HANDLE_HIT_PAD = 6.0`），位置表 `self._handle_nodes = {item_id: (entry_id, hx, hy)}`~~
  —— F7 之后这一族全部删除，卡片上**没有任何连接点**。
* 新增三条绑定（全部 `add="+"`，与既有的单击选中 / 双击 / 右键与中键平移 / 滚轮缩放互不干扰）：
  `<Button-1>` → `_on_canvas_press`（**按住 `Alt`** 才进入连线模式，见 `ALT_MASK`）→
  `<B1-Motion>` → `_on_canvas_motion`（浅灰虚线跟随鼠标；拖动期间只 `canvas.coords()` 更新那一条线，
  不重建图元）→ `<ButtonRelease-1>` → `_on_canvas_release`（`_node_under()` 找松手时压着的卡片）。
  另外绑 `<Alt-Button-1>` / `<Mod1-Button-1>` 作兜底（`_on_canvas_press_alt()` 先置 `_alt_armed`），
  `_on_canvas_press` 开头做「已在连线 / 已在拖卡就直接返回」的去重。
* 松在另一张卡上 → `open_relation_editor(src_id=…, dst_id=…)`：编辑器新增关键字参数，
  两端替你选好（`_select_entry()` 选中并滚进视野），状态行写「两端已经替你选好了…」，
  填个类型按「添加 / 更新这条关系」就建好了（F7 之后画出来与 AI 边**同款**）。
  松在空白 / 松在同一张卡 → 反馈「连线取消：把线拖到另一张词卡上松开才会建立关系」/
  「连线取消：起点和终点是同一个词」，**库里一个字都不写**。
* 「人工关系…」按钮仍走无参路径（`open_relation_editor()`），行为与批次 C 完全一样 ——
  拖拽只是多了一条更顺手的路，不是替代。

### F2 卡片位置可固定（schema v9 · `node_pins`）

* 新表 `node_pins(topic_id, entry_id, x, y, updated_at, PRIMARY KEY(topic_id, entry_id))`
  + `idx_node_pins_topic`，`entry_id` 带 `ON DELETE CASCADE`（删词条顺手清位置）；
  新 API `set_node_pin()` / `list_node_pins()` / `delete_node_pin()` / `clear_node_pins()`，
  非有限坐标直接 `ValueError("卡片坐标必须是有限数字")`。
* **坐标只在同一个主题里有意义** ⇒ `list_node_pins(None)` 只认主题 0，**不做跨主题合并**
  （有测试把这条语义钉住）。
* `_layout_core(..., pins=None)`（`layout_graph()` / `layout_extent()` 同样透传）：先按算法排好，
  再把有 pin 的卡片挪到 `max(m.pad + node.w / 2, x)` / `max(top + node.h / 2, y)`
  （夹回画布内），随后**重建 `by_id`** ⇒ 连线、分组底衬、`content_box`、自动适应视野
  全都跟着新位置走；`_draw()` 与 `_auto_fit()` 都传 `pinned_positions()`。
* 交互：按住卡片本体拖动（`_drag_node`），拖动中 `_move_node_items()` **只搬这张卡**的框 / 文字 / 柄
  （不重排全图）；松手 → `pin_node(entry_id, x, y)` 落库 + 反馈
  「已固定这张卡片的位置（「恢复自动布局」可一键放回去）」+ 重画。
* 新按钮「恢复自动布局」（`bar2`）→ `reset_node_pins()`：没有 pin 时反馈
  「没有固定过位置的卡片：现在就是自动布局」；有 pin 时清库 + 「已恢复自动布局（取消固定 N 张卡片）」。
* 换主题 / 词条变化时 `_reload_nodes()` 会重读 pin 与黑名单，不会把上个主题的位置带过来。

### F3 双击连线：人工边改 / 删，AI 边拉黑

* `_on_canvas_double_click()` 的判定顺序是 **先词卡（C4 跳主界面）→ 再连线**，都没命中才什么都不做 ——
  双击词卡跳转是既有承诺（C4 的测试仍绿），所以词卡优先。
* 双击**人工边** → `open_relation_editor(relation=rel)`：`RelationEditor(master, owner, *,
  src_id=None, dst_id=None, relation=None)` 进入**编辑模式**，两端 / 类型 / 依据都填好、
  列表选中那一条，状态行写「这就是那条人工关系：改完按「添加 / 更新这条关系」= 覆盖它，
  按「删除选中的关系」= 删掉它。」
* 双击 **AI 边** → 新对话框 `app\ui\edge_block_dialog.py`（`EdgeBlockDialog(master, owner, edge)`，
  纯 tk）：只读显示「起点　--类型-->　终点」+ 模型给的依据 + 证据片段
  （缺了就说「（模型这次没有给证据片段）」），按钮「标为不对（以后不再出现）」；
  下面列出该主题已经标为「不对」的关系，选中一条按「恢复这条关系」放回来。
* 黑名单表 `map_edge_blocks(id, topic_id, src_entry_id, dst_entry_id, label, reason, created_at,
  UNIQUE(topic_id, src_entry_id, dst_entry_id))`；`map_edge_block_pairs()` **两个方向都算**；
  `relations_for_draw()` 在「人工优先」之前先把命中的 AI 边丢掉 ⇒ **重新生成的新候选同样不画**；
  如果在同一对端点上手工建了关系，`save_manual_relation()` 会顺手清掉黑名单（人工的说了算）。
* 这一条把 §25 里 C5 遗留的「删掉某条 AI 边后不再自动回来」补齐了（剩下的一条「人工边类型白名单
  只在界面层限制」仍在清单上）。

### 顺手修掉的两个真 bug

1. **卡片拖不动**：圆角卡片走 `create_polygon`（`_round()`），第一个点是圆角弧的起点，
   而 `_move_node_items()` 原来拿 `coords[0]/coords[1]` 当卡片左上角比对 ⇒ 一张卡都匹配不上。
   改成按**外接矩形中心**认卡片（`min/max` over `coords[0::2]` / `coords[1::2]`），
   文字图元仍按中心比对。这条在假 Tk 上是「拖了但一张都没动」，在真机上就是「按住卡片拖，毫无反应」。
2. **临时连线画不出来**：原来直接调 `canvas.find_withtag(self._link_item)`，假画布没有这个方法 ⇒
   异常被吞掉后 `_link_item` 被清空。改成 `getattr(self.canvas, "find_withtag", None)` 探测可用性，
   再决定「新建 or `coords` 更新」。
   （顺带一条健壮性：`RelationEditor._select_entry()` 只有 `selection_clear` / `selection_set`
   是必须的，`activate` / `see` 改成 `getattr` + `callable` 判断的可选调用，极简替身也能预选成功。）

### F7 导图外观 / 交互返工（按实机截图反馈）

用户拿真机截图一口气列了 6 条问题，逐条对号如下（**只改外观与交互，AI 边样式与筛选口径一个字没动**）：

* **① 「框选位置的提示背景是透明的」** —— 真因是提示浮层跟画布**取的是同一个颜色**
  （都 `theme.PANEL = #F9F8F6`，它连边框都没有，自然「糊」在画布上）。现在
  `hint_box = tk.Frame(self.canvas, bg=theme.CARD_BG, highlightthickness=1, highlightbackground=theme.BORDER)`
  + 内层 `hint_inner` 给内边距，三行文字用 `theme.TEXT_MUTED`；位置仍是右下角
  `place(relx=1.0, rely=1.0, anchor="se", …)`。
* **② 「指向线粗细颜色都不同」** —— `EDGE_STYLES` 五项原来各给一个灰度
  （`BORDER_STRONG` / `TEXT_BODY` / `TEXT_FAINT`），人工边还额外加粗。现在新增
  `EDGE_FILL = theme.TEXT_FAINT`、`EDGE_WIDTH = 1`、`EDGE_LABEL_FILL = theme.TEXT_MUTED`：
  五项的 `fill` 全指向 `EDGE_FILL`，`_draw` 边循环与 `_highlight_edges()` 一律
  `width=max(1, theme.px(EDGE_WIDTH))`（选中才 +2px）；**只有虚实线仍按关系类型区分**
  （`hierarchy`/`direction` 实线、`cross` 长虚线、`feedback` 点线，`manual` 实线）。
* **② 「不要出现『人工-xx』的线标注」** —— 线上标签就是类型名；`MANUAL_LABEL_PREFIX`
  与 `MANUAL_KIND_NOTE` 现在只出现在依据面板与对话框里。
* **② 「线标注的文本位置也不对」「文本背景应该是透明的」** —— 标签底板
  （`_kind="edge-label-plate"`）整个删除；`label_box()` 原来拿**卡片行高**算标签尺寸，
  于是标签被顶到离连线十几像素开外，新增 `LABEL_FONT_SIZE = 7` + `label_em(m)` +
  `label_gap_px(m)` 一套自己的度量；`_label_position()` 改成**有序搜索**
  （先试基准点，再按 `LABEL_SLIDE_STEPS × LABEL_OFFSET_STEPS` 找不压卡片、不跟别的标签
  打架的位置，全压着时退回「面积最小、离基准最近」）；`_draw()` 里标签改到
  **所有卡片之后**才画，所以文字永远压得住卡片。
* **③ 「不要设置连接点」** —— `node-handle` 一族全部删除：常量 `HANDLE_RADIUS` /
  `HANDLE_HIT_PAD`、状态 `_handle_nodes`、方法 `_handle_at()` / `_oval()`、`_clear()` 里的复位，
  以及 `_move_node_items()` 里同步搬柄的那一段。测试里加了
  `test_cards_carry_no_connection_handles`（连 `hasattr` 一起断言），防止哪天又长回来。
* **③ 「按下 ALT+鼠标左键拖动…才会开始进行关系构建」** —— 新增 `ALT_MASK = 0x00020000`
  与 `_alt_held(event)`；`_on_canvas_press` 里 `Alt` 按下才进连线模式，否则一律当「拖卡片摆位置」。
  另绑 `<Alt-Button-1>` / `<Mod1-Button-1>` → `_on_canvas_press_alt()`（先置 `self._alt_armed`
  再转发），两条绑定同时命中也只开一次工（`_on_canvas_press` 开头判 `_link_from` / `_drag_node`）。
  `HINT_LINES[0]` 改成「左键拖卡片 = 摆位置（拖过就固定）· 按住 Alt + 左键拖到别的卡 = 建关系」。
* **③ 「标签的移动锚点有问题…要顺滑跟随鼠标」** —— 这是**两个真 bug**，都修了：
  ① `_move_node_items()` 原来按**布局坐标**重算位移（鼠标走一步、卡片走两步 ⇒ 越拖越飞），
  现在改成**增量位移**：`dx, dy = cx - last_x, cy - last_y`，搬完记 `_drag_last`；
  ② 卡片圆角是 `create_polygon`，原来按 `coords[0]` 认卡片永远匹配不上，
  改成按下时用 `_node_items()` 按**外接矩形中心**认准这一张卡的图元（`node` / `node-text`），
  拖动期间只 `canvas.coords()` 平移，不再每帧全量重算。
* 同步改：`ConceptMapWindow.export_style()`（导出用同一套 `edge_fill` / `label_fill` /
  `line_width`）、`app\map_export.py`（`LEGEND_LINE` 改写、`edge_style()` 不再特判人工、
  删掉 `manual_label()`、标签不画底板、`DEFAULT_STYLE` 去掉 `manual_width` /
  `manual_label_prefix`、docstring 与 `content_box` 估宽同步）、`tools\ui_preview.py`（离线预览同步）。

### 实测与回归

* `tests/test_db.py` 55 → **71**（+16）：`test_schema_version_is_9`、老 v7/v8 库补两表且幂等、
  `TestNodePins` 8 条（往返 / 覆盖 / 按主题隔离 / 非有限数拒绝 / 删除与清空 / 外键 / 级联）、
  `TestMapEdgeBlocks` 9 条（两向可查 / 同一对只留一行 / 自环报错 / 按主题隔离 / 反向清空 /
  删除命中与未命中 / 级联 / 外键）。
* `tests/test_ui_roundrect.py` 188 → **205**（+17，新类 `TestConceptMapDragPinsAndEdgeBlocks`）：
  柄的数量与命中、拖柄到另一张卡才打开编辑器（临时虚线出现又在松手后消失）、
  松在空白 / 松在同一张卡取消且不落库、拖卡片落库并只动这一张、重新生成后位置不变、
  `_reload_pins()` 重开读回、换个主题就是空、恢复自动布局两个分支、
  纯函数 pin 精确落点与负坐标夹回、双击 AI 边与人工边与词卡三条路径、
  拉黑后立刻不画且「重新生成」也不回来（黑名单两向）、恢复、恢复没标过的、
  建人工边顺手清黑名单、`EdgeBlockDialog` 端到端（文案 / 拉黑 / 列表 / 恢复 / 关闭）、
  `RelationEditor` 预选与编辑模式、按钮路径无参打开。
* **F7 之后**：`tests/test_ui_roundrect.py` 205 → **208**（改 8 条旧契约 + 新增
  「零连接点 / `Alt` 拖拽 / 两条绑定只开一次 / 不按 `Alt` 只挪位置 / 连拖 4 步跟手」5 条，
  标签那条改成「零底板 + 文字=类型名 + 颜色统一 + 最后画 + 贴着自己那条线」）；
  `tests/test_map_export.py` 48 → **49**（`test_manual_edge_looks_exactly_like_an_ai_edge`、
  `test_a_manual_label_still_counts_in_the_box`、
  `TestManualOrigin.test_manual_edges_are_not_special_cased_for_style`）。
* `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0；
  `bootstrap.py` 的 `_DIAGNOSE_MODULES` 补了 `app.ui.edge_block_dialog`。
* 18 模块白名单回归：**Ran 835 / failures=6**（831 + 3 + 1 = 835，日志
  `_check\tests_after_map_feedback.log`），6 条全是既有陈旧失败
  （`tests.test_reading_panel` 2 条 + `tests.test_unified_action` 4 条，与 §25 同一批）；
  上一轮那条**环境性**的单实例失败这次没出现（桌面上那份 `探索词典` 进程当时没占着互斥体）。
* **仍未实机验收**：`Alt` + 左键的手感（跟右键平移 / 单击选中 / 双击跳转的冲突）、
  标签在密集图里会不会仍被挤开、提示浮层在小窗口下会不会挡住卡片、拖卡片后压住别的卡时
  读不读得清、固定位置在换缩放 / 换显示器后的表现、「恢复自动布局」的体感、
  AI 边拉黑对话框的观感，以及**重新生成时被固定的卡片与 AI 边的绕线是否仍然好看**。
* exe 需要重新打包（`tools\build_app.ps1`）才会带上 db **v9** 与 `app\ui\edge_block_dialog.py`；
  老库第一次用新版打开会自动补两张新表（已在假数据上验证过幂等）。
* 下一批：批次 G（关系图模板）→ 见 §27；F4–F6 待用户点头；清单 D 批 / E 批（见 §25 末尾）仍在队列里。

## 27. 批次 G：关系图模板（布局骨架）

来源：你给了 10 张模板缩略图，问「**LLM 可以挑选合适的模板进行生成，用户也可以自己选择模板
进行自定义画导图**」。落地口径按你确认的三条：① 第一版做推荐的 6 个下拉项（含流程线三型）；
② 挑选走「本地规则 + **可选开启**的模型建议」；③ 切模板前问一句、确认后取消固定。
另加一条你追加的操作要求：**思维导图里左键是拖动，左键 + `Alt` 才是建立关系**。

**模板 = 布局骨架**：只决定「卡片摆成什么形状、线往哪长」，**不动关系数据、不改筛选口径、
不多花钱、不写库**（只往设置里记一个 key）。同一份关系换模板 = 换一种摆法，随时切回「自动」。

### 你在界面上会看到
* 关系图窗口工具条多了两个按钮：**「模板：×××」**（点开是选择对话框）与**「操作说明…」**
  （一张手势表，内容与代码里绑定的那份是同一份事实来源，不会各写各的）。
* 选择对话框里是清单 + 每一项的一句话说明 + 「适合：…」；顶部有**本地建议**；
  在**导图窗口**工具条点 `导图设置…` 勾上「打开模板对话框时，也让模型看一眼该用哪种排法」后，
  对话框里才会多出 `让模型也看一眼` 按钮（**主界面「设置」里不再显示导图这一节 —— 见 §28**）。
* 页脚（导出图上那一行）变成「…　模型：…　**模板：…**　导出：…」——只有非自动布局才写模板名，
  「自动」与今天**逐字节一致**。

### 九个下拉项（4 个摆放实现 + 参数）
| 下拉项 | 骨架 | 适合 |
| --- | --- | --- |
| 自动（按关系分层） | 今天这套：一层一行 + 整行居中 + 孤立词网格 | 通用 / 混合关系 |
| 思维导图 | 主题居中，一级分支左右分列，子树向外横排 | 包含·属于为主、发散 |
| 树状图 | 自上而下递归**子树块**（同一个父的子孙聚在一块，父压在块的几何中心） | 层级清楚 |
| 组织架构图 | 同树状图但**严格一层一行**（金字塔 + 自下而上重心 + 行内拉开） | 层级清楚、整齐 |
| 单向导图（向右） | 根在左、向右长、同层兄弟同列 | 层级清楚、宽屏 |
| 鱼骨图 | 因果层拓扑排成水平主脊，其余词挂上下两侧 | 一个结果 + 多条原因 |
| 流程线（水平 / 垂直 / S 型） | 按关系方向（`因果`·`依赖`）排成一条链；S 型折行反向 | 链式 / 步骤 |
* ❌ 没做：**树状图（右下）**（与树状图重复，只是斜着画）与**大纲**（左栏词条列表 + 孤立词诊断
  已经是这个信息，画到画布上反而占地方）。
* ⚠️ **命名诚实**：缩略图上的「水平 / 垂直 / S 型时间线」在这版叫**「流程线」** —— 我们的数据里
  没有时间戳，按关系方向排出来的是一条**顺序链**，不是时间轴，不假装是。

### 谁来挑模板（三层，手动永远压过前两层）
1. **本地规则（永远在、零成本）**：因果 ≥ 2 且占一半 → 鱼骨图；层级 ≥ 2 且占一半 → 组织架构图；
   依赖 ≥ 2 且占一半 → 垂直流程线；≥ 4 条且起点唯一 → 思维导图；≥ 3 条 → 水平流程线；否则自动。
   理由永远是**数出来的**（例：「因果边占了一半以上（3/3）」），只当建议显示，不自动套用。
2. **模型建议（默认关，设置里勾了才发）**：只发**关系摘要** —— 类型计数 + 若干行
   `起点 类型 终点`（词名），**不带 entry_id、不带上下文、不带释义、不带依据、不带证据、
   不带其它主题**；只要它回一个模板 id + 一句理由。id 不在清单里一律当没给；
   没配 Key / 超时 / 解析失败都**静默回退本地规则**，绝不因此让整张图打不开。
3. **手动**：下拉永远在，选谁就是谁。
* ❌ 不做：让模型直接产坐标（布局是「可复现的算法活」，模型给不准，还每次都不一样）。

### 切模板与「固定位置」（F2 的 pins）怎么相处
* 有固定卡片时先问一句：「切到「X」会取消你固定过的 N 张卡片，它们会按新骨架重新排。继续吗？」
  * 说「不」⇒ **一张都不动、设置也不写**（反馈：「没有切换模板：先按需要保留现在的位置」）。
  * 说「是」⇒ 清掉该主题的固定位置（`node_pins` 落库清空）再重排，反馈里明写
    「（顺带取消了 N 张卡片的固定）」，不偷偷清。
* 固定位置仍然**优先于骨架**：没被清掉的固定卡片待在自己被拖到的位置，别的卡片按骨架排。

### 操作管理（你追加的那条）
* **左键 = 拖动卡片**（拖过就固定位置）；**`Alt` + 左键从一张卡拖到另一张卡 = 建立关系**，
  松在空白处 / 同一张卡上 = 取消（只给一句提示）。
* **按下不再等于动作**：只有移动超过阈值才算「真的拖了」；抖一下鼠标不会固定位置、
  不会说「连线取消」、不会弹任何对话框。
* 卡片上**没有连接柄、没有圆点**（F7 已删干净），所有操作都在「操作说明…」里写着。
* ⚠️ 这一节在 §28 又被改了一次：**左键拖空白处现在也平移整张图**，并且 `人工关系…` /
  `孤立词诊断` / `打开选中的词条` 三个按钮已经内化进操作里（以 §28 为准）。

### 诚实边界
* **结构不符会直说**：例：只有 1 条因果边时选鱼骨图 ⇒ 反馈「这张图不太适合鱼骨图：
  因果边太少（只有 1 条），先按自动排。」+「（「模板…」里可以切回自动）」，按钮仍显示你选的
  骨架（材料变合适了就能用上）；此时画布与纯「自动」**逐点相同**（有测试盯着）。
* 骨架之外的关系边仍走候选搜索绕行 ⇒ 出现「绕很远的长线」是**正常代价**，不假装能画成商业模板。
* 模板只影响屏幕与导出（同一个骨架函数喂两处），导出仍然只有 **SVG + PNG**，没有新格式。
* 骨架全部在**逻辑坐标**里算完，`layout_extent()`（首开自适应）与 `layout_graph()`（绘制）
  透传同一个 `template` ⇒ 不会出现「先排再挪，一开就被裁掉」。

### 实测与回归
* 新增 `tests/test_map_templates.py`（**44 条，纯几何、不建窗口、不联网**）：每个骨架都验
  「一个词都不丢 / 卡片不出留白 / 任意两卡不叠 / 怪图（自环·双向包含·断链）也摆得出来」，
  再各验形状（父子上下、父 = 子树块中心、org 一层一行、单向一路向右、思维导图两支分左右且
  **单根时不假装对称**、鱼骨图主脊水平且按因果序、S 型按链序折返），加本地建议 7 条，
  加真布局集成 6 条（`layout_graph` 回报的模板名、`pins` 优先、鱼骨图拒绝时与纯自动逐点相同、
  `layout_extent` 不低估、**切回「自动」与从未用过模板逐像素一致**）。
* `tests/test_ui_roundrect.py` 新增 `TestConceptMapTemplates`（**21 条**）：按钮文案 = 当前骨架名、
  切模板写设置 + 真的重排 + 边数不变、未知 / 空 / 同名 → no-op 与对应反馈、鱼骨图结构不符的
  提示、**确认框「否」不动「是」清 pin**、对话框清单与「·　当前」标记、本地建议文案、
  模型按钮的开关与 `is_ready()` 返回值拆包、回调经 `after` 回 UI 线程、失败与不认识的名字
  **都不弹窗**（只写状态行）、导出带上同一个骨架名。
* `tests/test_map_export.py` 新增页脚断言：写模板名、顺序「模型 → 模板 → 导出」、
  `template_name=""` 时**不出现**「模板：」、端到端写进 SVG。
* `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0；
  `bootstrap.py` 的 `_DIAGNOSE_MODULES` 补了 `app.ui.map_templates` / `app.ui.template_dialog` /
  `app.ui.shortcuts_dialog`；新设置键 `map.template`（默认 `auto`）与 `map.template_ask_model`
  （默认关）都进了 `DEFAULTS`，老库无需迁移。
* **19 模块白名单回归：Ran 900 / failures=6**（新增 `tests.test_map_templates`；日志
  `_check\tests_after_templates.log`），6 条全是既有陈旧失败（`tests.test_reading_panel` 2 条 +
  `tests.test_unified_action` 4 条，与 §25 / §26 同一批），**没有一条新失败**。
* **仍未实机验收**：9 个骨架在真图上的观感（尤其思维导图左右分列与鱼骨图斜骨）、
  模型挑得准不准、「模板…」对话框在小窗口下的手感、切模板后重排的动画观感。
* exe 需重新打包（`tools\build_app.ps1`）才会带上 `app\ui\map_templates.py` /
  `app\ui\template_dialog.py` / `app\ui\shortcuts_dialog.py`；源码模式（`启动.cmd`）重启即生效。
* 下一批：F4（平滑曲线）/ F5（点阵网格 + `F` 键适应）/ F6（卡片视觉分层）待你点头；
  清单 D 批 / E 批（见 §25 末尾）仍在队列里。

---

## 28. 批次 H：操作管理第二轮（左键平移 / 三个功能内化 / 导图设置内嵌）

### 你这次提的三件事（原话口径）
1. 「鼠标左键不按 `Alt` 的时候是可以自动拖动的……参考一下 ProjectGraph 的操作逻辑。
   就是思维导图的边框是可以进行拖动的。按住 `Alt` 切换成建立关系。」
2. 「如图 2 所示的几个功能框应该都是**内化于实际功能中**的，不需要单独的功能栏显示。」
3. 「思维导图的导出和相关设置都需要在**导图界面**中，主界面不应该显示导图的相关设置。」

### ① 左键拖空白 = 平移整张图（ProjectGraph 式）
* 空白处按下左键 ⇒ 记下 `_bg_pan_last`（**刻意不复用右键的 `_pan_last`**，否则松手会被
  `_pan_guard()` 当成「右键通道还在拖」吃掉）；移动超过 `DRAG_SLOP = 4` 像素才算真的拖，
  之后每帧按位移 `xview_scroll(-dx)` / `yview_scroll(-dy)`，**与右键 / 中键平移同一套视图机制**。
* 平移**不写库、不固定卡片、不建关系、不弹窗**；松手就停（反馈：「平移整张图：松手就停
  （滚轮缩放 · 右键拖动也一样）」）。手抖一两像素既不会固定卡片也不会误判成「连线取消」。
* 命中卡片时 `_bg_pan_last` 直接清空 ⇒ **拖卡片永远是摆位置**，两个手势互不串台
  （有 `test_dragging_a_card_never_pans_the_view` 盯着）。
* 没做「拖到一半松开 `Alt` 就取消连线」：某些 Tk 版本 Motion 事件不带 `Alt` 位，
  误取消比不取消更糟；取消路径仍是「松在空白处 / 松在同一张卡上」。
* 右下角三行提示与「操作说明…」手势表同步改写（`HINT_LINES` / `INTERACTIONS` 现在 11 行），
  第一行开头就是 `按住 Alt + 左键从一张卡拖到另一张 = 建关系 · 左键拖空白处 = 平移整张图`。

### ② 三个功能内化（工具条只留五个入口）
| 原来的按钮 | 现在怎么用 | 代码 |
| --- | --- | --- |
| `人工关系…` | 按住 `Alt` + 左键从一张卡拖到另一张；或双击图上已有的线 | `_on_canvas_press_alt` / `_open_relation_editor_for`（**方法一个都没删**） |
| `孤立词诊断` | **点画布上那一行**「孤立词（候选关系都没通过核对 —— 点这一行看原因）」展开 / 收起，展开后双击某行定位 | `_bind_isolated_label` / `_on_isolated_label_click` |
| `打开选中的词条` | **双击词卡**（或双击左栏词条） | `_on_canvas_double_click` |
* 工具条现在只有：`导出关系图…` / `模板：×××` / `恢复自动布局` / `操作说明…` / **`导图设置…`**。
* 孤立词那一行用 `tags=("isolated-label",)` 建图元再 `tag_bind` —— 假画布没有 `tag_bind`，
  所以走 `getattr(canvas, "tag_bind", None)` 探测，测试里挂一个记录用的替身。

### ③ 导图设置搬进导图窗口（`app\ui\map_settings_dialog.py`，新模块）
* 工具条 `导图设置…` 打开一个小窗：**当前的布局骨架**（`模板：×××`）、`换模板…`
  （与工具条那个按钮同一个对话框）、`恢复自动布局`、以及「打开模板对话框时，也让模型
  看一眼该用哪种排法」的勾选（写的就是原来那个 `map.template_ask_model`）。
* 主界面「设置」里原来那个「关系图」小节（含 `var_ask_template` 勾选框与说明文字）**整段删除**，
  换成一段注释说明口径；保存行同步删除。**设置键没变**，老配置照旧生效，不需要迁移。
* 切模板后 `_sync_template_button()` 顺带刷新设置窗口里那一行（两边永远一致）。
* `bootstrap.py` 的 `_DIAGNOSE_MODULES` 补了 `app.ui.map_settings_dialog`（重新打包排查导入链要用）。

### 实测与回归
* `tests/test_ui_roundrect.py` 226 → **232 OK**（+6）：左键拖空白真的平移了视图且**不固定卡片**、
  抖 2 像素视图不动、拖卡片绝不平移、工具条不再有那三个按钮（五个按钮文案逐个断言）、
  孤立词那一行自己就能开合诊断、导图设置窗口活在导图窗口里（主界面设置里
  `var_ask_template` 已不存在 / 勾选写库 / 两个回调）、切骨架后设置窗口那一行跟着变；
  另改 1 条旧断言（「没有右键按下的 Motion 不许平移」）并更新模块 docstring。
* `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **19 模块白名单回归：Ran 906 / failures=7**（900 + 6 条新用例；日志
  `_check\t_ops_whitelist.log`）。7 条 = 6 条既有陈旧失败（`tests.test_reading_panel` 2 条 +
  `tests.test_unified_action` 4 条，与 §25 / §26 / §27 同一批）**+ 1 条环境性**失败
  `test_reading_panel.test_root_is_withdrawn_before_build_app` —— 桌面上正跑着打包版
  `探索词典.exe` 时单实例互斥体让它拿到 `EXIT_ALREADY_RUNNING = 5`，关掉那个实例即恢复 6 条。
  **没有一条新增失败**。
* **仍未实机验收**：左键拖空白平移的手感（与拖卡片的分界是否舒服）、`Alt` 拖拽的手感、
  点「孤立词…」那一行展开的观感、`导图设置…` 小窗的排版。
* exe 需重新打包（`tools\build_app.ps1`）才会带上 `app\ui\map_settings_dialog.py`；
  源码模式（`启动.cmd`）重启即生效。
* 下一批：F4（平滑曲线）/ F5（点阵网格 + `F` 键适应）/ F6（卡片视觉分层）待你点头；
  清单 D 批 / E 批（见 §25 末尾）仍在队列里。

---

## 29. 批次 I：右下角操作提示「可折叠 + 无底板」（P0-1）

### 你的原话（口径）
> 右下角的提示你改成可折叠起来的，用户刚刚打开时展开进行提示，过 20 秒之后淡出折叠。
> 这个背景要是透明的，不要给提示背景板设置颜色。

### ① 折叠（`HINT_EXPAND_MS = 20000` → `HINT_COLLAPSED_TEXT = "操作说明"`）
* 刚打开三行全展开，右对齐钉在画布右下角；用 `place(relx=1.0, rely=1.0, anchor="se", x=-theme.px(HINT_INSET), y=-theme.px(HINT_INSET))` 做**浮层**而不是画布图元 —— 平移 / 缩放时视图在动，提示必须待在窗口角上不动。
* 20 秒到点后**淡出**收成一行 `操作说明`：每 `HINT_FADE_STEP_MS = 90` 毫秒走一格、共 `HINT_FADE_STEPS = 6` 格（540ms 走完）。
* 折叠后那一行是 `cursor="hand2"` + `<Button-1>` 绑定，**点它就再展开**；这一次**不再自动收**（闩 `_hint_user_collapsed`）—— 这是「用户自己要看」与「自动提示」的分界。
* 关窗第一件事 `_cancel_hint_fade()`：定时器排在 `self.win` 上，窗口销毁后再触发会去打已没的控件。

### ② 「淡出」是怎么做出来的（tk 没有透明度的实话）
* tk 的 `-alpha` 是**整窗**属性，套在导图上会把整个导图窗一起变半透明 ⇒ 不能用。做法是**逐格把文字色混向画布底色**：`_hint_fade_color()` 自己拆 `#rrggbb` 三个通道做线性插值（`app\ui\theme.py` 只给色值字符串，没有拆通道的公开函数），起点色记在 `_hint_fade_base` —— 不记的话每一格都在上一格的结果上再插值，颜色会越走越偏。
* 定时器统一走 `getattr(self.win, "after_cancel", None)` 探测（极简替身 / 已销毁窗口上可能没有）。

### ③ 无底板（去掉 F7 加的那层底）
* `self.hint_box = tk.Frame(self.canvas, bg=self.hint_tint())`，`hint_tint()` **直接问画布**要底色（`self.canvas.cget("bg")`，问不到退回 `theme.BG`）⇒ 以后画布换色提示自动跟着换。
* **删掉** `highlightthickness=1` 与 `highlightbackground=theme.BORDER`；三行 Label 的 `bg` 也是画布底色。皮上只剩文字，没有「提示背景板」。
* **与 F7 的出入（如实记录）**：§F7 ① 的真因是提示浮层与画布**取同一个颜色**（都 `theme.PANEL = #F9F8F6`），当时的修法是给它 `CARD_BG` 底板 + 1px 细边（`app\ui\concept_map.py` 那段注释也写着）。这次口径明确要求去掉底板，就按**新口径**实现；代价是压在卡片上时略挤，折叠后只剩一行小字，影响小。

### ④ 实施中自查出的两个真 Bug
1. **`_restart_hint_fade()` 把用户闩清掉了**：它原本 `self._hint_user_collapsed = False`，而那正是 `_on_hint_rows_click()` 刚设上的闩 ⇒ 点开之后又被定时器收走，**点了像没点**。改成这个方法**不碰**该闩，并在 docstring 里写清为什么。
2. **`_show_hint` 被定义了两次**（`app\ui\concept_map.py:2670` 与 `:2795`），后一份覆盖前一份 —— 死代码占着名字。删掉重复那份；另用 AST 扫过整个模块，确认没有别的重名方法（`MapMetrics` / `LayoutNode` / `LayoutEdge` / `LayoutGroup` / `MapLayout` / `ConceptMapWindow` 六个类都干净）。

### ⑤ 假 Tk 环境补强（`tests\support.py`）
* `FakeTkWindow`（导图窗 `self.win` 的真身，`_FakeTkEnv` 把 `tk.Toplevel` 换成它）**原本没有 `after` / `after_cancel`**，产品里 `getattr(self.win, "after", None)` 的兜底会静默跳过 ⇒「到底有没有排上定时器、延时是不是 20 秒」在测试里**永远测不出来**（等于拿替身的缺口替产品背书）。现补上，登记进 `after_calls` / `after_cancelled`。
* `FakeWidget.after` 原本是 `return None`（**静默丢弃定时器**），现在也登记。
* 两处都让 **`ms == 0` 的回调立即执行**：真实 Tk 的 `after(0, …)` 就是「下一轮事件循环立刻做」，而 `settings_dialog._finish_connection_test()`（测试连接的反馈行）与 `concept_map` 的「异步结果回 UI 线程」都走这条路 —— 只登记不执行，反馈行永远是空的（`test_a_successful_test_writes_the_result_into_the_feedback_line` 会直接挂）。延时 > 0 的仍只登记，由测试自己驱动。
* 测试里还有一条**假 Tk 与真 Tk 的语义差**要在用例里显式补齐：真 Tk 里被 `after_cancel` 掉的那一拍**永远不会响**，假 root 只是把回调记在列表里 ⇒「点开之后不许再自动折叠」这条用例要手动 `win.win.after_calls.clear()`，否则会把上一轮的陈货当成「又自动收了一次」。

### ⑥ 实测与回归
* `tests\test_ui_roundrect.py` 232 → **238 OK**（+6 条 `TestConceptMapHintFold`）：刚打开就是三行 + 排了 20000ms 的定时器 / 跑完定时器收成一行 / 淡出真的逐格挪向画布底色（混色单调性与端点）/ 折叠那一行点得开且**点完之后不再自动收** / 底板与三个 Label 的 `bg` 都是画布底色且 `highlightthickness = 0` / 关窗把待执行的定时器取消掉。另**改 1 条旧断言**（原断言有 `CARD_BG` 底板 + 1px 边框，按新口径反转）。
* `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **19 模块白名单回归：Ran 912 / failures=6**（906 + 6；日志 `_check\p0_1_whitelist.log`）。6 条全部是既有陈旧失败（`tests.test_reading_panel` 2 条 + `tests.test_unified_action` 4 条，与 §27 / §28 同一批）—— 上一轮那条**环境性**失败 `test_root_is_withdrawn_before_build_app` 这次没出现。**没有一条新增失败**。
* **仍未实机验收**：20 秒淡出的手感（540ms / 6 格够不够顺）、折叠成一行后点开的观感、提示压在卡片上时字看不清到什么程度（「去掉底板」的已知代价）、不同屏幕尺寸下右下角的位置。
* **仍未重新打包**：本轮只改源码。打包版 exe 里的 `app\ui\concept_map.py` 是**旧副本**（见 §4.1 的加载顺序：exe 内 `bootstrap` 冻结、`app\` 走磁盘），要看效果请用源码模式 `启动.cmd`，或先跑 `tools\build_app.ps1`。
* 下一批：**F4**（关系线平滑曲线）/ **F5**（点阵网格 + `F` 键适应）/ **F6**（卡片视觉分层）仍等你点头；
  另有两笔已记下的欠账：**P0-2** 重新打包 exe、**P0-3** 左键拖空白平移的实机复核
  （上一轮交接里猜的「用户看的是旧 exe」已被排除：那台机器上跑着的 exe 启动于批次 H 之后，
  `app\` 走磁盘、代码是新的 ⇒ 若仍然没效果，按下点大概是落在卡片上了）。

## 30. 批次 J：按钮悬浮说明 + 搜索框占位提示（P0-4）

### 你的原话（口径）
> 我希望你为功能框加入简单说明，比如光标悬浮在功能框上时显示简短的说明。
> 还有就是搜索框可以加入可以搜索什么内容的提示，用户开始输入后消失。

### ① 悬浮说明：`app\ui\widgets.py` 的 `Tooltip` / `tooltip()`
* 悬停 `TOOLTIP_DELAY_MS = 450` 毫秒才弹（太快会一路闪小条），弹的是**无框小窗**：`tk.Toplevel` + `overrideredirect(True)`，外层 `bg=theme.BORDER` 当 1px 细线、内层 Label 米色底（`theme.PANEL` / `theme.TEXT_BODY` / `theme.font(8)`），方角无阴影，沿用编辑杂志风。
* 鼠标一走（`<Leave>`）或按下去（`<Button-1>`）立刻收掉，并且**取消还没弹的那一拍**（`_cancel_timer()` → `getattr(self.widget, "after_cancel", None)` 探测后调）—— 否则鼠标扫过按钮，过一会儿还会凭空冒出一句话。
* 位置是**控件正下方居中**，不跟光标：`_place()` 读 `widget.winfo_rootx/rooty/width/height`，下面放不下就翻到上面，再夹进屏幕（`TOOLTIP_EDGE = 6`）；`TOOLTIP_GAP = 8` 是控件与说明之间的空。说明要跟它解释的那个按钮**对齐**，跟着光标跑会让人看不出在说哪个按钮。
* `Tooltip` 的文案**支持 callable**（每次弹出重新取）：`self.btn_explain` 会在 `解释` / `重试` / `重新解释` 三态之间切，硬写死就会出现「按钮写着重新解释、说明还在讲第一次解释」。
* **没有用 `ttk`**，也没有第三方库：冻结运行时里没有 `tkinter.ttk`（见 §4.2，`tests\test_runtime_recovery.py:694-742` 有 AST 护栏）。文案集中在 `app\ui\main_window.py` 的 `BUTTON_TOOLTIPS`（**22 条**，键就是按钮上那句话）。

### ② 说明挂在哪 22 个入口上
* 工具条 8 个（`搜索` / `清空` / `导出` / `导图` / `设置` / `游戏模式：关` / `暂停取词` / `退出`）：工具条改成局部工厂 `_toolbar_button()` 收集进 `toolbar_buttons` 列表，建完**统一挂**说明（列表里没登记的文案，测试会报）—— 这样以后新增按钮不会漏。
* 主题区 4 个 + 标签区 3 个：抽了模块级 `_small_button()`（建按钮 + pack + 按文案自动挂说明）。
* 中部 2 个（`手动录入` / `剪贴板导入`）、详情右栏 3 个（`保存修改` / `解释` / `删除词条`）、告警条 1 个（`重试注册`）、以及每个 `+标签` 芯片（「把「X」填进标签框（还要点「保存修改」才生效）」）。
* 文案口径的例子：`导出`「把当前看到的词表导出成 CSV + Markdown 两份（写进 data\exports\，不发网络请求）」、`导图`「打开参考关系图：看词与词之间的参考关系（图里的生成要单独点，会消耗额度）」、`退出`「退出探索词典（主窗的 × 只是收起，后台继续取词）」。**不写空话**（「点击此按钮」这种等于没写）。
* 搜索框本身也有一条：`SEARCH_TOOLTIP` = 搜索算命中哪些字段 + 有搜索词时按**整库**找（不再是当前主题）。写的时候把 Markdown 的 `**` 去掉了 —— tooltip 是纯文本 Label，会原样显示星号。

### ③ 搜索框占位提示：`app\ui\widgets.py` 的 `placeholder()`
* 空框显示 `SEARCH_PLACEHOLDER = "搜词语 / 上下文 / 来源"`（浅一号字色 `theme.TEXT_FAINT`）；点进去 / 敲任意键（含中文输入法落字)/ 框里有真内容就消失；空着离开焦点再回来。
* **占位文字绝不写进 `search_var`**（docstring 里写在最前）：写进去就等于**真的拿一句提示去查库** —— 查不到东西，用户还会以为检索坏了。占位只动 Entry 的**显示缓冲**与字色。
* 提示里那三个字段不是随口写的，是照着 `app\db.py` 的 `list_entries` / `_entries_where` 那 8 个 `LIKE`（`term` / `context` / `one_line` / `detail` / `source_title` / `source_url` / `examples` / 标签名）与 `app\search_service.py:18-27` 的 `FIELD_ORDER` 定的。
* 参数名与按钮文案**一个字都没改**（`tests\test_reading_panel.py:3826-3865` 的按钮审计表按 `cget("text")` 找控件、断言回调的 `__self__` / `__name__`，还断言「设置 / 暂停取词 / 游戏模式：关 / 退出」在界面上只出现一次）—— 这轮只是给它们挂说明，不动行为。

### ④ 假 Tk 环境的三处补强（`tests\support.py`）
1. **`FakeWidget` 原本没有 `winfo_rootx` / `winfo_rooty`**，`winfo_height()` 恒返回 0 ⇒「把说明摆到控件正下方」的定位计算在假环境里直接抛 `AttributeError`（或算出荒唐坐标）。已补 `winfo_rootx/rooty`（100/100，与 `FakeTkWindow` 同口径）与 `winfo_screenwidth/screenheight`（1920/1080），`winfo_height()` 改回 22。
2. **假控件的 `bind()` 会顶掉先绑的回调**（原本 `binds[sequence] = func`）：搜索框上先绑的占位提示 `<Button-1>` 会被后绑的说明气泡**整个换掉** ⇒「点进搜索框、提示没消失」这种真问题在假环境里根本红不了。新增 `_chain_binds()` / `_ChainedBinds`，同一事件的多条绑定按登记顺序依次执行（单条绑定时存的就是原回调 `binds[seq](event)` / `binds.get(seq)` 照旧可用，老用例不用改）。
3. **`_FakeTkEnv._make_toplevel` 原本把 `**kw` 全丢了**（`FakeTkWindow(self.width, self.height)`）⇒「说明小窗有没有自己的底色 / 内边距」这类断言无从查起；现在原样透传，`FakeTkWindow` 记进 `self.kw`。

### ⑤ 实测与回归
* `tests\test_ui_roundrect.py` 238 → **249 OK**：+`TestButtonTooltips` 5 条（工具条每个按钮都挂全 `<Enter>/<Leave>/<Button-1>` 且在表里 / 表里 8 个工具条键都能在界面上找到 / 悬停排的是 450ms 定时器且到点真弹出（`overrideredirect`、文案逐字对得上）、移开鼠标收掉**并取消还没弹的那一拍** / 每个按钮的说明各不相同且弹出的小窗里逐字对得上 / `解释·重试·重新解释` 三态各有说明）+`TestSearchPlaceholder` 6 条（空框显示提示且 `search_var` 为空、字色浅一号 / 点进去提示消失且字色回正常 / 敲键（含输入法落字）提示消失且搜索照常 / 空着 `<FocusOut>` 提示回来 / 框里有真内容时离开焦点**绝不被提示盖掉** / 刷新列表也不把提示写进 `search_var`）。
* `compileall -q app tools tests bootstrap.py desktop_entry.py` 退出码 0。
* **19 模块白名单回归：Ran 923 / failures=7**（912 + 11；日志 `_check\p0_4_whitelist.log`）。7 条全部是既有陈旧失败：`tests.test_reading_panel` 3 条（其中 `test_root_is_withdrawn_before_build_app` 是**环境性**的 —— 桌面上正跑着打包版 exe，单实例互斥体给出 `EXIT_ALREADY_RUNNING = 5`，关掉即恢复 6 条）+ `tests.test_unified_action` 4 条。**没有一条新增失败**。
* **仍未实机验收**：450ms 的悬停手感、说明小窗在不同 DPI 下的位置与字号、长文案（`导出` / `导图` 那两条跨两行）的观感、`+标签` 芯片的说明是否嫌啰嗦、搜索框占位文字与真输入的字号观感。
* **仍未重新打包**：本轮只改源码（`app\ui\widgets.py` + `app\ui\main_window.py`），打包版里仍是旧副本（见 §4.1：exe 内 `bootstrap` 冻结、`app\` 走磁盘）。要看效果用源码模式 `启动.cmd`，或先跑 `tools\build_app.ps1`。`tools\ui_preview.py`（离线设计预览）本轮没动。
* 下一批：**F4**（关系线平滑曲线）/ **F5**（点阵网格 + `F` 键适应）/ **F6**（卡片视觉分层）仍等你点头；
  另有两笔已记下的欠账：**P0-2** 重新打包 exe、**P0-3** 左键拖空白平移的实机复核
  （上一轮交接里猜的「用户看的是旧 exe」已被排除：那台机器上跑着的 exe 启动于批次 H 之后，
  `app\` 走磁盘、代码是新的 ⇒ 若仍然没效果，按下点大概是落在卡片上了）。

---

## 31. 批次 K：勾选 = 「要进导图 / 要拆走的词」结果

> 需求原话（2026-10-05）：勾选框应该是**可进入导图的关键词** —— 录了很多词、最后总结时只要几个关键术语做导图/导出，所以**默认全部勾选**，并在合适位置加 `全选` / `取消全选`。验收口径经确认：**勾选只影响导图与拆分**，导出维持现状（导当前范围）。

### 31.1 三件事

1. **默认全勾**：进一个主题时该主题的词条全部勾上（主界面 `_refresh_checked_scope()`）；手动取消的不会被刷新补回来；搜索 / 标签筛选只换范围、不重置。范围用新加的 `db.list_entry_ids()` 算 —— 只查 id、**不受 `MAX_CARDS` 分页限制**，所以屏幕上没画出来的词（第 301 条以后）也算在内。
2. **`全选` / `全不选`**：中栏 `head` 行两个小按钮，勾选提示那一行本身也能点（= 全选）。文案：`已勾选 N / M 条` / `本组 M 条已全勾` / `点这里全选`。
3. **导图按勾选筛**：`导图` 按钮把勾选集交给图窗，复用 C3 就有的子集机制（`ConceptMapWindow._subset_ids` + `_set_subset()`）；**全勾 = 不设子集**（等于整个主题，不白多一份缓存与提示）。顺带把图窗挑主题的逻辑改成**跟着主界面正在浏览的主题**——否则图跑去画「最近更新的那个主题」，勾了半天等于没勾。

### 31.2 四个真 bug（自己写的，靠测试 + 探针才露出来）

- **`App` 上没有 `main_window`**：真名是 `self.main`。两处写 `getattr(self.app, "main_window", None)`，`getattr` 的兜底把错误吃掉了 ⇒ **不报错、功能就是不生效**（导图不做筛选；双击词卡回主界面也不切主题）。已改对，并加只读别名 `self.main_window = self.main`。
- **搜索被当成换主题**：查库用 `bool(tag or query)`、重置判据用 `entry_scope()`，两者在「有搜索词 + 正在浏览主题」时不等（`(1, True)` vs `(1, False)`）⇒ 每搜一次就把取消的勾补回来。统一走 `_checked_view_key()`。
- **搬完家又把勾勾回来**：`apply_move` 改完 `_browse_batch_id` 后下一次刷新会重跑「换主题 = 全勾」。现在改完立刻对齐视野键。
- **主题还没定时算子集**：构造期 `_topic_id is None` ⇒ `int(None)` 抛 `TypeError`。现在 `_set_subset()` 没主题就返回 False，范围寄存到 `_pending_only_ids`，由 `_load_topic()` 再套。

### 31.3 两条守卫

- **交集为空绝不生效**：勾的是别的主题的词、或库里的词都删光了 ⇒ 退回「全部」，绝不给一张空图（看起来像主题坏了）。
- **子集不跨主题沿用，但主界面刚设的那个要保住**：`_only_topic_id` 记「这个子集是本主题的」；删词之后子集被裁成空 = 回到全部，不会再被「保住」那一支反复抖回去。

### 31.4 测试

- 新增 `TestConceptMapCheckedFilter` **5 条**（只画勾上的 / 全勾 ≠ 子集 / 没解释的词也不炸 / 别的主题的词不给空图 / 删掉的词掉出子集）。
- `TestLibraryOrganizationControls` 扩到 20 条：默认全勾、取消不回来、换主题重新全勾、搜索不重置、320 条时屏幕外的也归全选管、全勾拆分先问一句。
- `tests.test_ui_roundrect` **Ran 257 OK**；三模块 **Ran 345 OK**；`tests.test_db` **Ran 71 OK**。
- 19 模块白名单：**Ran 931 / failures=6**（全是既有陈旧失败，无新增）。
- `compileall` 退出码 0。

### 31.5 仍未做

- **没有重新打包 exe**：本轮只改源码，要用上勾选筛选请走源码 `启动.cmd`。
- 实机手感未验收：几百条的主题里「默认全勾」会不会让人误会；`全选 / 全不选` 够不够显眼。
- 批次 H / I / J / K 的改动**还没提交、没推送**。


## 32. 批次 M：导出多格式（八选一）+「设置 → 导出」保存位置

> 需求原话（2026-10-05）：导出内容要支持这几种文件格式，用户可以进行选择。你可以把主界面的导出和导图导出的文件进行分类。
> 附图列了六种：CSV / JSON·JSONL / Anki 导入文本 / PDF / HTML 单页 / 纯文本 TXT —— 连已有的 Markdown 一共**八种**。

### 32.1 两份导出就此分类

| 从哪导出 | 出什么 | 说明 |
| --- | --- | --- |
| 主界面工具栏 `导出` | **八选一**：CSV / Markdown / JSON / JSONL / Anki 导入文本 / PDF / HTML 单页 / 纯文本 TXT | 先弹一个小窗选格式，**选完只写这一个文件**（以前固定写 CSV + Markdown 两份） |
| 导图窗口 `导出` | 完整 **PNG** + 可编辑 **SVG** | 一行没改：图就该是图 |

选格式的小窗（新文件 `app\ui\export_dialog.py`）只做三件事：选格式 / 告诉你存到哪 / `导出`·`取消`。
每行右边的小字写清「给谁用」，比如 CSV 是「表格；Excel 双击不乱码（带 BOM），也能导进 Anki」，
Anki 那行是「制表符分隔、无表头、末列是 `#标签` —— Anki『导入文件』直接认」。
**记住上次用过的格式**（`export.format`），下次打开就停在那一项；没选过就是 CSV（与老行为一致）。

导出范围没变：**跟着当前筛选走**（主题 / 标签 / 搜索词），**与勾选无关**；同名绝不覆盖（自动 `-2`）。

### 32.2 存到哪：设置里多了一段

- 新设置项 `export.directory`，界面上是 `设置 → 导出 → 保存到`（输入框 + `恢复默认` + 一行说明小字）。
- **留空 = 默认目录** `data\exports\`（`app\paths.py` 的 `exports_dir()`）—— 不设就跟以前完全一样。
- 填别的目录（含 `%USERPROFILE%\Desktop` 这类环境变量）就在**按「保存」时当场试建**：
  建不出来（盘符不存在 / 没权限 / 路径非法）在设置窗底部说清楚，**不把坏路径存进去**，旧设置不受影响。
- 导图导出**不受这个设置影响**（仍在 `data\exports\`），与「导图维持出图」的口径一致。

### 32.3 PDF 是自己写的：不装库、不联网

口径是「手写精简 PDF」（本机也确实没有 reportlab / pypdf），于是新增两个只依赖标准库的模块：

- `app\pdf_writer.py`：A4 纵向、自动折行 + 自动分页、页脚「第 n / N 页」、标题 / 小节 / 正文三级字号；
  来源链接、来源应用、捕获时间、重复次数走灰色小字。
- `app\ttf_subset.py`：把系统里的中文字体**按实际用到的字**做成子集再嵌进 PDF
  （`simhei.ttf` 9.7MB → 一份 10 条词的 PDF **约 60–90KB**，字体流约 27KB；生成耗时约 **50ms**）。

两条实现取舍（以后别推翻）：
1. **字形编号保持原样、不重编号** —— 复合字形的部件引用可以逐字节照抄，代价只是 `loca` 里留空洞（不占体积）。
2. **cmap 只重建一条 format 4**（BMP 够用），按「码点差 == glyph id 差」切段，同段 `idDelta` 恒定。

找不到中文字体也不崩：退化成只写拉丁字母的 Helvetica，文件照样能打开（可用环境变量 `EXPLORER_DICT_PDF_FONT` 指定字体）。

### 32.4 抓到一个会「白给」的 bug

第一版 PDF 生成完自测是「绿的」，但换了个角度看才发现问题：

- **`FontDescriptor` 漏了 `/FontFile2`** —— 字体流确实写在文件里（第 6 号对象），可**没人引用它**。
  在**本机**打开看不出差别（系统字体里什么字都有），**换台电脑打开就是方块**。
- 更值得记的是**为什么第一版没测出来**：早先的探针只查「有没有那段流」，**没查有没有人引用它**。
  教训：验证要看**引用链**，不是「存在性」。
- 修法：`descriptor` 里补 `/FontFile2 %d 0 R`（指向 `base + 3`），顺手把 `upem` 抽出来给 FontBBox / Ascent 复用。
  修完 PDF 反而从 63,764 B 缩到 **59,896 B**（引用补上后 zlib 压得更顺）。

### 32.5 验证（这一批花了大力气在「真的能打开」上）

- **PDF 结构对拍**：字体描述指向 `/FontFile2 6 0 R`；`/Length 27587` 与实际字节一致、`/Length1 132416`；
  解压后是 `\x00\x01\x00\x00` 开头的真 TrueType（28562 字形）；**ToUnicode 89 条里 88 条与嵌入字体 cmap 逐条一致**
  （唯一的 gid 0 是空位）；内容流 86 个字形全部查得到 Unicode；版式越界 0。
- **版式体检**：从内容流里抠出每一行 `Tm` / `Tj` 坐标 —— 4 页、135 行、字号 `[8.0, 9.5, 11.5, 17.0]`、
  每页行数 `[40, 41, 41, 13]`、页脚 y=30.0，**无文字越界、无行间贴脸**。
- **整页图复看**：把内容流当排版指令、用 PIL + 嵌入字体逐字画成 4 张 PNG（120 DPI）。
  逐页看过：标题、灰字元信息、编号词条小节、英文长句自动折行、中英混排、分页、右下角页脚全部正常。
  ⇒ PDF 版式已是「肉眼级」确认可用。
- **真数据样例**：真库 10 条词 × 八种格式各落一份在 `D:\探索工具\_check\m_export\`（供肉眼验收）。

### 32.6 测试与回归

- 新增测试 **19 条**：
  - `tests\test_export_service.py` 10 → **Ran 17 OK**：八种格式各写**一个**文件且后缀对、HTML 自包含单页且带 charset、
    Anki 制表符无表头、JSON 的标签 / 例子是数组、JSONL 一行一条、**PDF 真的把字体带在身上**、
    找不到字体时的降级、未知格式退回 CSV、目录解析与环境变量展开、引号容忍。
  - `tests\test_ui_roundrect.py` 257 → **Ran 269 OK**：点 `导出` 先弹选择器（光打开不落盘）、八种格式端到端、
    选择器清单与 `export_service.FORMATS` 同源、**连点只导一次**、`export.directory` 决定落盘目录、
    设置里回填 / 恢复默认 / 坏路径拒收 / 环境变量展开。
- **19 模块白名单：Ran 950 / failures=6**（`_check\batch_m_whitelist.log`）—— 6 条全是既有陈旧失败
  （`tests.test_reading_panel` 2 + `tests.test_unified_action` 4），**无新增**。

### 32.7 已知的小事

- PDF 里遇到**零宽空格**（U+200B，来自网页正文的 `source_title`）会占一个「无字形」的空位 ——
  它本来就不可见，渲染看不出差别，只是复制出来的文字里会少它。
- 「Anki 导入文本」与「纯文本 TXT」后缀都是 `.txt`：同一批导出里第二份自动带 `-2`，
  这是「同名绝不覆盖」的既有规则。

### 32.8 仍未做

- **没有重新打包 exe**（老欠账 P0-2）：要用上导出选择器请走源码 `启动.cmd`。
- 实机手感未验收：八行格式清单够不够好读、小窗尺寸、记住上次格式是否顺手。
- 批次 H / I / J / K / L / M 的改动**仍只在本机**（本地有还原点，等你说「统一上传」再推）。

## 33. 批次 M 补记：PDF「乱码」的真凶是 `/W` 宽度数组

第一版样例 PDF 在 WPS 里是灾难现场：英文词从中间被劈开、句子右边直接消失。用户截图报障：「所有文件必须是人能看懂的」。

### 33.1 现象与误判

一开始怀疑三个方向，全被证据否掉：

- **折行算错了？** 不是。`wrap_text()` 算出来 441.8 / 489.2 / 394.2pt，PIL 真渲染 446.4 / 494.4 / 398.4pt，**本来就是对得上的**。
- **空格丢了？** 不是。SimHei 的 `units_per_em = 256`，空格 gid = 3、advance = 128（= 0.5em，9.5pt 下 4.75pt），一直在。
- **编码/字体坏了？** 不是。ToUnicode 映射逐条与嵌入字体的 cmap 一致，复制出来的文字正确。

### 33.2 真凶

`/W`（CID 宽度数组）的语法是「从这个 CID 起**连续**这么多个宽度」：

```
/W [ 3 [500 500 500 …] ]     ← 我原来写的：连号字形按 100 个一组打包
```

这行实际含义是「从 CID 3 开始，连续 N 个字形宽度都是 500」，可我只写了 100 个数、却以为它们是 100 个**不同**的字形 ID 的宽度。于是：

- 只有前 60 个字形（组 1 起始 CID 3）拿到正确的 500；
- **第 61 个字形之后全落进 `/DW 1000`**（默认宽度 = 1em）；
- 1em 是 0.5em 的两倍 ⇒ 每行写到第 60 个字就被推出右边界裁掉。

同一行里，前面的字被撑开、后面的字消失 —— 看上去就是「乱码」。

### 33.3 修法

一个字形一条：

```python
glyphs = sorted(self._used_glyphs)
widths = b" ".join(
    b"%d [%d]" % (g, self.font.advance(g) * 1000 // self.font.units_per_em)
    for g in glyphs
) or b"0 []"
```

`app\pdf_writer.py` 里配了 9 行注释讲这次事故，免得以后有人再「优化」成范围组。

### 33.4 验证

| 手段 | 结果 |
| --- | --- |
| 逐行重算终点 | 最右一行 549.3 = 右边界 549.3，**越界 0 行** |
| 内容流还原文本 | 135 行，空格正常 |
| 按 `/W` 重新渲染成图 | 标题 / 灰字元信息 / 编号词条 / 英文折行 / 中英混排 / 页脚「1 / 4」全对 |
| 八种格式逐份翻看 | 另外七种本来就正常（CSV 带 BOM + 引号、JSONL 一行一条、Anki 三列制表符、TXT 带编号、HTML 自包含） |

### 33.5 两条守卫测试（都证明过能打红）

```python
test_pdf_width_array_has_one_entry_per_glyph      # 每个 /W 条目只许一个宽度
test_no_pdf_line_runs_past_the_right_margin       # 照阅读器方式算，没有一行越界
```

`.tmp\prove_guard.py` 把 `_font_objects` 临时改回范围组 ⇒ `FAILED (failures=2)`，两条都红；恢复后绿。

### 33.6 又一个「验证要看引用链」的教训

第一版样例生成于修 `/FontFile2` **之前**，`FontDescriptor` 里没有 `/FontFile2` —— 字体流写进文件了，但没人引用它。**两条 bug 叠加**才让用户看到那幅惨状。这和 §32.4 是同一个教训的第二次出现：**存在 ≠ 被引用**。

### 33.7 仍然存在的「看不懂」

用户截图里的 `assur-ances`、`adapt-ing`、`resource-con-strained`、`high computationa` 这些断字，**是库里的原文本身**（划词/OCR 抓取时就带着连字符换行与截断）。八种格式都是照着原文导的，导出功能没动过它们。要修得在**导出时做一层清理**（去连字符换行、按原词重接），或在**划词时**就别抓半截词 —— 这属于另一批。

### 33.8 回归

`tests.test_export_service` 17 → **Ran 19 OK**；逐字 19 模块白名单 **Ran 952 / failures=6**（`_check\batch_m2_whitelist.log`；6 条仍是 `test_reading_panel` 2 条 + `test_unified_action` 4 条，无新增）。

## 34. 批次 M 补记：PDF 版式照 HTML 骨架重排

> 需求原话（2026-10-05）：**「优化一下排版好吗？我看着很不舒服。」**
> 追问后确认口径：**「HTML 的排版很好，主要问题是 PDF」** —— 不再自己发明版式，照 HTML 单页那版来。

### 34.1 从 CSS 到 PDF：0.66 比例映射

| HTML 单页那版 | PDF | 换算 |
| --- | --- | --- |
| `h1 { font-size: 22px }` | 标题 17.0pt / 行高 24.0 | 页面 900px 宽 → 503pt（可用正文宽） |
| `article h2 { font-size: 16px }` | 词条标题 12.5pt / 行高 17.5 | 16 × 0.66 ≈ 10.6，取 12.5 更精神 |
| `.meta { 13px #6E6C67 }` | 元信息 8.2pt 灰 | |
| `dl dt { 12px #6E6C67 }` | 字段名 8.2pt 灰 | |
| `dd { font-size: 默认 }` | 字段值 9.0pt | |
| `.tag { 12px, 圆角药丸 #EDEBE6 }` | 标签药丸 8.2pt（矩形 + 两端贝塞尔圆头） | |
| `article { border: 1px #E3E1DC }` | 词条之间一条 `#E3E1DC` 细横线 + 标题左侧 2.4pt 小色块 | PDF 里没有卡片底色和圆角 |

结构完全照搬：`标题` → `灰字元信息（范围｜条数｜导出时间）` → `一行说明` → `细横线` →
每条词条一个块（`编号 + 词` 的标题、标签药丸行、`字段名小灰字` + `值缩进挂在下面`）。

**试过又弃用的**：同一行画两遍、错开 0.28pt 冒充粗体。真 Tk 里看着还行，PDF 里**糊成重影**。
现在层级只靠**字号 + 颜色**（`app\pdf_writer.py` 的 `FAUX_BOLD_SHIFT = 0.0` 留着当标记）。

### 34.2 词条不被页码劈开（对应 HTML 的 `break-inside: avoid`）

`PdfDoc` 多了一个**量高模式**：`dry_run = True` 时不真分页、不往页上落指令，只把游标拉回页顶并记账；
`PdfDoc.measure(render)` 返回 `(used, pages)`。导出前**先量整条多高**：

```python
start_y = doc.y() - MARGIN_BOTTOM
used, pages = PdfDoc.measure(render_card)
if used <= start_y:      moved = False          # 本页装得下
elif used <= one_page:   moved = start_y < used * SPLIT_IF_ROOM_RATIO   # 52%
else:                    moved = False          # 自己就超过一页，只能顺着排
```

即：本页剩下的空间**不足整条高度的 52%** 就整条挪下一页；剩得多就让正文接着流
（页底空一大块比断一下更难看）。超过一页的长词条没办法，顺着排。

**量高必须与当前位置无关**（这两个坑都踩过、都写进注释了）：

1. 拿 `start_y - 量完的 y` 当高度 —— 中途翻页后游标已经回到页顶，量出 **-466.99**，「放不下」判断全失灵；
2. 把中途累计的用量再加一遍 —— 同一条词条量出 **297.7 / 365.9 / 672.6** 三个数（折行随剩余高度变）。

### 34.3 空白页：翻页只能翻一次

`rule(keep_with_next=...)` 自己会「先翻页再画线」，外面那句 `if moved: doc.new_page()` 又翻一页 ——
**中间那页就空了**。真实数据下 10 条词排出 **9 页**，其中 **4 页只有页脚**。
修法：**翻页只交给 `rule()` 做**，外面不许再翻。

顺带治了「线孤零零留在上一页页底」：`rule()` 的判断必须在**画线之前**做：

```python
if keep_with_next and self._y - keep_with_next - 8.0 < MARGIN_BOTTOM:
    self.new_page()
```

### 34.4 两条守卫（都能被旧写法打红）

```python
test_pdf_does_not_leave_blank_pages_between_entries   # 每页都得有相当多的正文
test_pdf_measures_a_card_from_the_top_of_a_page       # 量高不为负、超页卡片页数跟着涨
```

第一条的判据**不是「页数少」** —— 字体不同折行就不同、页数会飘；而是
「**每一页的文字段数都不少于平均值的 25%**」：空白页的文字段数是 0，怎么飘也躲不过。
`.tmp\prove_pagination_guard.py` 把旧写法放回去 ⇒ 打红：

```
AssertionError: 1 not greater than or equal to 7.1875 : 有页面几乎是空的（每页文字段数 [40, 1, 49, 25]）
```

### 34.5 验收

真库 10 条词导出 → **5 页**，`PDF 98122 B`、子集字体 164084 B：

| 页 | 词条 | 文字段数 |
| --- | --- | --- |
| 1 | `[1, 2]` | 44 |
| 2 | `[3, 4]` | 42 |
| 3 | `[5, 6]` | 42 |
| 4 | `[7, 8]` | 43 |
| 5 | `[9, 10]` | 42 |

每页都是**完整词条、无空白页**。一条词条约 300pt、一页可用 735.89pt ⇒ 一页正好两条，
**页底留白是几何必然**（三条要 900pt，装不下）。逐字渲染成 5 张图复看过。

### 34.6 回归

`tests.test_export_service` 19 → **Ran 21 OK**（新加 2 条）；
逐字 19 模块白名单 **Ran 954 / failures=6**（`_check\batch_m3_whitelist.log`；
6 条仍是 `test_reading_panel` 2 条 + `test_unified_action` 4 条，无新增）。

### 34.7 仍然没动的

「词条本身是断字」（`assur-ances` / `resource-con-strained` / `high computationa`）**来自库里的原文**，
八种格式都照原文导，§33.7 记着这件事 —— 要修得么在**导出时清理**、要么在**划词时**就别抓半截词，等你定。

## 35. 批次 M8：PDF 的「间隔均匀」

> 需求原话（2026-10-05）：**「排版要整齐，间隔均匀。」**

### 35.1 先量「不齐」在哪

`.tmp\measure_rhythm.py` 给 `PdfDoc.line` 挂钩子，把每行的
`(text, size, indent, color, y, page)` 记下来，再算相邻两行的基线差。结果一张卡片里
有 **七种间距**：

| 相邻的两种行 | 基线差 |
| --- | --- |
| 字段名 → 它自己的值 | 12.20 |
| 值里自己折行 | 12.60 |
| 值 → 下一个字段名（按位置不同） | **15.60 / 16.60 / 19.60 / 20.60** |
| 词条标题 → 第一个字段 | 42.10 |
| 上一条最后一行 → 下一条标题 | 53.10 |

### 35.2 一个模数管三件事

`app\pdf_writer.py` 的常量整块重写：

```python
SIZE_FIELD = 8.6                 # 8.2 → 8.6，元信息不再单独用 7.6 当正文字号
LEADING_HEADING = 16.8           # 12.5 × 1.34
LEADING_BODY = 13.3              # 9.5 × 1.4
LEADING_VALUE = 12.0             # 9.0 × 1.33
LEADING_FIELD = 10.0             # 8.6 × 1.16（下面紧跟的是值，不需要整行行高）
GAP_AFTER_FIELD = 3.6            # 字段名→值、值→下一个字段名、标题→第一个字段：都是它
GAP_LABEL_TO_VALUE = GAP_AFTER_FIELD
GAP_AFTER_HEADING = GAP_AFTER_FIELD
```

`app\export_service.py` 的 `_pdf_card()` 里，元信息字段不再自己一套
（原来 `size=SIZE_FOOT, label_size=SIZE_FOOT-0.6, gap_after=3.0, gap_before=1.0`），
改成和别的字段同字号同间距、**只靠颜色淡一档**。

### 35.3 量出来的结果

| 相邻的两种行 | 改前 | 改后 |
| --- | --- | --- |
| 字段名 → 值 | 12.20 | **13.6**（80 次全一样） |
| 值 → 下一个字段名 | 15.60 / 16.60 / 19.60 / 20.60 | **15.6**（全一样） |
| 正文折行 | 12.60 | 12.0（9.5pt 的行高） |
| 值折行 | 12.60 | 15.6（9.0pt 的行高） |

### 35.4 连带的好处：一页正好两条

卡片从 332.6–345.2pt 瘦到 **305.2–317.2pt**，一页可用 735.89pt 正好装两条
（原来 2 条要 707.5pt、可用只有 671.27pt，装不下，还多出一页只有一行的第 6 页）。
真库 10 条词现在是 **5 页**：页 1 `[1,2]`、页 2 `[3,4]`、页 3 `[5,6]`、页 4 `[7,8]`、
页 5 `[9,10]`，`PDF 98122 B`。

### 35.5 `measure()` 的参照点（这一轮才彻底想明白）

`used` 是「卡片真正的**上边界** → 下一条之前」的整段占地，而游标指的是第一条线的
**基线**。所以：

```python
start_y = doc.y() - pdf_writer.MARGIN_BOTTOM        # 基线到页底，还剩多少
used, pages = pdf_writer.PdfDoc.measure(
    render_card,
    pad_top=pdf_writer.LEADING_HEADING + pdf_writer.GAP_AFTER_RULE)   # 基线之上那一格
```

`pad_top` 补的是**基线之上**那一格（词条标题的行高 + 卡片前那条分隔线的空当），
`start_y` 量的是**基线之下**还剩多少 —— 两边各管一头，合起来正好是整段。
少算任何一笔，判定「刚刚好放得下」的卡片真画时都会从页底溢出一行，那行被甩到下一页。

### 35.6 回归

`tests.test_export_service` + `tests.test_ui_roundrect` = **Ran 290 OK**；
逐字 19 模块白名单 **Ran 954 / failures=6**（`_check\batch_m8_whitelist.log`；
6 条仍是 `test_reading_panel` 2 条 + `test_unified_action` 4 条，无新增）。

## 36. 批次 M9：双击 exe 弹「启动失败」

### 36.1 你看到的现象

双击 `探索词典.exe`，弹一个框：「**探索词典启动失败。** 详情见日志文件：
`D:\探索工具\探索词典\data\logs\startup.log`」。点掉之后什么都没有。

### 36.2 日志里的那一行

`data\logs\startup.log` 里躺着（20:11:10 与 20:11:16 各一次，
末尾 `DONE exit=4 elapsed=0.79s`）：

    File "bootstrap.py", line 499, in run_app
    File "D:\探索工具\探索词典\app\main.py", line 45, in <module>
        from .ui.main_window import MainWindow
    File "D:\探索工具\探索词典\app\ui\main_window.py", line 56, in <module>
        from .. import export_service, search_service
    File "D:\探索工具\探索词典\app\export_service.py", line 31, in <module>
        import html
    ModuleNotFoundError: No module named 'html'

### 36.3 标准库怎么会没有

因为**冻进 exe 的只有「构建那一刻」的那一份标准库快照**，而 `app\` 是**从磁盘
加载**的（`bootstrap.py:43-46`：`ROOT = Path(sys.executable).parent if frozen else
Path(__file__).parent`，再 `sys.path.insert(0, str(ROOT))`）。「改一行 `app\`、
重启 exe 就生效」这条约定因此一直成立 —— 但它有个前提：**这个批次没有新增
import**。批次 M 的导出功能加了 `import html`，而部署的 exe 是 **10-03 10:16:54**
构建的，那天还没有这一行，于是双击就死在 `import app.main`。

### 36.4 为什么所有测试都是绿的

源码模式跑的是系统 Python，标准库齐全；19 模块白名单跑的也是源码。这件事**只有
「双击 exe」才暴露得出来**。

### 36.5 改法：把标准库整个收进 exe

`ExplorerDict.spec` 新增 `_stdlib_hiddenimports()`：遍历
`sorted(sys.stdlib_module_names)`，逐个 `collect_submodules(name)`，平台不支持的包
`try/except` 跳过，最后按 `_STDLIB_SKIP` 过滤去重，接进
`HIDDEN_IMPORTS = _diagnose_hiddenimports() + _stdlib_hiddenimports() + [...]`。
「只收构建那一刻 import 到的」这个策略从此作废。

### 36.6 刻意继续不收的四个模块

`_STDLIB_SKIP` 分三类：Windows 上没有的 POSIX 模块；只服务 Python 自己的开发工具
（`idlelib` / `test` / `venv` …）；以及**四个 tkinter 模块**。

前两类是常识，第三类值得说清楚：`tkinter.ttk` 有 AST 守卫盯着（界面全部手写 tk）。
`tkinter.filedialog` / `tkinter.colorchooser` / `tkinter.scrolledtext` 则是
`app\paths.py:113`、`app\export_service.py:22`、`app\ui\export_dialog.py:19`、
`app\ui\shortcuts_dialog.py:18` 四处注释公开声明的设计前提 ——「冻结运行时里没有
`filedialog`，所以这里自己画纯 tk 的另存为对话框」。**收进来这些注释就变成假话了。**
与其让文档失真，不如继续不给它：谁哪天真的要用，改这一行 + 重新打包，正好是一次
有意识的决定。

（第一版重建时 `collect_submodules("tkinter")` 顺手把 `tkinter.filedialog` 收了
进去，是拆开 `PYZ.pyz` 数模块才发现的。）

### 36.7 护栏

`tests\test_runtime_recovery.py` 新增 `TestPackagingKeepsUpWithTheSource`（4 条）：

- `test_source_only_imports_what_the_frozen_build_ships`：AST 扫 `app/**/*.py` +
  `bootstrap.py` + `desktop_entry.py` 的顶层 import，每个名字必须是标准库、本仓库
  的模块，或 `PIL` / `pystray` / `six` 这三个已声明的第三方之一。加了新依赖就会红。
- `test_the_declared_third_party_is_actually_in_the_spec`：那三个名字必须在打包清单
  里出现过（前缀匹配，`"PIL.Image"` 也算 `PIL`）。
- `test_spec_collects_the_whole_standard_library`：清单里必须还有
  `sys.stdlib_module_names`，防止被删回「只收 import 到的」。
- `test_spec_keeps_the_hand_rolled_dialogs_out`：AST 读出 `_STDLIB_SKIP`，
  断言那四个 tkinter 名字仍在里面。

### 36.8 重新打包与实测

    .build-env\Scripts\python.exe -m PyInstaller --noconfirm \
        --distpath artifacts/package --workpath artifacts/pyinstaller ExplorerDict.spec
    robocopy artifacts\package\探索词典\_runtime _runtime /MIR
    Copy-Item artifacts\package\探索词典\探索词典.exe 探索词典.exe -Force

- exe **3,652,536 B → 6,159,151 B**（`_runtime` 42,525,744 → 42,605,887 B）。
- `探索词典.exe --diagnose --console-log` ⇒ `STATUS OK (no window created, no hook
  installed)`，**exit 0**；32 个 `_DIAGNOSE_MODULES` 全部 `import … ok`。
- 拆开新 exe 的 `PYZ.pyz`（**538 个模块**）：`html` / `csv` / `urllib.parse` /
  `email.mime.text` / `xml.etree.ElementTree` / `zoneinfo` / `app.export_service` /
  `app.pdf_writer` / `app.ui.export_dialog` / `app.ttf_subset` **都在**；
  `tkinter.ttk` / `tkinter.filedialog` / `tkinter.colorchooser` /
  `tkinter.scrolledtext` / `test` / `idlelib` / `lib2to3` / `distutils` /
  `ensurepip` / `venv` **都不在**。
- 构建期只剩两条无害 WARNING（`curses` 收不到子模块、`tzdata` 没装）。

### 36.9 回归

`tests.test_runtime_recovery` = **33 OK**（新增 4 条）；
逐字 19 模块白名单 = **Ran 958 / failures=6**（此前 954，正好 +4；
`_check\batch_m9_whitelist.log`；6 条仍是 `test_reading_panel` 2 条 +
`test_unified_action` 4 条，无新增）。

### 36.10 这一批的教训

「改了 `app\` 重启就行」是个**有前提**的约定：前提是这一批次没有新增 import。
只要新增了一个当时没冻进去的模块，双击就是「启动失败」—— 而源码模式、19 模块
白名单、`--diagnose` 全都看不出来（`--diagnose` 是在**新**构建上跑的）。
现在这个前提由四道静态护栏 + 「标准库整个收进来」一起兜住了。

## 37. 批次 M10：左键拖动标签（思维导图）

### 37.1 用户原话

> 请你进行反思不要再出现应用无法打开的问题。现在集中修改思维导图的交互方式，我一直声明的是
> 左键拖动标签，你现在的问题还是左键的时候无法拖动/拖动不准确不流畅一卡一卡的，
> 这是首要的问题。你先修复这个问题。

要求按 **①能拖起来 ②准确（跟手）③流畅** 排序。

### 37.2 真凶：拖动靠的是「只有测试替身才有」的属性

`app/ui/concept_map.py` 里 `_node_items(entry_id)` 原本遍历
`getattr(self.canvas, "item_options", {})` 找 `_kind in ("node", "node-text")` 的图元。
`item_options` **只有 `tests/support.py` 的 `FakeCanvas` 有**，真 `tk.Canvas` 没有 ⇒
`getattr` 给空字典 ⇒ 认不到图元 ⇒ `_drag_items` 空 ⇒ `_move_node_items()` 早退 ⇒
**拖动期间卡片一动不动，松手才瞬移**。真 Tk 探针实测「框图元位移 0.0 px（期望 100 px）」。

### 37.3 改法

| 改动 | 位置 | 为什么 |
|---|---|---|
| `NODE_TAG_PREFIX` + `node_tag()` | `app/ui/concept_map.py` | 用真 Tk 的 tag 认图元，不再自己记一本只有替身有的账 |
| `_draw()` 给框与文字打同一个 tag | `app/ui/concept_map.py` | `find_withtag` 一次就能取到整张卡 |
| `_node_items()` → `canvas.find_withtag()` | 同上 | 不留兜底分支：留了会继续替「忘了打 tag」装绿 |
| `_move_node_items()` → `canvas.move()` | 同上 | 带 tag 一次搬整组；实测搬 2 个图元 0.003 ms |
| 新增 `_follow_edges()` | 同上 | 拖动期间贴着这张卡的连线端头跟着走，线被拉直；松手 `_draw(force=True)` 恢复正规走线 |
| `FakeCanvas.find_withtag` / `.move` | `tests/support.py` | 替身补齐真 Tk 语义（`_tagged` 改为 `sorted`，保证「创建顺序」） |

### 37.4 数字

- 整张重画 `_draw(force=True)`：**14.9–17.9 ms**（60 Hz 一帧 16.7 ms ⇒ 边拖边重画走不通）。
- 修后每个 Motion 事件：**0.079 ms**（比整张重画便宜 **189.8 倍**）。
- 200 个 Motion 后图元位移 Δ=(200.000, 100.000)，**逐位跟手**。

### 37.5 测试

新增真 Tk 守卫 `tests/test_ui_roundrect.py::TestConceptMapDragPinsAndEdgeBlocks::test_a_real_canvas_drag_actually_moves_the_card`，
并**用变异证明它在旧实现上会红**（`AssertionError: 0 != 2`，同一轮假 Tk 的 26 条仍全绿）。
另外新增 `_real_root()` 上下文管理器：真窗口测试用完必须把 `theme` 的字体族 / 缩放**还原**，
否则后面的像素断言会偏 2 px（`theme.init` 是进程级全局）。

`tests.test_ui_roundrect` = **270 OK**；逐字 19 模块白名单改动后 **959 / failures=7**，
stash 回 HEAD 也是 **958 / failures=7** 且失败清单相同 ⇒ 无新增失败。

## 38. 批次 M11：左键怎么还是连线（思维导图）

### 38.1 用户原话

> 你逗我玩呢？左键怎么还是连线？你到底能不能做好？

附一张拍屏：主题 10 张卡**全都在可见范围内**，画面上却有**两条没有终点的长线**（一条实线从
画布左上角外接在 `drift-detectio` 上，一条虚线从 `Machine learning (ML) algorithms` 顶端
拖出画布）。10 张卡都在屏幕里 ⇒ 这两条线不可能是关系边，只能是「建关系」的橡皮筋线
（`_link_item`）卡住没收掉。

### 38.2 第一处真凶：`<Mod1-Button-1>` 抢走了每一次普通左键

`app/ui/concept_map.py` 原来同时绑了 `<Button-1>` → `_on_canvas_press`（拖动）
与 `<Mod1-Button-1>` → `_on_canvas_press_alt`（当时以为是「Alt 兜底」）。

**真鼠标实测**（`ctypes.windll.user32` 的 `mouse_event` + `SetCursorPos` + `keybd_event`，
配 260×160 置顶小窗，**分步 `after` + 每步 `root.update()`**）：

```
A1 不按键 · 只绑 <Button-1>              命中: [('<Button-1>', '0x8')]
A2 不按键 · 绑定同应用（五条）             命中: [('<Mod1-Button-1>', '0x8'), ('<ButtonRelease-1>', '0x108')]
B1 按住 Alt · 只绑 <Button-1>            命中: [('<Button-1>', '0x20008')]
B2 按住 Alt · 绑定同应用（五条）            命中: [('<Mod1-Button-1>', '0x20008'), ('<ButtonRelease-1>', '0x20108')]
B3 按住 Alt · 五条探针（含 <Alt-Button-1>） 命中: [('<Alt-Button-1>', '0x20008')]
C1 按住 Ctrl · 五条探针                   命中: [('<Control-Button-1>', '0xc')]
```

单绑逐条：`<Button-1>` ✓ `state=0x8 num=1`、`<Mod1-Button-1>` ✓ `state=0x8 num=1`、
`<Alt-Button-1>` ✗、`<Shift-Button-1>` ✗、`<Control-Button-1>` ✗。

**Windows 上「不按任何键的真实左键」`state` 就是 `0x8`**，而 `0x8` 恰好是 Tk 的 `Mod1Mask`
那一位；Tk 对同一个控件**只触发最具体的那一条**绑定 ⇒ 同时绑这两条时，**每一次普通左键都被
`<Mod1-Button-1>` 抢走**，拖动那条路根本不执行 ⇒ 左键永远在连线。`ALT_MASK = 0x00020000`
本身是对的（真按 Alt 时 `state=0x20008`）。这一条同时解释了 M10 与 M11 两次报障。

> ★ 排查纪律：**绝不要绑 `<Mod1-Button-1>` 做诊断**，连只加一条日志都不行 —— 它会把每一次
> 普通左键都抢过去（我踩过，日志里只剩它）。

### 38.3 第二处真凶：`after_idle` 的重画把半路手势擦掉了

修掉绑定之后，真机探针（真根窗口 + 真 `ConceptMapWindow` + 真鼠标）显示：按下**确实**设好了
`_drag_node`，卡片却仍然不动。在 `_clear()` 上挂钩子抓到「调用前状态」：

```
press_xy=(86.0, 124.0) started=False drag_node=(24, 0.4262499999999818, 0.0)
link_from=None bg=None 图元数=37
```

调用栈：`root.update()` → `tkinter/__init__.py:876 callit` →
`app/ui/concept_map.py:4824 in _draw` → `self._clear()`。`callit` 说明它来自
`after` / `after_idle` 排的队 ⇒ `_on_canvas_configure`（画布首次拿到真实尺寸
`<Configure> = [(762, 451)]`）。`_clear()` 会清
`_press_xy / _drag_started / _link_from / _drag_node / _drag_target / _bg_pan_last`
⇒ 紧随其后的 `<B1-Motion>` 认为「手上没有手势」，直接返回。

它原本的理由（「重画后图元 id 都换了」）在 M10 改成按 tag `canvas.move()` 之后**已经过期**，
代价却是手势随时可能被一次重画抹掉。

### 38.4 改法

1. 删掉 `<Mod1-Button-1>` 这条绑定，只留 `<Button-1>` 与 `<Alt-Button-1>`。
2. 彻底删掉 `_alt_armed`（属性 / 置位 / 复位 / 读取）。`_alt_held()` 只看
   `event.state & ALT_MASK`；`_on_canvas_press_alt()` 只做
   `self._on_canvas_press(event, alt=True)`。
3. 手势自愈：`_on_canvas_press()` 见到残留手势先 `_forget_gesture()` 再按新的来
   （原来是无条件 `return`，会让画布永久卡死 —— 拍屏里那两条长线就是这么留下的）。
4. 顶层兜底：`self.win` 再绑 `<ButtonRelease-1>`（`add="+"`）→ `_on_foreign_release()`，
   另加 `<Escape>` → `_on_escape()`。
5. 手势期间禁止重画：`DRAW_DEFER_MS = 120` + `_gesture_live()` / `_defer_draw()` /
   `_draw_after_gesture()` / `_cancel_deferred_draw()`；`_draw()` 开头
   `if not force and self._gesture_live(): self._defer_draw(); return`。
   `close()` 里补 `_cancel_deferred_draw()`。

### 38.5 量测与验证

真鼠标探针 `.tmp\probe_m11_realmouse.py` **通过**（退出码 0）：

```
窗口收到的事件  : [('<Button-1>', '0x8', 86, 124), ('<B1-Motion>', '0x108', 121, 146), … ,
                  ('<ButtonRelease-1>', '0x108', 226, 214)]
_clear() 调用   : press_xy=None started=False drag_node=None link_from=None bg=None 图元数=37
  移动 1/4 … 4/4 : 框图元位移 (35,22) → (70,45) → (105,67) → (140.0, 90.0) = 期望位移
落库的固定位置 : {24: (224.6, 213.0)} =（起点 84.6,123.0）+（140,90）
拖动期间出现过的临时线 : （没有，对）
结论：卡片跟手 = True ；拖动期间误开连线 = False
```

**合成事件的规矩**（`.tmp\probe_double.py`）：`event_generate("<Button-1>", x=…, y=…, state=…)`
**不给 `time` 时事件的 time 恒为 0** ⇒ Tk 认为「两次点击相隔 0 ms、同一位置」⇒ 第二次起
全被判成**双击**，只派发给 `<Double-Button-1>`。实测不带 time ⇒
`第1次=[('press', 0)] 第2次=[('double', 0)]`；每次 +900 ms ⇒ 四次全是 `press`。
**必须「控件已映射」+「每次带递增 `time`」**。

### 38.6 测试

新增三条守卫（`tests/test_ui_roundrect.py`）：

- `test_the_canvas_never_binds_the_mod1_sequence` ——
  `"<Mod1-Button-1>" not in [str(n) for n in win.canvas.bind()]`。
- `test_a_real_click_routes_to_dragging_and_alt_click_to_linking` —— 真画布
  `event_generate(state=0x8)` 走拖动、`state=0x20008` 走连线（带递增 `time`）。
- `test_a_redraw_in_the_middle_of_a_drag_is_deferred` —— 拖动中途 `_draw()` 之后手势仍在、
  补画定时器排上了，松手后清掉。

并把 `test_alt_press_is_not_swallowed_by_the_plain_binding` 里那句
`self.assertFalse(win._alt_armed, …)` 改成 `hasattr(win, "_alt_armed")` 为假 +
`win._drag_node is None`。

`tests.test_ui_roundrect` = **273 OK**；`tests.test_export_service + tests.test_ui_roundrect`
= **294 OK**；逐字 19 模块白名单 = **Ran 962 / failures=6**（959 + 3 条新守卫，6 条仍是
既有陈旧失败，无新增；日志 `_check\batch_m11_whitelist.log`）。

### 38.7 三条「验证层」的教训

同类病已经出现四次，都是「测试全绿、产品是坏的」，但**每一次骗过测试的层不一样**：

| 批次 | 骗过测试的是什么 | 谁看得出来 |
|---|---|---|
| M9 | 冻结运行时**比源码少**东西（缺 `html`） | 只有真双击 exe |
| M10 | 测试替身**比真 Tk 多**东西（`item_options`） | 只有真 `tk.Canvas` |
| M11-A | 绑定层被更具体的序列抢走 | 只有真鼠标输入 |
| M11-B | `after_idle` 重画擦掉半路手势 | 只有真事件循环 |

⇒ **替身、直接调处理函数、`event_generate` 三者各有盲区**：假环境能证明「逻辑对不对」，
证明不了「真机上走的是哪条路」。

## 39. 批次 M12：松手落位 + 关系线排线算法（思维导图）

**你在界面上会看到什么**：拖动卡片松手，它**停在鼠标松开的地方**（以前缩放不是 100% 时
会自己往回跳一截）；关系线不再绕来绕去 —— 同层两张卡能直连就直连，挡着就从本行上方 /
下方绕过去，跨行走两侧走廊，车道按「谁跨得短谁贴里面」分，**9 个模板跑下来一条交叉都没有**。

### 39.1 松手落不到位：一个缩放被乘了两遍

`pin_node()` 拿到的是**画布坐标**（已经乘过缩放），`_layout_core` 却按**世界坐标**
（`zoom = 1`）摆，`_scaled_layout` 最后又乘一次 ⇒ 卡片落到「松手位置 × 缩放」上。
改成进库前先 `_to_world()`（画布坐标 ÷ 缩放）。真机探针量的现场：松手在画布
(224.57, 213.00)，重画后卡片中心是 (168.50, 160.00) —— 正好 × 0.75。

### 39.2 排线：从「按层号」换成「按几何 + 按模板」

* **行按几何现算**：模板只换卡片坐标，`node.row` 是旧值（`tree` 模板里 `row=0` 的三个节点
  y 相差 318px），拿它算车道必然对不上；
* **按模板定主轴**：只有「单向导图」把整张图转置 90° 后用同一套规则算；
* **同层**：没东西挡就一条直线，挡着就走 ∩ / ∪ 形绕本行上 / 下；单行布局用一行外的
  **虚拟车道**（`ORTHO_VIRTUAL_GAP = 20.0`）；
* **车道按跨度分道**：短跨度的线走内圈、长跨度走外圈，长线的横段就不会被人家的竖直小段穿过；
* **交叉数当第一判据**：每条边先挑「跟已定下来的线交叉最少」的走法；
* **跨边（`对照`…）也走正交线**了 —— 对照实验：折点 `auto 17→8`、`tree 28→10`、
  `org 28→10`、`oneway 26→8`、`fishbone 17→8`，只有 `flow-s` 从 14 变 16。类型区分
  改由**线型 + 线上的短标签**承担（原先靠「跨边画弧线」）。

### 39.3 实测量与回归

9 个模板（真库主题 12 的 10 个词、5 条关系）折点数 / 交叉数：
`auto 8/0 · mindmap 23/0 · tree 10/0 · org 10/0 · oneway 8/0 · fishbone 8/0 ·
flow-h 8/0 · flow-v 8/0 · flow-s 16/0`（`mindmap` 的 23 里大部分是弧线采样）。

新增守卫 `TestMapRouteOrdering`（9 模板零交叉 / 不穿第三张卡 / 每个模板都申报自己的主轴），
fixture 直接用**用户真看到的那份数据**；变异验证：把车道排序退回旧写法 ⇒ 守卫变红。

**测试**：`tests.test_map_templates + tests.test_ui_roundrect + tests.test_export_service`
**Ran 342 / OK**；逐字 19 模块白名单 **Ran 967 / failures=7**，6 条既定陈旧失败 + 1 条环境
（本机正开着 `探索词典.exe`，单实例闸门让 `app.main.main([])` 返回 5）。

## 40. 批次 M12 补记：关系短标签之间留缝

排线干净了，`auto` / `鱼骨` 上「因果」与「对照」两个短标签却只隔 5.03px，屏幕上糊成一片。

- 原因在 `_label_position` 的调用方：标签放置只要求「不压上去」
  （`_label_fits(box, blockers, pad=0.0)`），放过一个标签就把它**原尺寸**的框
  登记进 `label_blockers`，后面那个自然可以贴着它 0.1px 放下。
- 修法：登记时按新常量 `LABEL_SEPARATION = 6.0` 外扩（纯函数 `_inflate_box(box, pad)`）。
  已放好的**标签**要求留缝；节点矩形那侧的净空不变（它们本来就带 `label_gap` 内边距）。
  顺带让 `route_cost` 里的 `1e4 * label_overlap` 变严 —— 会贴到别人的走法先被换掉。
- 实测（真主题 12 × 9 个模板）：最挤的一对 **5.03px → 10.73px**，
  而折点数（8 / 23 / 10 / 10 / 8 / 8 / 8 / 8 / 16）与交叉数（全 0）**一格没变** ⇒ 只挪标签、不动线。
- 守卫：`tests/test_ui_roundrect.py::TestMapRouteOrdering::test_two_relation_labels_never_hug_each_other`
  （先断言 `LABEL_SEPARATION >= 4.0`「定得太小等于没留」，再逐模板逐对标签框按
  `LABEL_SEPARATION - 0.05` 的容差断言不相交）。
- 顺手修掉 `_segments_intersect` docstring 里 `.tmp\probe_m12_why.py` 的反斜杠转义告警
  （Python 3.14 起是 `SyntaxWarning`）；`compileall` 现在零告警。

## 41. 批次 M13：无边界画布 + 组标签 + 关系线「全直角、零交叉」

**你这一句里三件事**：「导图画布无边界，请你不要自己加边界约束」「这个褐色背景应该是作为组标签」
「你生成的时候就是交叉的线条」—— 附了一张拍屏：卡片被拖到四面八方，关系线斜着穿过整张图，
分组是一块褐色底衬。

### 41.1 画布不再自己设边界

- 以前固定位置时算法会把坐标**夹回留白里**（`x = max(pad + w/2, spot[0])`），
  所以卡片拖到左上角外面再松手会**弹回来**贴住留白。这条约束是我自己加的，不是你要的。
  现在**一个方向都不夹**：拖到哪就摆到哪（世界坐标原样落库）。
- 光去掉夹还不够：内容外框原来只往**右 / 下**算（`content_w` / `content_h` 本身就是右 / 下边界），
  跑到原点**左上**的内容既不在滚动范围里、首开自适应也算不到，会被裁掉。
  `MapLayout` 因此新增 `content_x0` / `content_y0`（内容外框的**左上原点**，世界坐标）；
  `content_w` / `content_h` **仍是右 / 下边界**（老调用方一个字都不用改）。
- `_region_box()` 与 `layout_extent()` 都按 `min(0.0, content_x0)` 换算：
  滚动条能一路滚到负坐标那边，首开自适应按「原点 + 跨度」算缩放。
- 实测（真主题 12）：5 张固定卡 `(-162,168)` / `(340,-41)` / `(47,350)` / `(710,331)` / `(2188,303)`
  **一个都没被改**；守卫里拖到 `(-500,-500)` 也照摆，`layout_extent` 覆盖 `x < -500` 那边。

### 41.2 分组画成「细框 + 组名」，不再铺褐色底衬

- `LayoutGroup` 新增 `label`（组名 = 组长那张卡的词）与 `framed`。
- 成员外接框里**混进了别的卡片**时 `framed = False`：只写组名、不画框 ——
  不然那个框会把不相干的卡片圈进去，看着像「这些是一组」。
- 画布：先画 `fill=""` 的细框（`theme.BORDER`），再在左上角写 `GROUP_LABEL_FONT_SIZE = 6` 的组名。
  导出（`app/map_export.py`）同口径：`fill="none"` + `stroke=边框色` + `group_font = 8.0`。
- 实测：真主题 12 那两组（`shiftin` / `real-world environments`）**都**混着别的卡片，
  所以两个都只写组名 —— 你截图里那块褐色底衬没了，变成左上角一行淡字。

### 41.3 关系线：全直角，且一条都不交叉

症状（你那张图）：卡片被拖散之后，兜底候选（弧线 / 直连）胜出 ——
`依赖` 画成 `(133,348) → (612,372) → (809,372) → (2101,305)`（两段斜线跨 1900px），
`因果` 在 y=234 上横扫 1775px。18 格（9 模板 × {默认, 你拖散的位置}）实测 **7 处交叉 + 2 条斜线**。

- **① 正交兜底**：新增 `_simple_orthogonal_routes(start, end, nodes, m, *, src_node, dst_node)` ——
  L 形两组（按端口法线排序）+ 两组 Z 形 + 两条绕全局外圈的五点兜底。
  车道 = 正中 + 上下外圈 + **每张别的卡片的边缘 ± (route_margin + LAYOUT_EPS)**，按离正中由近到远取前 8。
  （只加「正中 + 外圈」时 `依赖` 的**十个候选全部 `clear=False`** —— 每条车道上恰好压着一张卡。
  所以车道必须**从障碍边缘推**。）
- **② 短的先挑线**：`sorted(plans, key=lambda item: (item["direct"], item["order"]))`。
  跨得最远的那条先挑 = 先把一条**横贯全图的干线**铺好，后面每条要上下穿过它的线都躲不开 ——
  6 处交叉全来自 `依赖 × 对照`。顺带：按距离截前 8 条车道时，`outer_*`（绕到所有卡片外面）**永远留着**，
  那正是干线唯一躲得开「从上往下穿的线」的地方。
- **③ 收尾重挑**：全部定下来之后，对每条**还跟别人交叉**的线，拿它自己的全部候选 + 现在这条重比一次
  （`clashes` **只有更少才换**）。理由：「按顺序贪心挑时后面的线只看得到前面已定的」⇒ 两条线互相挡。
  实测 4 处残留交叉**全都由 `对照` 换线解决**。
- **代价表**（`route_cost`，前面的数字是权重）：
  `1e6 * 不干净 + 1e5 * 两条线重合 + 4e2 * 交叉 + 5e2 * 斜线段 + curve_last(1e3) + drift
  + 2.0 * 折点数 + 0.35 * 长度 + 1e4 * 标签压别人 + index * 1e-3`
  ⇒ 绕开一处交叉约值 **1100px** 额外路程（`4e2 / 0.35`），一条斜线段值 **1400px**。
- **计划干净就照计划走**：只有**计划本身会跟已定的线交叉**时，才把直角备选放进来比价。
  否则 `curve_last`（1e3，罚 2 点走法）会把「同层跨边该是一条干净直线」挤成 3 点 ——
  这条被 `test_cross_edges_keep_type_label_and_do_not_change_layers` 抓住了（`AssertionError: 3 != 2`）。

**实测**（真主题 12，10 词 / 6 关系 / 你拖过的 5 张卡）：

| | 默认布局 交叉 / 斜段 | 你拖散的位置 交叉 / 斜段 |
|---|---|---|
| 改动前 | 2 / 0 | 7 / 2 |
| 定稿 | **0 / 0** | **0 / 0** |

折点：默认布局 6 → 12 是**换来的**（0 交叉 + 全直角）；例 `因果` 变成
`(421,291) → (309,291) → (309,347) → (421,347)`（往左绕开 `对照` 在 y=313 的横段，多走 224px 而不是穿过去）。
**你的口径「不要出现关系线交叉」优先。**

**守卫**：`TestMapRouteOrdering::test_cards_dragged_apart_still_get_straight_orthogonal_lines`
（9 模板逐条边 `_diagonal_segments == 0`）与 `::test_cards_dragged_apart_do_not_make_relation_lines_cross`
（9 模板 `_crossings == 0`），fixture 直接用**你真看到的那份数据**（6 条关系 + 那 5 张固定卡的世界坐标）；
变异：把收尾重挑关掉（`for _round in range(0)`）⇒ 4 个模板立刻各出 1 处交叉。
分组两条：`test_a_group_is_drawn_as_a_name_not_a_filled_slab`（组名 = 组长词 / `framed` 是 bool /
这张图上必须有 `framed=False` 的一组）与导出侧
`tests/test_map_export.py::TestSvgDocument::test_a_group_is_a_frame_with_its_name_not_a_filled_slab`。

**测试**：`tests.test_map_templates + tests.test_map_export + tests.test_ui_roundrect + tests.test_export_service`
**Ran 396 / OK**；逐字 19 模块白名单 **Ran 972 / failures=7**（6 条既定陈旧失败 + 1 条环境：
本机正开着 `探索词典.exe`，单实例闸门返回 5）。

## 42. 批次 M14：关系线照开源重做成一条三次贝塞尔

用户口径：「请你参考开源的思维导图进行修正，这个实在是太杂乱了。」+「你现在只是参考技术文章，
但是你不参考开源的实际的代码和效果？」

### 42.1 先读源码（六家）

`D:\探索工具\survey\`（仓库外）存着下载下来的原文。逐行对照表见 `改进清单.md` 的 M14 节。
**结论**：六家里没有一家绕开卡片、没有一家躲交叉；每条连线的几何都只是两个端点的闭式函数，
没有任何一个函数拿得到「第三个节点」。⇒ 交叉交给布局，不交给连线。

### 42.2 落地

`app/ui/concept_map.py`（batch M14 起点在 `:1838` 那段注释，`:1872` 起是代码）：

* `_RELATIVE_SIDES` / `relative_side(src, dst)`：照抄 `getRectRelativePosition`，9 种方位。
* `side_anchor(node, side)`：照抄 `getNodePoint`（`range=0`），取那条边的中点。
* `bezier_controls(x1, y1, x2, y2)`：照抄 `computeCubicBezierPathPoints`，三个分支逐字一致
  （包括 `abs(...) <= 5` 那个 `min`）。
* `_bezier_polyline(...)`：`CURVE_SEGMENTS = 12` 段采样。整条链路（画布 / 标签 / 箭头 /
  PNG / SVG / PDF）共用「一串点」，所以下游零改动。
* `relation_curve(src_node, dst_node)`：上面三步串起来。

**退役**：`_visual_rows` / `_corridor_routes` / `_plan_routes` / `_orthogonal_plans` 不再参与连线。

### 42.3 守卫改写与实测

29 条旧契约守卫逐条改写（名字与断言见 `改进清单.md` M14 节），
`tests.test_map_templates + tests.test_map_export + tests.test_ui_roundrect + tests.test_export_service`
= **Ran 400 / OK**；逐字 19 模块白名单 = **Ran 972 / failures=6**（6 条既定陈旧失败，无新增）。
新守卫的口径：**每条边逐字等于 `relation_curve()`**（不是「像曲线」），
再加两条棘轮（9 模板交叉 ≤ 2、穿卡 ≤ 5）。

## 43. 批次 M15：关系线改成「就着弦鼓一点点」

用户口径：「可以有交叉，主要是你自己做的交叉效果不好。」——**零交叉这个指标当场作废**。

### 43.1 病根

M14 那套是给**树**写的（simple-mind-map `getNodePoint`）：树上一个父节点方位固定，「取朝向对方那条边的
**中点**」永远够用。自由摆放的关系网里，`model performanc` 一次伸出三条线（对照 / 因果 / 对照），
全落在右边中点**那一个像素**上，控制点又固定水平出入 ⇒ 三条线从同一点出发互相绞住。
`.tmp/probe_m15.py` 实测：最尖交叉 **18.8°**、最长弦 **1758 px**。

### 43.2 落地

`app/ui/concept_map.py`：

* `border_anchor(node, toward_x, toward_y)`：从自己中心朝对方中心射一条线，**落在自己边框上**
  （方向不同 ⇒ 落点不同，守卫 `test_one_source_aims_each_edge_at_its_own_target` 钉死）。
* `CURVE_SAG_RATIO = 0.10` / `CURVE_SAG_MAX = 16.0`：控制点只在弦上鼓一点点。
* 段数自适应 `clamp(round(弦长 / 20), CURVE_SEGMENTS, 48)`。
* **删除** `_RELATIVE_SIDES` / `BEZIER_MIN_GAP` / `relative_side` / `side_anchor` / `bezier_controls`。
  `_bezier_polyline()` 与 `CURVE_SEGMENTS = 12` 原样保留。

### 43.3 实测

9 模板 × {默认排布, 拖散位置}：`pinned` 交叉 2→4 / 穿卡 2→1；`default-tree` 多绕 **+39.0% → +2.4%**；
`flow-h` 多绕 **+39.4% → +23.8%**。**交叉数变多而线条变顺**——交叉数不是用户要的指标。
回归 4 模块 **Ran 400 / OK**、白名单 **Ran 972 / failures=6**。还原点 `d699ff8`。

## 44. 批次 M16：边界即抓取区 + 文字不许压卡

用户口径：「1 太乱…我也暴露了我们一个问题，导图应该看上下文进行归纳总结。2 你这个边框优化一下，
边框同时也作为用户对词语的抓取边界，只要在词语标签的边界内，他就可以进行拖拽。」

### 44.1 M16-A：判定本来就是整张卡，缺的是光标

`MapLayout.node_at(x, y, pad=theme.px(NODE_HIT_PAD))`（`NODE_HIT_PAD = 2`）一直按**整张卡片矩形**判定；
真 Tk 探针在卡内点 8 个位置 **8/8 命中**。补的是悬停光标（`_hover_cursor` → `fleur`）。
第一版误挂 `<B1-Motion>` 不生效——**测函数返回值测不出事件有没有接上**。

### 44.2 M16-B：文字一个都不许落在卡片框里

`_label_position()` 把卡片当**硬障碍**（关系标签 / 组名 / 孤立词行），无处可去就**不画**（`None`）。
前后对照：`pinned` 压卡 **2 → 0**、`tree` **2 → 0**、`mindmap` **1 → 0**；真 Tk 复验卡外文字 9 处、
压卡 **0 处**。新守卫 `TestMapTextNeverCoversACard`（3 条）。还原点 `9196e86`。

### 44.3 M16-C：只做到侦查——「太乱」的病根在生成侧

真文章（18 词）走产品管线：`app/api_client.py` 的 `MAP_SYSTEM_PROMPT` 只要**两两关系**⇒ 产出必然是网；
同批词先归 4 个分支再画（分组树 / 放射思维导图）**交叉 0、穿卡 0**。⇒ 缺的是「会归纳」的生成侧。

## 45. 批次 M17：②缩放去顿挫 ③标题栏三键 ④线框抗锯齿去白边

用户口径：「继续学习，差太多了，无论是分辨率还是流畅度…我禁止你把文字盖在关键词方框后面」
+「缩放有卡顿…没有缩小模式，窗口模式，大屏模式…线框…锯齿边缘…边框附件没有裁剪干净存在白色的
边线」+「包括浮窗方块。」拍板：按钮要「和一般的 windows 应用一样，窗口显示栏」；本批先做 ②③④。

**「分辨率」先核实**：应用本来就声明了 per-monitor-v2 DPI 感知（`app/main.py` 启动时
`enable_dpi_awareness()`；探针实测 dpi=144），窗口没被系统拉伸。真正的「看不清」是 **Tk 画布不抗锯齿**。

### 45.1 M17-A：`draw_round_rect` 换成 2 倍超采样位图

* 先试「PIL 超采样 2 倍 → `Image.reduce(2)` → `PhotoImage` → `create_image`」，**做不到就原样退回折线**
  （假 Tk 画布没有 `create_image` ⇒ `tests/support.py` 的断言一条不改）。
* 位图缓存在 `canvas._aa_images`（**不能挂模块级全局**：`PhotoImage` 属于某个 Tk 解释器），
  `AA_CACHE_MAX = 160`，只淘汰「图元已全没了」的条目。
* 成本 0.33 ms/框（13 框冷启 18.5 ms、**命中缓存 0 ms**）；`TermCard` 悬停改色改成 `_redraw_shape()`
  （位图图元没有 `-fill`）；`ScrollRail` 滑块与阅读浮窗描边一起换过来；`round_rect_points()` 未动。
* **像素级验收**：沿弧横扫，折线路径 **0 个中间灰**（硬台阶），位图路径 **7 个**（覆盖率像素）。
* **白线**：位图贴图坐标原来被 Tk 向上取整，描边外缘半像素被丢 ⇒ 窗口最外像素是底色(249,248,246)。
  改成 `math.floor` 往外取整后最外像素 **200/200 = 描边色**（**只修了一半，真病根见 45.4**）。
  **未做**：浮窗最外圈是 Win32 region（二值、无 AA）裁的，那一圈永远是硬边；要平滑需换分层窗口。

### 45.2 M17-B：滚轮只合并重画，不合并缩放

`zoom_by(..., coalesce=True)`：`_zoom` 与锚点**立刻生效**，重画交给
`after(ZOOM_REDRAW_MS = 16, self._flush_wheel_redraw)`。实测一次滚动的 5 个刻度
**121.5 ms / 5 次重画 → 4.3 ms / 1 次重画**，指针底下的世界坐标漂移 < 1 px。
cProfile：大头是 Tk 解释器调用（≈142 次/帧），不是纯 Python 布局。

### 45.3 M17-C：标题栏三键

* `app/win32util.py`：`show_in_taskbar()` / `minimize_window()`（`WS_EX_APPWINDOW` + `SW_MINIMIZE`）。
  裸 `win.iconify()` 对 `overrideredirect` 窗口抛 `TclError`；先摘 `overrideredirect` 会闪原生标题栏。
* `widgets.BorderlessChrome`：新增 `on_maximize` / `btn_max`（字形 `□` U+25A1），
  从右到左 pack × □ —。「▢」走 `state("zoomed")` / `state("normal")`。
* 只加**主窗口 + 导图窗**；其余 10 个小对话框仍只有一个 ×。
* 坑：`is_window_visible()` 最小化时仍 True，判「缩下去了」要用 `is_iconic()`。

### 45.4 M17-D：那条白线的真病根（用户在深色桌面上问「能发现吗？」）

`math.floor` 只修掉了一半。用户随即拍了一张**深色桌面**上的浮窗小方块：亮块 66x66 物理像素里，
左边缘 x=33,34、上边缘 y=61,62 是描边色 (200,198,192) ✓，但**右边缘之后还多一列 248、下边缘之后还多一行 248**
（≈ `theme.PANEL` 249,248,246）⇒ 一条近白细线，而且只在右、下两边。

* 病根：`window_border_box()` 从 `inset + 0.5` 起笔，描边**骑在路径两侧** ⇒ 外缘落在
  `0.5 - stroke/2` **之内**，最外那一圈没人画，露出窗口底色。**不是 M17-A 引入的**（旧折线路径同样如此）。
* 改法：按线宽算（`half = stroke/2`，矩形 = 窗口去掉一圈线宽）⇒ **描边外缘 = 整块窗口**、
  **外弧半径 = radius**（与 Win32 region 的圆角重合）；`_render_border()` 把同一个线宽同时交给
  `window_border_box()` 和 `draw_round_rect()`。
* 真窗实测（150% DPI）：最外像素 **248 → 197**（描边色），四条直边各取中间 30 像素普查 = 非描边色 **0/30**，
  弧上 18 个抗锯齿过渡像素原样保留；对照图 `.tmp\m13_view\dark-dock-columns.png`。
* 守卫 `TestWindowBorderHugsTheWindowEdge`（5 条）钉住两条契约 + 实测那组数字 + **调用点必须传线宽**。
* **未做**：最外一圈仍是 Win32 二值 region 裁的硬边（`CreateRoundRectRgn` 无 AA），要平滑需分层窗口。

### 45.5 回归

`tests.test_ui_roundrect` **Ran 298 / OK**（新增 6 条标题栏守卫 + 5 条描边外缘守卫）；
5 模块 **Ran 596 / failures=3**（两条既定陈旧失败 + 一条环境性：开着 `探索词典` 时进程守卫返回 5）；
逐字 19 模块白名单 **Ran 988 / failures=7**（983 + M17-D 5 = 988；那 7 条 = 6 条既定陈旧失败
+ 1 条环境性（跑测试时桌面上开着 `探索词典` ⇒ 进程守卫返回 5），无新增失败；
`_check\batch_m17d_whitelist.log`）。
