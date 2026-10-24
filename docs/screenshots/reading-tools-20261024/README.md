# 阅读与翻译截图

2026-10-24，使用 `tools/capture_fulltext_translation.py` 从当前源码的原生窗口离屏渲染，不启动取词监听，不操作用户文章。

- `translation.png`：文章工具栏内的原文与译文对照，没有独立翻译分区。
- `translation-narrow.png`：同一功能在900×620窗口中的布局。
- `paper-reader.png`：实际导入的公开论文，共14页；保留全文及原始 PDF。
- `fulltext-detail.png`：论文全文导入后的资料信息。
- `subscription.png`、`subscription-narrow.png`：网站订阅查找表单及窄窗布局；表单位于订阅列表前，无须翻过全部已订阅来源。

演示短句为“Robots can learn from demonstrations.”，译文“机器人可以从演示中学习。”来自配置中的 `deepseek-flash`、`high` 实际请求。截图脚本复用已保存的这次结果，没有再次调用模型。公开论文来源为 https://arxiv.org/abs/2610.31121 。截图中的资料不含个人数据或密钥。

截图用于说明布局和内容来源，不代替所有语言、长文质量或普通桌面操作验收。
