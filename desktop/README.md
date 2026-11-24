# 零售通桌面版开发

版本配对按补丁号：Mac 奇数、Windows 紧随偶数；当前 Mac 0.1.9 对应 Windows 0.1.10，统一功能级别 desktop-3。Mac 负责共享功能和覆盖测试，Windows 在 Windows 环境复用同一后端和前端，完成原生壳、平台交互、安装包与实机验收。

macOS 使用 AppKit / WKWebView 原生壳；Windows 使用 WinForms / WebView2 原生壳。两端都启动内置 Python，后端仅监听系统分配的 `127.0.0.1` 端口；ready 文件校验 PID、版本和 URL 后停止启动轮询。前端使用一致的原生文件桥，设置持久化到应用数据，退出时回收后端。

## Windows 构建

要求 Windows 10/11 x64、.NET 10 SDK、Python 3.12、openpyxl 与 et_xmlfile。用户安装后不需要 Python 或构建工具，但系统需要 Microsoft Edge WebView2 Runtime。

```powershell
python -B desktop\windows\build_windows.py --publish --keep-build
```

构建脚本会生成自包含 WinForms 应用、内置 Python 运行环境、安装程序和两文件交付目录；冒烟测试会真实请求健康、业务模板与缺少样例的错误响应。产品交付只保留安装包和对应 HTML 说明。构建缓存和展开载荷位于 `desktop/build/`，测试数据位于隔离目录，不上传 GitHub。

## macOS 构建

构建需要 Intel Mac/macOS 13+、Swift 5.8+、可迁移的 Python 3.12 运行环境、openpyxl、et_xmlfile 及 Pillow（仅绘制构建图标）。

```sh
python3 desktop/build_macos.py --python-runtime /path/to/portable-python --publish
```

测试时通过 RETAIL_LINK_TEST_ROOT 指向测试空间，隔离所有数据／导出／日志／临时目录；禁用持久WebKit数据。无该变量时使用系统用户目录。

Windows 当前产物为 x64 未签名试用包；SmartScreen 风险已在交付说明和 Release notes 中明确。macOS 当前产物覆盖 Intel Mac；Apple Silicon 需内置 arm64 或 universal 运行环境并在对应设备原生验证。平台通过状态只以该平台实机验收记录为准。
