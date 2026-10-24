# 多来源订阅与更新状态

2026-10-24，`tools/capture_discovery_recovery.py` 在独立资料目录读取默认的10个公开订阅来源，再从真实原生窗口离屏截图。图片来自订阅及原网页，没有生成封面或使用第三方图片代理。

- `discovery.png`：7个平台的200条内容，包含7张原站封面。
- `sources.png`、`sources-narrow.png`：逐来源最近成功更新时间、条数及订阅操作，后者为900×620窗口。

当次arXiv人工智能栏目合法返回空列表，机器人栏目和Solidot读取两次后仍失败；界面保留这些状态，没有改成全部成功。其余来源包括IT之家、BBC、ScienceDaily、The Guardian、NPR、PLOS ONE和The Conversation。

没有使用私人阅读记录、发送模型请求或启动全局取词监听。截图不代表所有来源持续可用。
