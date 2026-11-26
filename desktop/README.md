# 零售通桌面版开发

> 2026-11-26 发布更新：Windows v0.2.4 已同步数据覆盖与可计算性报告，并完成技术构建、安装／启动／单实例／进程回收和普通／彻底卸载范围验证后发布 x64 安装包。客户此前的安装卸载体验反馈仍为未关闭的人工验收项，完整业务功能尚未验收。Mac v0.2.1 已验收源码仍锚定 v0.2.1-macos（fbb54cf）。

当前交付 Mac 0.2.1／Windows 0.2.4，功能基线 desktop-4。Mac 奇数、Windows 为大于配对 Mac 补丁号的偶数。版本号与功能清单是源码目标，不代表所有平台均已通过客户验收。

macOS 使用 AppKit／WKWebView 原生壳，内置 Python 仅监听系统分配的 127.0.0.1 端口。共享 server/backend/web 只维护一套业务算法。Windows 复用同一清单、同一虚构样本和 desktop-4 协议，宿主使用 WinForms／WebView2；v0.2.4 已完成数据健康接口和 Windows 技术验收，安装位置、启动与快捷方式选项、卸载方式和结果反馈均可实际操作。

## 构建

构建需要 Intel Mac、macOS 13+、Swift 5.8+、可迁移 Python 3.12、openpyxl／et_xmlfile 和 Pillow。用户不需要 Python 或终端。

```sh
python3 -B desktop/build_macos.py --python-runtime /path/to/portable-python --publish
```

编译包含 macos/Main.swift 和 macos/LifecycleSupport.swift。图标由构建脚本绘制客户指定的红色主底、白色经营折线图与数据点，生成 PNG、ICNS、ICO。产品交付仍仅最新 DMG 与对应 HTML 两文件。构建缓存、App 与中间图标位于 desktop/build，不进源码交付。

## 安装、升级与卸载契约

DMG 可以拖拽到用户选择的实际位置，也可打开 App 后用“零售通 → 安装与升级…”选择可写目录。安装流程显示来源、实际目标和版本；复制校验、codesign 完整性核对后才发布。符号链接目录、无权限位置、其他同名应用和正在运行的目标不允许覆盖。升级从新版 DMG 打开，选择原安装目录；当前运行的旧 App 必须先退出。旧 App 保留在同目录“零售通-升级前-…”副本，恢复前先退出新版。安装不自动删除旧备份，也不变更用户业务文件。

启动时 desktop-install.json 记录实际 Bundle 路径；路径以当前运行 Bundle 为准，不假定 /Applications。安装完成可立即启动、打开目标目录或完成。首次引导说明用途、三步操作、本地数据位置、原件默认只读及联网边界。

卸载采用明确单选，默认普通卸载；下一步列出实际清理路径，再确认。普通仅移走当前 App；彻底卸载只移走明确白名单中的应用数据、配置、历史、缓存、日志。原件、导入副本、生成文件、exports 中的文件备份与未知文件保留。经营账本仅清理 operations/state.json，写回配置仅清理 writeback/settings.json 和 writeback/templates.json；不删除其父目录，writeback/backups 原件备份及 writeback/operations 恢复元数据始终保留。文件移入废纸篓后，结果显示成功或失败、实际移动位置和遗留项；恢复凭据写到 Documents/零售通/卸载记录。测试不对真实安装或用户资料执行卸载。

## 启动与诊断

前端可操作后发送：

```js
window.webkit.messageHandlers.retailLinkDesktop.postMessage({action: 'frontendReady'});
```

原生验证消息来自当前本地页面。ready-file、页面导航完成均不等于业务入口可用；总超时一直保留到 frontendReady。40 秒为错误保护上限，8 秒提示仍在等待，均不是已经测定的性能合格线。阶段日志写入 ~/Library/Logs/零售通/startup.jsonl，菜单可定位；日志含过程、版本、进程、耗时及失败阶段，不写工作簿内容或凭据。ready 后取消启动计时，不新增空闲业务轮询。

## 隔离验收

RETAIL_LINK_TEST_ROOT 必须指向测试空间或临时目录，隔离支持目录、导出、日志和临时目录，禁用持久 WebKit 数据。带此环境并传 --lifecycle-preview 可只打开生命周期预览、不启动业务后端。虚构目录测试：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s 测试空间 -p test_v02_desktop.py -v
```

从仓库运行时按实际测试目录调整路径。现成 App 专项测试前先从 DMG 恢复或重建 App；不在签名包中写 pycache。

## 性能测量

measure_resources.py 按 PID 及其子进程采样，OS 重新托管的 WebKit／WebView 辅助进程须先核实归属，再通过 --include-pid 明确加入。不能把单个主进程截图当完整占用。示例：

```sh
python3 -B desktop/measure_resources.py --pid 1234 --label idle --version 0.2.1 --duration 60 --out /tmp/retail-idle.json
```

固定设备、系统版本、应用构建、样本和静置条件；分别记录 first（全新隔离数据）、cold（重启系统后的首轮）、warm（退出后再次启动）、idle（主界面准备后固定静置）、long-idle（固定长期静置）。脚本不代替系统重启，也不把热启动冒充冷启动。macOS 记录逐进程 RSS 合计（共享页可能重复计数）；Windows 记录 Working Set 与 Private Bytes；两者不直接当同一数值比较。CPU 来自区间累计处理器时间；磁盘读写未采集时明确为 null，需 OS 跟踪补证据。脚本不生成伪造阈值或自动判通过；先实测、再冻结各平台设备基线与回归门槛。55.4MB 主进程截图不足以判超标。

## 分发边界

Mac 本地构建仍仅 Intel Mac；ad-hoc 签名验证只保证包完整性，不等于 Developer ID 公证。arm64／Universal 和正式公证继续单独验收。Windows v0.2.4 已提供 x64 安装包并完成技术复验，但客户 GUI 安装／卸载体验、完整业务功能和首次／冷／热启动性能矩阵仍未验收；安装包未代码签名，需要 WebView2。


下载发布先在目标同目录创建唯一私有暂存文件，流式复制、fsync 与哈希重读后，以 RENAME_EXCL 排他原子发布；最终文件名在完整数据就绪前不可见，源暂存文件由调用方统一清理。跨盘不直接写最终目标。若文件系统不支持排他原子重命名，明确失败并清理本次私有暂存，不退为暴露半写目标。物理 FAT/exFAT 仍需实机验证，不能仅凭强制复制测试宣称兼容通过。
