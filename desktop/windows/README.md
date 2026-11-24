# Windows 跟随实现与交付约定

目标：Windows v0.2.2 配对 Mac v0.2.1，featureLevel=desktop-4。这里是协议和验收要求，本地尚无 Windows 宿主、安装器或安装包；不建立已发布 Windows 标签。

必须复用同一 server.py、backend/、web/ 和 release.json 清单，平台只替换桌面容器、路径、系统对话框、进程管理及安装升级卸载实现，不另写业务算法。

## 桌面消息与启动协议

内置 Python 启动 server.py --desktop --parent-stdin --port 0 --data-dir ... --original-dir ... --export-dir ... --ready-file ...。ready JSON 为 {url,version,pid}，核对其本机地址、PID 与版本后加载 WebView。宿主退出关闭父管道，后端需回收端口和就绪文件；单实例、重试及崩溃后不得留下孤儿服务。

WebView2 注入 window.webkit.messageHandlers.retailLinkDesktop.postMessage，并回调 window.RetailLinkDesktop.resolve。支持 chooseDirectory、chooseFile、saveFile、showInFolder；frontendReady 不要求 id，表示关键数据和操作已经准备。总启动保护必须覆盖到 frontendReady，不能在后端 ready 或导航结束时提前判完成。

记录进程启动、宿主窗、后端就绪、页面载入、前端可操作和错误阶段；阶段与实际耗时可见，慢启动有反馈，失败可重试或退出。设置从后端恢复，不能依赖随机端口的 localStorage。

## 正式安装体验

GUI 真的可以选择安装目录、返回更改；无权限、非法路径和空间不足明确失败且不遗留半安装。InstallLocation、实际文件清单、快捷入口、升级及卸载共享一个路径契约，不能只允许默认目录卸载。

完成页显示品牌、图标、版本、实际路径、打开目录、产品用途、三步使用与本地数据说明。用户可选择立即启动、桌面快捷方式、开始菜单快捷方式，实际结果与选择一致；默认项有说明，取消启动后不自动启动。高 DPI、键盘和屏幕阅读器可用，不以裸 MessageBox 代替整个正式流程。

覆盖升级沿用实际位置，先处理运行实例；数据保留、失败可恢复旧版，不产生第二个无提示安装。普通卸载默认选中，彻底卸载单独选中并二次确认，不能使用“是/否”映射两种方式。确认前列本次文件、快捷入口、登记项和数据范围；成功、部分失败、失败都有结果与日志，失败退出码非零。未知文件、原始资料、导入副本、Documents 用户文件及生成文件保留；彻底卸载采用白名单和可验证的恢复策略。

## 验收与外部反馈

逐项执行 desktop/验收清单.md，特别是默认/自定义/中文空格路径下的安装、启动、升级和两种卸载。核对实际文件、快捷方式、HKCU 登记、进程与数据，不以弹窗通过替代实机结果。

人工验收bug.md 中 Windows v0.1.10 安装卸载被打回、启动体感慢与55.4MB截图暂属外部反馈；须对应构建、源码和截图核验，不得记为本地复现或修复。性能必须统计完整应用进程及可归属 WebView2 辅助进程，先测 first/cold/warm/idle/long-idle，再冻结设备与门槛，不以单进程截图定超标。
