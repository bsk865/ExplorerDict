# 多源订阅与浮窗翻译联动

截图由当前源码通过 `tools/capture_diverse_discovery.py` 离屏渲染，使用临时资料库。新闻与论文标题、简介、封面均来自原始公开订阅，权利归各来源所有。

- `discovery.png`：当前跨平台推荐和来源筛选。
- `sources.png`、`sources-narrow.png`：来源管理与实际最近更新状态，窄窗可纵向滚动。
- `translation-handoff.png`：本地交接演示；原有草稿为自编短句，新选区为公开英文短句。没有调用模型，也没有虚构译文。
- `mind-map.png`、`mind-map-narrow.png`：204字符合成阅读材料经 `deepseek-flash`、`high` 实际生成并复核的17节点、10关系导图，以当前原生界面重新渲染。示例非用户文章，也不代表14页论文已通过。运行记录保存在本机 `artifacts/native-checks/public-live-map-6msphmgc`。

最近一次读取取得220条内容、8个平台及8张原站封面。arXiv 两个栏目当次没有条目，界面保留并显示这一状态。截图未访问用户的词库、阅读记录或密钥。
