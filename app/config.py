"""应用设置（全部存 DB.settings；API key 单独走 DPAPI 密文）。"""
from __future__ import annotations

from .db import Database
from .logging_setup import register_secret

DEFAULTS: dict[str, str] = {
    # API
    "api.base_url": "https://api.deepseek.com/v1",
    "api.model": "deepseek-chat",
    "api.timeout": "25",
    # 推理强度：空串 = 请求体里根本不带 reasoning_effort（兼容所有网关，
    # 包括不认识这个字段的第三方端点）。**档位随模型不同**（DeepSeek 官方只有
    # low / high / max，思考模式默认 high），界面档位见 effort_choices()。
    "api.reasoning_effort": "",
    # 导图的思考模式开关：auto（默认，DeepSeek 系才显式关掉思考模式）/ always / never。
    # 关掉它是为了让「重新生成」给出同一张图：思考模式不支持 temperature，
    # 采样不受约束 ⇒ 同一份材料每次提的候选都不一样。见 Config.map_thinking()。
    "api.map_thinking": "auto",
    # 取词（默认从简：只要能选中文本就尝试读取）
    "capture.enabled": "1",
    "capture.min_len": "1",
    "capture.max_len": "80",
    "capture.context_chars": "120",
    "capture.dedupe": "1",
    # 同一个概念再次被划到（上下文不同）时的动作：
    # "new" = 建新词条（默认，历史行为）；"append" = 把新上下文追加到旧词条上。
    # 见 Config.duplicate_action / app/capture_service.py 的 bookmark()。
    "capture.duplicate_action": "new",
    "capture.notify_on_failure": "0",
    # 取词时顺手把**这一页的原文**抄一份留下来（导图要「按原文归纳」，只拿每个词
    # 240 字上下文是归纳不出文章脉络的）。默认开：抓取在后台线程做，不挡划词、
    # 不挡解释；同一页只抓一次，抓不到就如实记「未取到原文」。
    # 关掉它 = 完全不联网取原文，导图退回「词与词的关系」那一套。
    "capture.fetch_article": "1",
    # 前台环境门控：默认放开普通窗口，只挡「手动游戏模式 / 已知游戏进程 / 全屏」。
    "gate.game_mode": "0",
    # 界面
    "ui.topmost": "1",          # 浮层与主界面默认置顶（用户诉求）
    # 浮窗置顶授权：空串 = 还没问过用户（不置顶），"1" = 允许，"0" = 拒绝
    "ui.overlay_topmost_consent": "",
    "ui.batch_scope_all": "0",
    # 首次配置向导是否已经看过（看过就不再弹 —— 用户明确要求「第一次打开主界面」才弹）。
    # 空/0 = 还没看过。见 app/ui/setup_wizard.py 与 App.maybe_show_setup_wizard()。
    "ui.wizard_done": "0",
    # 阅读面板（Dock + 展开浮窗）位置尺寸：空 = 用默认（右边缘小方块 / 其左侧面板）
    "ui.panel.dock_x": "",
    "ui.panel.dock_y": "",
    "ui.panel.x": "",
    "ui.panel.y": "",
    "ui.panel.w": "",
    "ui.panel.h": "",
    # 追问（对话）
    "chat.enabled": "1",
    "chat.history_turns": "6",   # 只带最近 6 轮（1 轮 = 用户 + 助手）
    "chat.max_history_chars": "4000",
    "chat.no_key_hint": (
        "尚未配置 API Key，问题没有被发送。请在「设置」里填写 Base URL / 模型名 / API Key，"
        "然后重新点「发送」。"
    ),
    # 参考关系图（AI 生成候选 + 证据校验；只在用户打开导图 / 点生成时请求）
    "map.enabled": "1",
    "map.max_entries": "24",     # 一次最多带多少个词条（只带当前主题）
    "map.material_chars": "240",  # 每个词条的上下文 / 释义截断长度
    # 布局骨架（G1，见 app/ui/map_templates.py）：auto = 今天这套「一层一行」；
    # 其余是思维导图 / 树状图 / 组织架构图 / 单向导图 / 鱼骨图 / 流程线。
    "map.template": "auto",
    # 让模型在「模板」下拉里给一条建议（G3）：默认关 —— 只发关系类型与起止词名，
    # 不发释义正文；关掉时只用本地规则（零成本）。
    "map.template_ask_model": "0",
    "map.no_key_hint": (
        "尚未配置 API Key，参考关系没有被生成。请在「设置」里填写 Base URL / 模型名 / "
        "API Key，然后点「重试」。"
    ),
    # 导出：保存到哪个文件夹（空 = 用默认的 <data>\exports\）。
    # 允许写 %USERPROFILE%\Desktop 这类带环境变量的路径；界面上的「导出」按这个目录落盘，
    # 只在用户点导出时读一次，见 app/export_service.py 的 resolve_directory()。
    "export.directory": "",
    # 导出：上次用过的格式（CSV / markdown / json / jsonl / anki / pdf / html / txt）。
    # 只用来把选择器里上一次的选项记住，默认 CSV（与老行为一致）。
    "export.format": "csv",
}


