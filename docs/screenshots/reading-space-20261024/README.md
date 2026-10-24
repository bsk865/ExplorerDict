# 文章旁问答与资料库问答

2026-10-24，`tools/capture_public_rag.py` 复用已保存的公开论文实际问答，在独立数据库副本中渲染1440×900和900×620窗口，没有新增模型请求。

- `public-paper-rag.png`、`public-paper-rag-narrow.png`：资料库问答。窄窗压缩输入区，保留回答正文。
- `public-paper-reader.png`、`public-paper-reader-narrow.png`：同一原文与已保存问答并排显示；侧栏来源默认收起，输入与发送保持可见。

原文为公开14页论文 [RGraph-Eval](https://arxiv.org/abs/2610.31121)。回答涉及数据集规模、视频评估子集和表格指标，保留对应原文引用。截图脚本将已保存问答放入同一篇论文的演示侧栏，没有冒充一次新的模型回答。

窗口通过Qt控件API离屏渲染，没有操纵用户真实鼠标、键盘或私人文章。
