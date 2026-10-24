# 图片阅读

截图来自原生图片查看器，使用公开论文 [RGraph-Eval](https://arxiv.org/abs/2610.31121) 的第1页。

- `image.png`：适应窗口。
- `image-narrow.png`：500×400窗口，工具栏自动换行。
- `image-pan.png`：放大后的拖动模式，保留已选区域。

通过 `tools/capture_image_reading.py` 在独立资料目录渲染，不操作用户鼠标、键盘或前台程序。框选后打开所属文章的图片会话，重新打开恢复区域；截图流程不调用模型，不能作为图片问答能力的证据。
