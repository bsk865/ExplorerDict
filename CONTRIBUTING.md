# 参与探索词典

## 提交问题与建议

在 [Issues](https://github.com/bsk865/ExplorerDict/issues) 描述使用版本、操作步骤、预期结果和实际现象。界面问题可以附上截图；涉及资料时可提供便于复现的公开文章或简短示例。

分享日志和截图前移除API密钥、私人文章和个人路径。

## 准备开发环境

环境与启动步骤见[源码运行指南](docs/复现指南.md)。当前主入口是 `native_desktop.py`，主界面位于 `app/native/`；浮窗组件位于 `app/ui/`，业务和存储模块位于 `app/`。

可以使用[环境变量示例](scripts/repro-env.example.ps1)将开发资料保存到独立目录。问答、翻译和导图共用设置中的模型服务。

## 修改代码

围绕一个具体行为提交修改。界面展示放在页面控制器，业务规则放在服务模块，持久化沿用共享存储和事务。新增文章工具按文章保存状态；涉及主界面与浮窗的修改同步考虑共享配置和事件。

根据改动范围运行对应检查。阅读、问答或导图改动关注文章版本和原文依据；界面改动查看正常窗口的实际排布；构建相关改动核对两个桌面入口。

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.build-env/Scripts/python.exe -m unittest tests.test_discovery_updates tests.test_native_discovery -q
Remove-Item Env:QT_QPA_PLATFORM
```

上面的命令适用于发现页相关修改。其他模块在 `tests/` 选择对应检查，桌面划词在正常桌面环境中复核。

## 提交修改

为修改创建分支，提交信息说明具体变化。Pull Request介绍问题、修改后的行为与所做检查；界面变化附上截图。同步更新涉及的使用或技术文档。

源码与文档提交Git；构建程序通过Releases分发。资料数据库、API密钥、个人日志和模型权重保存在本地。
