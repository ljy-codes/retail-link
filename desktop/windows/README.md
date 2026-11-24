# Windows 工程与 desktop-4 配对状态

已合并Windows v0.1.10工程、源码标签和远端技术发布记录；人工安装/卸载体验仍被打回。当前v0.2.2是desktop-4配对目标，尚未完成本机Windows构建或实机验收。

当前build_windows.py只允许desktop-3，desktop-4会明确拒绝构建。以下保留0.1.10工程说明和0.2.2验收合同，不能将旧版技术结果当作新版本验收。

## 0.2.2 必须处理的适配

- frontendReady全程就绪及诊断协议；当前宿主仅按后端ready隐藏启动页，旧桥要求每条消息有id。
- 可选安装路径、默认安全卸载选择与结果页；将排除式清理改为明确名单，保护writeback/backups及writeback/operations恢复记录。
- Windows专用使用说明入口；已保留历史说明于使用说明-v0.1.10.html，不能把全局Mac手册直接打包给Windows。
- 客户指定红色图标；当前旧构建器仍会绘制青绿ICO，须在Windows适配中更新生成规则。
- 完整功能、性能与安装/升级/普通和彻底卸载矩阵在Windows环境验证后，才能登记v0.2.2标签或安装包。

## 0.1.10 历史工程说明

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


## desktop-4 目标验收合同

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