# 这些键绝不允许出现在 settings 表里（防止明文 key 落库）
FORBIDDEN_SETTING_KEYS = {"api.key", "api_key", "apikey", "api.key_plain", "api.authorization"}

# 模型名候选（设置页的预设按钮，仍然允许手输第三方网关的模型名）
MODEL_PRESETS: tuple[str, ...] = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-coder",
)

#: 设置页的「服务商」一键模板：``(显示名, Base URL, 推荐模型名)``。
#:
#: 点一下只**预填**地址与模型名 —— **不内置任何密钥**，Key 永远由用户自己粘贴；
#: 也不改变底层逻辑（照样是用户自备的 OpenAI 兼容端点）。
#: 各家模型迭代很快，这里填的只是当下的常用默认值，模型名照样能手输。
#: 顺序有讲究：第一个是默认端点（与 ``DEFAULTS["api.base_url"]`` 一致）。
PROVIDER_PRESETS: tuple[tuple[str, str, str], ...] = (
    ("DeepSeek 官方", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("硅基流动", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    ("通义千问（兼容模式）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("本地 Ollama", "http://127.0.0.1:11434/v1", "qwen2.5:7b"),
)

#: 本地推理服务（首次配置向导里的「本地」选项）：Ollama 默认端口上的 OpenAI 兼容端点。
LOCAL_ENDPOINT: tuple[str, str] = ("http://127.0.0.1:11434/v1", "qwen2.5:7b")

# 推理强度：空串 = 不发送该字段；其余值原样发送给支持它的模型 / 网关。
# 这张表是**校验白名单**（老设置、手改过的配置、别家网关写的值都要能存住），
# 不是设置页给出的档位 —— 界面上显示哪几档由模型名决定，见 effort_choices()。
# OpenAI 风格的可选值是 minimal / low / medium / high / xhigh / max / ultra；
# DeepSeek 官方只认 low / high / max（其余值官方映射表会折算，见下面的文案）。
REASONING_EFFORTS: tuple[str, ...] = (
    "",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

#: 各系列模型在设置页里给出的档位（键 = 模型名里出现就算命中，大小写不敏感）。
#: 依据 DeepSeek 官方《思考模式》文档：思考模式默认打开、``reasoning_effort``
#: 的取值是 low / high / max（minimal→low，medium→high，xhigh→high，ultra→max）。
EFFORT_TIERS_BY_MODEL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deepseek", ("", "low", "high", "max")),
)

#: 认不出模型时的通用档位（OpenAI 风格的三档写法）。
EFFORT_TIERS_FALLBACK: tuple[str, ...] = ("", "low", "medium", "high")


def effort_choices(model: str) -> tuple[str, ...]:
    """按模型名给出**设置页该显示哪几档推理强度**。

    DeepSeek 只有 ``low / high / max`` 三档（外加「不发送」= 用模型默认，官方默认
    ``high``）；换成其它模型（OpenAI 风格网关）就回到 ``low / medium / high``。
    认不出来时给通用档位：档位只是界面候选，用户照样能手输模型名，保存后按裸值发送。
    """
    name = str(model or "").strip().lower()
    for keyword, tiers in EFFORT_TIERS_BY_MODEL:
        if keyword in name:
            return tiers
    return EFFORT_TIERS_FALLBACK


# 界面文案（设置页的说明行、「当前生效」行与检查反馈共用）
REASONING_LABELS: dict[str, str] = {
    "": "不发送（用模型默认；DeepSeek 思考模式默认 high）",
    "minimal": "minimal（最少思考；DeepSeek 会折算成 low）",
    "low": "low（更少思考，更快更省）",
    "medium": "medium（中等；DeepSeek 会折算成 high）",
    "high": "high（更多思考，更细更慢）",
    "xhigh": "xhigh（更高；DeepSeek 会折算成 high）",
    "max": "max（最多思考，最慢最贵；DeepSeek 的最高档）",
    "ultra": "ultra（最高；DeepSeek 会折算成 max）",
}

#: 单选按钮上的**短**文案（长话放不下，完整解释由说明行显示）
REASONING_SHORT: dict[str, str] = {
    "": "不发送（默认）",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}


def looks_like_deepseek(model: str, base_url: str = "") -> bool:
    """模型名或 Base URL 里出现 ``deepseek`` 就当作 DeepSeek 系网关。

    只用来决定「要不要发 DeepSeek 专有字段」（思考模式开关）：别的网关收到不认识
    的字段可能直接 400，所以宁可不发。判错了也不要紧 —— ``api.map_thinking``
    可以强制 ``always`` / ``never``。
    """
    text = f"{model or ''} {base_url or ''}".lower()
    return "deepseek" in text


def model_signature(model: str, reasoning_effort: str = "") -> str:
    """缓存键 / 指纹里的「模型签名」：换了推理强度就不许命中旧结果。

    返回值仍然是纯文本，直接塞进 sha256 输入；空推理强度时与旧口径完全一致
    （``model`` 原样返回），所以老缓存 / 老指纹不会因为这次改动失效。
    """
    effort = str(reasoning_effort or "").strip().lower()
    return f"{model}|{effort}" if effort else str(model or "")


def effort_from_label(label: str) -> str:
    """界面文案 → 真正发出去的字段值；认不出来一律空串（= 不发送）。

    同时接受**裸值**（``low`` / ``medium`` / ``high``，大小写与空格不敏感）：
    老设置文件、手改过的配置、以及别的界面都可能直接存裸值，
    只认文案的话它们会被静默重置成「不发送」，那是丢用户配置。
    """
    text = str(label or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for effort in REASONING_EFFORTS:
        if REASONING_LABELS[effort] == text or (effort and effort == lowered):
            return effort
    return ""


class Config:
    """设置访问器。写入前统一拦截明文 key。"""

    def __init__(self, db: Database):
        self.db = db
        for k, v in DEFAULTS.items():
            if db.get_setting(k) is None:
                db.set_setting(k, v)

    # ------------------------------------------------------------- 基本访问
    def get(self, key: str, default: str | None = None) -> str:
        fallback = DEFAULTS.get(key, default if default is not None else "")
        v = self.db.get_setting(key)
        return fallback if v is None else v

    def set(self, key: str, value: str) -> None:
        if key.lower() in FORBIDDEN_SETTING_KEYS:
            raise ValueError(
                f"禁止把明文密钥写入 settings（{key}）；请使用 Database.set_secret() 走 DPAPI"
            )
        self.db.set_setting(key, value)

    def get_bool(self, key: str, default: bool = False) -> bool:
        """读一个布尔开关。

        **认不出的写法退回 ``default``，不是退回 False**（批次 M20-A 修的）：用户手改
        `data\\config.json` 时打错一个字（写成 ``"也许"`` / ``"ture"``），过去会被静默当成
        「关」—— 一个装上去就再也开不回来的开关比报错更糟。已知的「假」写法
        （``0`` / ``false`` / ``no`` / ``off``）仍如实算关。
        """
        v = self.db.get_setting(key)
        if v is None:
            v = DEFAULTS.get(key)
        if v is None:
            return default
        text = str(v).strip()
        if text in ("1", "true", "True", "yes", "on"):
            return True
        if text in ("0", "false", "False", "no", "off"):
            return False
        return default

    def set_bool(self, key: str, value: bool) -> None:
        self.set(key, "1" if value else "0")

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(str(self.get(key, str(default))).strip())
        except (TypeError, ValueError):
            return default

    def set_int(self, key: str, value: int) -> None:
        self.set(key, str(int(value)))

    # ----------------------------------------------------------------- 导出
    @property
    def export_directory(self) -> str:
        """导出保存目录（空串 = 用默认的 ``<data>\\exports``）。"""
        return self.get("export.directory").strip()

    @property
    def export_format(self) -> str:
        """上次用过的导出格式 key；认不出来的值由 export_service 退回 CSV。"""
        return self.get("export.format").strip().lower() or "csv"

    # ------------------------------------------------------------------ API
    @property
    def base_url(self) -> str:
        return self.get("api.base_url").strip()

    @property
    def model(self) -> str:
        return self.get("api.model").strip() or "deepseek-chat"

    @property
    def reasoning_effort(self) -> str:
        """白名单化后的推理强度（""/minimal/low/medium/high/xhigh/max/ultra）。

        这里**不按模型过滤**：模型名是自由文本，老设置里也可能存着别的写法；
        设置页显示的档位由 :func:`effort_choices` 按模型名给出。
        非法值（拼错、乱写）一律当「不发送」。
        """
        value = self.get("api.reasoning_effort").strip().lower()
        return value if value in REASONING_EFFORTS else ""

    def set_reasoning_effort(self, value: str) -> None:
        effort = str(value or "").strip().lower()
        self.set("api.reasoning_effort", effort if effort in REASONING_EFFORTS else "")

    @property
    def model_signature(self) -> str:
        """缓存 / 指纹用的模型签名：换了推理强度就不能命中旧结果。"""
        return model_signature(self.model, self.reasoning_effort)

    @property
    def map_thinking(self) -> str:
        """导图管线要不要**显式关掉思考模式**：返回 ``"disabled"`` 或 ``""``。

        DeepSeek 的思考模式默认打开，而思考模式**不支持 temperature** —— 于是同一
        份材料每点一次「重新生成」都会给出不同的候选（实测 6/6/5 条、并集 10 条），
        用户看到的就是「导图每次都变」。DeepSeek 网关接受
        ``{"thinking": {"type": "disabled"}}``，关掉后连逐条内容都稳定。

        默认 ``auto``：**只在 DeepSeek 系**（模型名或 Base URL 含 deepseek）才发这个
        字段，别的网关收到不认识字段可能 400。``api.map_thinking`` 可写
        ``always`` / ``never`` 强制开关（不放进设置界面，改配置即可）。
        """
        mode = str(self.get("api.map_thinking") or "auto").strip().lower()
        if mode in ("always", "1", "true", "yes", "on"):
            return "disabled"
        if mode in ("never", "0", "false", "no", "off"):
            return ""
        return "disabled" if looks_like_deepseek(self.model, self.base_url) else ""

    @property
    def model_display(self) -> str:
        """给用户看的「当前生效」一行。"""
        effort = self.reasoning_effort
        if effort:
            return f"{self.model} · 推理强度 {effort}"
        return f"{self.model} · 推理强度不发送"

    @property
    def timeout(self) -> float:
        try:
            return max(3.0, float(self.get("api.timeout")))
        except (TypeError, ValueError):
            return 25.0

    def api_key(self) -> str:
        """从 DPAPI 密文解出。没有则返回空串（不读环境变量、不读全局配置）。"""
        key = self.db.get_secret("api_key") or ""
        if key:
            # 登记已知密钥：非 sk- 前缀的第三方 Key 也能在日志 / 异常文本里被替换掉
            register_secret(key)
        return key

    def set_api_key(self, key: str) -> None:
        value = key.strip()
        self.db.set_secret("api_key", value)
        if value:
            register_secret(value)

    def has_api_key(self) -> bool:
        return self.db.has_secret("api_key")

    # -------------------------------------------------------------- 取词参数
    @property
    def capture_enabled(self) -> bool:
        return self.get_bool("capture.enabled", True)

    @property
    def context_chars(self) -> int:
        return max(0, min(1000, self.get_int("capture.context_chars", 120)))

    @property
    def min_len(self) -> int:
        return max(1, self.get_int("capture.min_len", 1))

    @property
    def max_len(self) -> int:
        return max(1, self.get_int("capture.max_len", 80))

    @property
    def dedupe(self) -> bool:
        return self.get_bool("capture.dedupe", True)

    @property
    def duplicate_action(self) -> str:
        """同一个概念**再次被划到**时怎么办（B4）：

        * ``"new"``（默认，历史行为）—— 新语境就建一条新词条；
        * ``"append"`` —— 把新语境**追加**到已有词条上（一个词条收齐它在不同
          材料里的各种语境），旧解释会被标成「需要重新解释」。

        只认识这两个值：配置文件里写了别的（手改 / 历史残留）一律退回 ``"new"``，
        绝不让一个拼错的值把取词带进未知分支。
        """
        value = str(self.get("capture.duplicate_action", "") or "").strip().lower()
        return "append" if value == "append" else "new"

    @property
    def notify_on_failure(self) -> bool:
        return self.get_bool("capture.notify_on_failure", False)

    @property
    def fetch_article(self) -> bool:
        """取词时要不要顺手把这一页的原文抓下来（默认开）。

        关掉它是**唯一**能让本应用完全不联网取原文的开关；导图随之退回
        「按词条关系生成」，界面上会如实写明没取到原文。
        """
        return self.get_bool("capture.fetch_article", True)

    # ---------------------------------------------------------- 门控
    @property
    def game_mode(self) -> bool:
        """手动游戏模式：持续禁用全局鼠标监听与取词，直到用户主动关闭。"""
        return self.get_bool("gate.game_mode", False)

    # ---------------------------------------------------------- 界面
    @property
    def topmost(self) -> bool:
        """用户偏好的「置顶」开关（默认开启）。游戏/全屏期间临时取消，离开后恢复。"""
        return self.get_bool("ui.topmost", True)

    def set_topmost(self, value: bool) -> None:
        self.set_bool("ui.topmost", bool(value))

    @property
    def overlay_topmost_consent(self) -> bool | None:
        """浮窗置顶授权：``None`` = 还没问过用户（**不置顶**），True 允许，False 拒绝。"""
        raw = (self.get("ui.overlay_topmost_consent") or "").strip()
        if raw == "1":
            return True
        if raw == "0":
            return False
        return None

    def set_overlay_topmost_consent(self, value: bool) -> None:
        self.set_bool("ui.overlay_topmost_consent", bool(value))

    # ---------------------------------------------------------- 阅读面板
    def panel_state(self):
        """面板几何状态（纯数据，见 ``app.ui.panel_geometry``）。"""
        from .ui.panel_geometry import PanelState

        return PanelState.load(self.get)

    def save_panel_state(self, state) -> None:
        state.save(self.set)

    # -------------------------------------------------------------- 追问
    @property
    def chat_enabled(self) -> bool:
        return self.get_bool("chat.enabled", True)

    @property
    def chat_history_turns(self) -> int:
        """只带最近 N 轮对话（避免把整段历史发出去）。"""
        return max(0, min(50, self.get_int("chat.history_turns", 6)))

    @property
    def chat_max_history_chars(self) -> int:
        return max(200, min(20000, self.get_int("chat.max_history_chars", 4000)))

    @property
    def chat_no_key_hint(self) -> str:
        hint = (self.get("chat.no_key_hint") or "").strip()
        return hint or DEFAULTS["chat.no_key_hint"]
