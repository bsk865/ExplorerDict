# 三篇公开资料的问答与原文定位

2026-10-24，使用应用实际配置的官方 `deepseek-flash / high`，完成一次真实问答。三份输入分别为 RGraph-Eval 公开 PDF、PLOS ONE 原始 PDF，以及 Europe PMC 官方全文 XML 转换的纯文本；未使用用户文章。

- RGraph-Eval：https://arxiv.org/abs/2610.31121
- PLOS ONE：https://doi.org/10.1371/journal.pone.0361484
- ACS Omega：https://doi.org/10.1021/acsomega.4c09986
- ACS 官方全文：https://www.ebi.ac.uk/europepmc/webservices/rest/PMC11822487/fullTextXML

回答分别给出30场景、90视频与一名评估者；32小时内19.47 mM DSS；从100 mM L-Tyrosine获得81.7 mM SAA。引用均来自对应文章，保留实际片段、版本和字符位置。ACS摘要中的81.67 mM与结论中的81.7 mM均按作者精度保留。RGraph-Eval的Table 3说明文字直接列出评分规模。

`answers.png` 与 `answers-narrow.png` 为1440×900和900×620窗口。`source-1.png` 至 `source-3.png` 分别通过回答引用的“在原文中定位”进入各篇文章，高亮引用片段；返回后原回答保留。截图复用已保存回答，没有再次请求模型，未操作用户鼠标、键盘或其他应用。

这组案例修复了被点名论文在其他文章参考文献中出现时的排序问题，以及长PDF跳转至未排版段落出现空白、引用只定位页首的问题。检索保留其他文章候选；题名匹配不会把无关正文变成正分依据。

真实问答记录为 `artifacts/native-checks/multi-public-rag-zq948x9w/verification.json`。输入文件的SHA256由 `tools/check_native_multi_public_rag.py` 校验。截图脚本为 `tools/capture_multi_public_rag.py`，要求选中内容与原引用完全一致，且目标文字存在可见排版。ACS输入仅为全文文字，不包括原图；这些截图不表示应用已支持直接导入XML，也不表示全部跨文献问答均已验收。
