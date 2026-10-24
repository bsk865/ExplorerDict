# 图片阅读

截图来自原生图片查看器，使用公开论文 [RGraph-Eval](https://arxiv.org/abs/2610.31121) 的第1页。

- `image.png`：适应窗口。
- `image-narrow.png`：500×400窗口，工具栏自动换行。
- `image-pan.png`：放大后的拖动模式，保留已选区域。
- `table-answer.png`、`table-answer-narrow.png`：第8页表2选区的实际模型回答，分别为普通及900×620窗口。

查看器截图通过 `tools/capture_image_reading.py` 在独立资料目录渲染，不操作用户鼠标、键盘或前台程序。框选后打开所属文章的图片会话，重新打开恢复区域；查看器截图流程不调用模型。

表格回答通过 `tools/check_native_live_image.py` 使用官方 `deepseek-flash/high` 实际生成，输入为第8页表2的605×252像素选区，未提供可提取正文或历史回答。识别描述和回答两次请求均只发送选区。回答正确区分 Human evaluation 中 Ours 与 SG-Ego 的 Objects/Relations F1：89.67/82.66与91.78/67.80，并引用对应图片；原文资料和选区保存在独立目录。该案例不代表所有图表与扫描资料均通过。

表格截图通过 `tools/capture_image_answer.py` 复用上述已保存回答渲染，不额外调用模型。所有截图都不含用户资料或密钥。
