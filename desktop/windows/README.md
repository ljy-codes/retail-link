# Windows v0.2.4 工程与发布状态

更新：2026-11-26（Asia/Shanghai）。Windows v0.2.4 配对 Mac v0.2.1、功能基线 desktop-4，已同步数据覆盖与可计算性报告，并完成技术构建、自动回归和本机真实安装／启动／卸载复验。客户人工 GUI 安装／卸载体验和完整业务功能仍待复验，不能把技术通过写成客户验收通过。

`build_windows.py` 接受 desktop-4；Windows补丁号必须是大于配对Mac补丁号的偶数，允许后续Windows-only维护版本按2递增，不虚构未构建的Mac版本。源码、安装器和卸载器测试覆盖 Windows v0.2.4，v0.2.2及v0.1.10历史说明继续保留在下方。

## 0.2.4 技术验收结果

- 合入共享数据覆盖健康报告：`/api/health/report`基于已保存快照展示来源、观察日期、日期缺口、仓库范围、字段缺失、指标状态和补资料动作，不把缺失值当0。
- Windows包、数据健康与HTTP专项24项通过；Python全量280项通过、7项环境跳过。
- 最终包在 `D:\Program Files\零售通` 安装成功，版本 `0.2.4`、HKCU实际路径、桌面和开始菜单快捷方式均核对一致；应用实际启动后端约3.2秒就绪，健康接口返回desktop-4及`data_health`能力。
- 第二实例退出码0；正常退出后主进程、内置Python、监听端口和ready文件均回收。
- 普通卸载保留5类用户资料哨兵；彻底卸载按白名单清理配置、历史、日志、缓存、操作库和写回设置，保留原始资料、exports、写回备份、未知业务文件、`daily.sqlite3`及Documents用户文件。
- 本次主进程采样Working Set约144.9 MB、Private约64.7 MB，Python Working Set约42.3 MB；不是完整进程树矩阵，不表示启动慢或内存问题已解决。

## 0.2.2 历史技术验收结果

- `frontendReady` 支持不带 `id`，总启动保护持续到页面可操作；启动页显示“运行环境 → 后端就绪 → 页面加载 → 可操作”和耗时，菜单可打开启动诊断。
- 最终包在 `D:\Program Files\零售通` 静默安装成功，中文空格路径、HKCU 实际路径、版本 0.2.2、桌面和开始菜单快捷方式均核对一致。
- 实际启动约 2.43 秒到达可操作；健康接口返回 desktop-4；第二实例退出码 0；正常退出后端口、ready 文件和应用进程均回收。
- 普通卸载退出码 0，程序、快捷方式和登记删除，已有 197 个应用数据文件哈希全部保留；彻底卸载按白名单清理并保留原始资料、exports、写回备份和恢复记录。
- 空闲完整进程树实测 9 个进程，Working Set 约 649.9 MB、Private 约 314.4 MB。用于记录“仅启动内存偏高”的真实反馈，不表示性能已优化或达到验收阈值。
- 安装包未签名、需要 WebView2、仅 x64；客户安装／卸载体验及完整业务验收仍为未完成项。

## 0.2.2 已完成适配

- 可选安装位置、可写权限与空间检查、实际路径登记和从登记路径卸载。
- 立即启动、桌面快捷方式和开始菜单快捷方式可独立选择并实际执行。
- Windows 专用使用说明、产品简介、首次使用、数据位置和联网边界说明。
- 普通／彻底卸载互斥选择、彻底卸载二次确认、清理范围摘要和结果／失败反馈。
- 客户指定红色折线图标、启动阶段诊断和 app 进程回收。

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


## desktop-4 验收合同与外部反馈

# Windows 跟随实现与交付约定

目标：Windows v0.2.4 配对 Mac v0.2.1，featureLevel=desktop-4。Windows 宿主、安装器和安装包已技术验收并发布；以下内容仍作为客户复验和后续回归的合同，不因技术构建完成而关闭人工验收项。

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

人工验收bug.md 中 Windows 安装卸载被打回、启动体感慢与内存占用截图属于外部反馈；v0.2.2已重做安装／卸载，v0.2.4继续保留并复验。v0.2.2曾测得空闲完整进程树9个进程、Working Set约649.9 MB、Private约314.4 MB；v0.2.4本次仅采样主进程和内置Python，仍需按first/cold/warm/idle/long-idle建立冻结设备和门槛，不以单次样本或单进程截图宣称性能达标。
