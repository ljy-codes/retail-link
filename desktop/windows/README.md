# Windows 桌面版构建与验收

Windows v0.1.10 对应 Mac v0.1.9，功能基线 `desktop-3`。补丁版本号 Mac 使用奇数、Windows 使用紧随其后的偶数；共享业务代码继续以 Mac 端功能与覆盖测试为基线，Windows 只维护平台容器、文件桥、进程管理、安装卸载和发布适配。

工程复用同一 `server.py`、`backend/`、`web/` 与 `desktop/release.json` 功能清单，不单独复制修改业务算法。

## 目录与产物

```text
desktop/windows/
  app/          WinForms + WebView2 应用
  installer/    当前用户安装程序
  uninstall/    普通／彻底卸载脚本
  build_windows.py
```

构建输出位于 `desktop/build/windows/`，正式交付只复制安装包和 HTML 到根目录 `产品交付/`。应用、Python 运行时、构建缓存和测试数据不提交源码仓库。

## 运行协议

内置 Python 启动：

```text
server.py --desktop --parent-stdin --port 0 --data-dir ... --original-dir ...
          --export-dir ... --ready-file ...
```

原生壳读取 `{url,version,pid}`，校验本机 HTTP 地址、进程 PID 和版本后显示 WebView2。应用元数据写入 `version=0.1.10`、`platform=windows`、`featureLevel=desktop-3` 和 `pairedMacVersion=0.1.9`。

WebView2 注入 `window.webkit.messageHandlers.retailLinkDesktop.postMessage` 兼容入口，转发到宿主消息通道；结果通过 `window.RetailLinkDesktop.resolve` 返回。文件桥支持文件／目录选择、另存和定位文件夹。

## 用户路径

- 程序：`%LOCALAPPDATA%\Programs\零售通\`
- 应用数据：`%LOCALAPPDATA%\零售通\`
- 原始资料：`%LOCALAPPDATA%\零售通\原始资料\`
- 默认生成文件：`%USERPROFILE%\Documents\零售通\生成文件\`

安装按当前用户进行，开始菜单快捷方式写入用户目录，卸载项写入 HKCU。普通卸载保留应用数据；彻底卸载清理应用数据，但保留原始资料、导入文件、生成文件与 Documents 用户文件。

## 构建

要求 Windows 10/11 x64、.NET 10 SDK、Python 3.12 与 openpyxl／et_xmlfile。构建和真实使用依赖 Microsoft Edge WebView2 Runtime。

```powershell
python -B -m unittest tests.test_windows_package -v
python -B desktop\windows\build_windows.py --publish --keep-build
```

构建冒烟测试会启动内置 Python，验证健康接口、业务模板导入、缺少样例时的明确 400 和 JSON 404 响应路径。生成后必须执行真实安装、GUI、单实例、原生保存、退出回收、升级、普通卸载和彻底卸载检查，不能以静态测试替代。

当前安装包未代码签名，可能触发 SmartScreen；发布说明必须提供最终 SHA-256、验证范围、WebView2 依赖和已知限制。
