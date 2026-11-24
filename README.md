# 零售通 Retail Link

本地电商经营工具：Excel导入、库存销量看盘、每日价格记录、问题追溯、历史、Excel安全导出与轻量数据迁移。

## 当前版本

Windows v0.1.10（Windows 10/11 x64），配对 Mac v0.1.9，共享功能基线 desktop-3。新增双层日期价量表、持久商品关联、导入差异核对及筛选日记录 Excel/CSV 导出，并修复组合历史原件保护。Windows 使用 WinForms + WebView2 原生壳和内置 Python，无需客户安装 Python 或 Codex；依赖 Microsoft Edge WebView2 Runtime。当前安装包尚未代码签名，可能触发 SmartScreen 提示。

Mac 采用奇数补丁号，Windows 采用紧随的偶数。Windows 0.1.10 已在 Windows 环境完成源码适配、构建、真实 GUI 与安装升级／卸载验收；交付目录只保留安装包和 HTML 使用说明两个文件。未签名的 Windows 试用包不等同于正式签名、公证或组织级软件分发审批。

## 分支与源码版本

长期分支为 [main](https://github.com/ljy-codes/retail-link/tree/main)、[macos](https://github.com/ljy-codes/retail-link/tree/macos)、[windows](https://github.com/ljy-codes/retail-link/tree/windows)。main维护共享业务代码、测试和概览；平台分支保留完整工程，负责系统适配、构建与发布记录。Mac带头实现与验证，Windows同步同一功能基线。

Mac 源码标签为 `v0.1.9-macos`；Windows 0.1.10 构建验收后创建 `v0.1.10-windows` 并发布安装包与使用说明。main、macos、windows 同步同一业务基线，平台分支保留各自系统适配和发布记录。完整规则见 [GitHub分支与版本规定](docs/GitHub分支与版本规定.md)。

11月22日答疑5项已全部确认并归档：净销量、单品85%、货损双轨、可选源写回、先人工下载。本轮已完成单品目标与任务闭环，净销量重建、货损双轨及源写回仍待后续，详见 [阶段性总结顶部](docs/阶段性总结.md)。

11月23日[当前答疑](docs/答疑文档/零售通-20261123.xlsx)仅3项；[版本分析与实施进度](docs/下一版本分析-20261123.md)以总规划v0.2为主线。已有原表适配与商品关联已在0.1.9本机交付，下一步推进发货/售后及净销量真实对账。细节分批补充，不等待全部答完。真实价量样表仅有结构、数值为空，本轮数值闭环用虚构样本验证。

## 使用与交付

Windows 产品交付仅保留 `零售通-v0.1.10-windows-x64-setup.exe` 与 `零售通-v0.1.10-使用说明.html` 两个文件；安装包、运行环境二进制和用户数据不放入源码仓库。GitHub Release 同样只上传这两个资产，并附最终 SHA-256、验证范围、WebView2 依赖和未签名风险说明。

[使用说明](desktop/使用说明.html) · [项目规划](docs/项目规划.html) · [进度记录](docs/task.md) · [桌面构建](desktop/README.md) · [跨平台验收清单](desktop/验收清单.md)

## 已实现

- 多文件／多工作表Excel，逐表确认纳入／忽略范围、人工字段映射及格式基线比较。
- 单品目标考核、逐款缺口合计、每日任务与未达标筛选；合计比例不替代逐款达标。
- 计划、库存、销量、出库、在途、货损看盘；缺失与零值分开，日期／单位／重复／标识冲突校验。
- 每日售价销量、历史快照与软删除恢复；JSON、SQLite、JSONL轻量存储。
- 双层日期价量原表、合并仓位、持久商品关联、逐条旧新差异预览、重复幂等及日记录筛选Excel/CSV导出。
- 组合历史恢复后保护全部输入原件及硬链接，预览、提交与实际发布前核对目标变化。
- 五工作表Excel生成、原子保护、备份恢复、CSV下载、日志、应用数据迁移。
- 浅色／深色／系统、两组配色、阅读字号与玻璃效果开关。
- Windows 独立窗口、WebView2 页面、原生文件／目录／另存对话框、启动进度与重试、单实例、跨端口配置恢复。
- 内置 Python 子进程由原生壳管理；ready 文件校验 PID、版本和本机 URL，正常退出回收子进程并清理临时就绪目录。
- 当前用户安装、升级覆盖保留应用数据；普通／彻底卸载尽量移入回收站，用户原始资料与 Documents 生成文件不随卸载删除。
- 退出与父进程管道EOF回收后端，无空闲轮询；应用资源与用户数据隔离。
- 普通／彻底卸载的确认与 Windows 回收站恢复流程，源文件及生成文件保留。

## 开发与验证

开发浏览器入口：`python -m pip install -r requirements.txt`，然后 `python server.py --open`。Windows 构建要求 .NET 10 SDK、Python 3.12 和 openpyxl，不依赖客户机器上的 Python；完整命令见 [Windows 构建说明](desktop/windows/README.md)。

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests -v
python -B desktop\windows\build_windows.py --publish --keep-build
```

本轮完整 Python 回归 171 项通过、2 项环境跳过；Windows 包契约 8 项通过；日记录界面 16/16、单品界面 23/23，共 39 项界面脚本通过。最终安装包 SHA-256 和真实 GUI 检查范围记录于 [平台发布记录](docs/平台发布记录.md)。源码仓库不含业务原表与构建二进制；真实价量样表价量为空，仅完成结构核对，数值流程以隔离虚构样本验证。自动化测试不能替代全部真实业务 Excel 对账。

用户业务真实对账继续推进，技术测试不替代业务验收。Windows 已知限制包括未代码签名、依赖 WebView2 Runtime、仅提供 x64 包，以及尚未用生产业务资料完成净销量与货损对账。

## 长期工程约定

每日疑问最多5项，优先解决影响主线的问题；低优先级记录后推进阶段目标。源码与概览持续同步GitHub，原始资料、运行数据、凭据、测试产物与构建二进制不上传。
