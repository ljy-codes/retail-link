# Windows 跟随实现约定

对应 Mac v0.1.7 的 Windows 版本为 v0.1.8，功能基线 `desktop-2`。补丁版本号 Mac 使用奇数、Windows 使用紧随其后的偶数；每轮先完成 Mac 的功能与覆盖测试，再在 Windows 环境实现原生交互与打包。

必须复用同一 `server.py`、`backend/`、`web/` 与 `desktop/release.json` 中的功能清单，不单独复制修改业务算法。只替换桌面容器、文件／保存对话框、路径、进程管理和安装／卸载实现。

运行协议：内置 Python 启动 `server.py --desktop --port 0 --data-dir ... --original-dir ... --export-dir ... --ready-file ...`；读取 `{url,version,pid}` 后显示 WebView。应用包元数据 `版本.json` 写入 `version=0.1.8`、`platform=windows` 和相同的 `featureLevel=desktop-2`。

若使用WebView2，应注入 `window.webkit.messageHandlers.retailLinkDesktop.postMessage` 兼容入口，转发到宿主消息通道；结果仍回调 `window.RetailLinkDesktop.resolve`。

Windows 原生桥接须实现与 Mac 一致的 `retailLinkDesktop` 消息和 `RetailLinkDesktop.resolve` 结果（目录／文件选择、另存、定位文件夹）。主题／配置从本地后端恢复，不能依赖随机端口的 localStorage。

在 Windows 运行共享全部单元与接口测试，并逐项完成 `desktop/验收清单.md`，另测中文路径、空格、文件占用、进程退出、安装升级与两类卸载。与配对 Mac 输出使用同一虚构样本逐项比较。安装包只能在 Windows 环境构建与验证；当前未生成、未声明已完成 Windows 安装包。
