"""生成 artifacts/ 里的真实主界面截图（本任务专用，不参与应用运行）。

执行前检查（默认拒绝）
----------------------
本脚本会**真实启动应用主窗口**并截图，属于「会打扰用户的 GUI 动作」
（用户经常在玩游戏 War Thunder）。因此：

* 默认拒绝运行，除非用户主动同意：
  环境变量 ``EXPLORER_DICT_ALLOW_GUI_TESTS=1`` 或命令行 ``--allow-gui``；
* 即使同意，也要先通过 ``app.gui_preflight`` 的**实时真实前台**预检：
  只有「阅读白名单应用 + 非全屏 + 非游戏模式」才继续，否则退出且不建窗口。

做法：
1. 用 **项目内的临时数据目录** ``.tmp/shot_data/``（EXPLORER_DICT_DATA_DIR 覆盖），
   **绝不**触碰真实 ``data\\explorer_dict.sqlite3``；
2. 往隔离库里写入**明显标记为【测试数据】/【截图专用】** 的批次与词条；
3. 启动真实 ``app/main.py``（带 --console-log），主窗口**保持默认标题**；
4. 等窗口出来后用现成的 ``tools/screenshot_window.py``（``--exact`` 精确定位 +
   窗口自身重绘一次）截图；
5. 关闭**本次任务启动的**测试进程，并清掉本进程启动的 UIA helper。

安全约束（刻意遵守）：
* **不**把测试窗口提到前台（``--foreground`` 不用），避免打扰用户当前前台窗口；
  截图只用 GetWindowDC/BitBlt 读该窗口自己的像素；
* 不打开游戏、反作弊或用户既有浏览器会话；
* 不绕过门控显示浮窗（截图里只有主界面）；
* 用户正在游戏/全屏时**默认直接退出**（见上面的执行前检查）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOT_DATA = ROOT / ".tmp" / "shot_data"
#: 与 app/ui/main_window.py 里的默认标题一致（截图里不出现测试标记）
WINDOW_TITLE = "探索词典 — 跨应用阅读划词收藏器"

TEST_ENTRIES = [
    ("卷积神经网络", "深度学习中最常用的特征提取结构，通过局部感受野与权值共享降低参数量。",
     "【测试数据】示例文档 A", "https://example.invalid/test-doc-a", "url_document",
     "一句话：一种用于图像与序列建模的层次化特征提取网络。"),
    ("注意力机制", "让模型对输入的不同位置分配不同权重，是 Transformer 的核心组件。",
     "【测试数据】示例文档 A", "https://example.invalid/test-doc-a", "url_document",
     "一句话：按相关性对输入加权求和的建模方式。"),
    ("bank", "river bank — 河岸；bank account — 银行账户，同一词在不同语境下含义不同。",
     "【测试数据】语境消歧示例", "", "window_title_only",
     "一句话：多义词必须结合上下文判断。"),
    ("tokenizer", "把文本切成模型可处理的 token 序列的组件。",
     "【测试数据】示例文档 B", "https://example.invalid/test-doc-b?id=2", "url_document", ""),
]


def build_isolated_db() -> Path:
    if SHOT_DATA.exists():
        import shutil
        shutil.rmtree(SHOT_DATA, ignore_errors=True)
    SHOT_DATA.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from app.db import Database

    db = Database(SHOT_DATA / "explorer_dict.sqlite3")
    try:
        bid_a = db.create_batch("【测试数据】示例文档 A · 09-30 10:00",
                                "url:https://example.invalid/test-doc-a", "url_document")
        bid_b = db.create_batch("【测试数据】示例文档 B · 09-30 10:05",
                                "url:https://example.invalid/test-doc-b?id=2", "url_document")
        groups = {0: bid_a, 1: bid_a, 2: bid_a, 3: bid_b}
        for i, (term, ctx, title, url, conf, one_line) in enumerate(TEST_ENTRIES):
            eid = db.add_entry(
                batch_id=groups[i], term=term, context=ctx,
                doc_key=f"k{i}", source_app="msedge.exe", source_title=title,
                source_url=url, source_confidence=conf, capture_method="manual_input",
            )
            if one_line:
                db.update_entry(eid, explain_status="ok", one_line=one_line,
                                detail="（截图用测试数据，非真实模型输出）",
                                examples='["仅用于界面截图"]',
                                model_config="(未配置 API，本条为测试数据)",
                                explained_at="2026-09-30T10:10:00+08:00")
        db.log_event("selftest", "artifacts 截图用隔离测试库")
    finally:
        db.close()
    return SHOT_DATA / "explorer_dict.sqlite3"


def _preflight_or_exit(argv: list[str]) -> None:
    """执行前检查：默认拒绝；同意后还要真实前台安全才允许继续。"""
    if "--allow-gui" in argv:
        os.environ.setdefault("EXPLORER_DICT_ALLOW_GUI_TESTS", "1")

    sys.path.insert(0, str(ROOT))
    from app import gui_preflight as preflight

    if not preflight.user_consented():
        print(
            "[已拒绝] tools/capture_artifacts.py 会真实启动应用主窗口并截图，"
            "默认不自动执行（用户可能正在游戏/全屏）。\n"
            "  仅当**用户主动**要求生成截图时才运行，并二选一：\n"
            "    set EXPLORER_DICT_ALLOW_GUI_TESTS=1   （或在命令行加 --allow-gui）\n"
            f"  当前预检：{preflight.check().label}",
            file=sys.stderr,
        )
        raise SystemExit(3)

    decision = preflight.check()
    if not decision.allowed:
        print(f"[已跳过] {decision.skip_message('截图演示')}", file=sys.stderr)
        raise SystemExit(4)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "artifacts" / "main_window.png"))
    ap.add_argument("--wait", type=float, default=6.0)
    ap.add_argument("--allow-gui", action="store_true",
                    help="用户主动同意被打扰（仅人工验收时使用）")
    args = ap.parse_args(argv)

    _preflight_or_exit(argv)

    db_file = build_isolated_db()
    print(f"[1/4] 隔离测试库: {db_file}")

    env = dict(os.environ)
    env["EXPLORER_DICT_DATA_DIR"] = str(SHOT_DATA)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "app" / "main.py"), "--console-log"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    print(f"[2/4] 已启动测试实例 pid={proc.pid}（数据目录={SHOT_DATA}）")
    try:
        time.sleep(args.wait)
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            print("启动即退出，输出：\n" + out[-2000:])
            return 2
        out_png = Path(args.out)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-X", "utf8", str(ROOT / "tools" / "screenshot_window.py"),
               "--title", WINDOW_TITLE, "--exact", "--out", str(out_png)]
        shot = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, timeout=120)
        print("[3/4] " + shot.stdout.decode("utf-8", "replace").strip())
        if shot.returncode != 0:
            print(shot.stderr.decode("utf-8", "replace"))
            return 3
        print(f"[4/4] 截图: {out_png} ({out_png.stat().st_size} bytes)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        print(f"已关闭测试实例 pid={proc.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
