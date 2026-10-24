# 全文与翻译界面

由 `tools/capture_fulltext_translation.py` 使用当前原生界面和临时资料库离屏渲染。

论文来自公开来源 [RGraph-Eval，arXiv:2610.31121](https://arxiv.org/abs/2610.31121)，原始 PDF 为14页、3,051,967字节。论文内容及图表权利归原作者；详情页演示预览取自 PDF 首页，资料库保存了完整文件和提取正文。

翻译截图使用公开短句“Robots can learn from demonstrations.”及2026-10-24通过配置中的 DeepSeek Flash、high 实际得到的译文。渲染脚本本身不调用模型；若本机没有这次请求的记录，就展示空白翻译页。

截图不含用户资料或密钥。截图展示布局；普通桌面的连续使用体验另行试用。
