# 零售通 Retail Link

本地电商经营工具：Excel导入、库存销量看盘、每日价格记录、问题追溯、历史、Excel安全导出与轻量数据迁移。

## 当前版本

Mac v0.1.7（Intel / macOS13+），功能基线 desktop-2。采用原生AppKit/WKWebView和内置Python，无需客户安装Python或Codex。当前为本机试用包；Developer ID公证和Apple Silicon原生包待对应验证。

Mac采用奇数补丁号，Windows采用紧随的偶数。本轮Windows配对v0.1.8；Windows需在Windows系统实现、打包和验收，当前没有Windows安装包。本机由Mac带头做功能、全功能分析和覆盖测试，Windows复用同一业务代码和功能清单，只替换平台交互层。

## 分支与源码版本

长期分支为 [main](https://github.com/ljy-codes/retail-link/tree/main)、[macos](https://github.com/ljy-codes/retail-link/tree/macos)、[windows](https://github.com/ljy-codes/retail-link/tree/windows)。main维护共享业务代码、测试和概览；平台分支保留完整工程，负责系统适配、构建与发布记录。Mac带头实现与验证，Windows同步同一功能基线。

当前Mac源码标签为 `v0.1.7-macos`，对应本轮构建与验收的共享提交。Windows0.1.8仍待实机适配与打包，无已发布Windows标签。完整规则见 [GitHub分支与版本规定](docs/GitHub分支与版本规定.md)。

11月22日答疑5项已全部确认并归档：净销量、单品85%、货损双轨、可选源写回、先人工下载。本轮已完成单品目标与任务闭环，净销量重建、货损双轨及源写回仍待后续，详见 [阶段性总结顶部](docs/阶段性总结.md)。

## 使用与交付

产品交付仅保留最新安装包DMG与版本对应的HTML使用说明，两文件。安装包与运行环境二进制不放入源码仓库。

[使用说明](desktop/使用说明.html) · [项目规划](docs/项目规划.html) · [进度记录](docs/task.md) · [桌面构建](desktop/README.md) · [跨平台验收清单](desktop/验收清单.md)

## 已实现

- 多文件／多工作表Excel，逐表确认纳入／忽略范围、人工字段映射及格式基线比较。
- 单品目标考核、逐款缺口合计、每日任务与未达标筛选；合计比例不替代逐款达标。
- 计划、库存、销量、出库、在途、货损看盘；缺失与零值分开，日期／单位／重复／标识冲突校验。
- 每日售价销量、历史快照与软删除恢复；JSON、SQLite、JSONL轻量存储。
- 五工作表Excel生成、原子保护、备份恢复、CSV下载、日志、应用数据迁移。
- 浅色／深色／系统、两组配色、阅读字号与玻璃效果开关。
- Mac独立窗口、原生文件对话框与下载、启动进度／重试、单实例、跨端口配置恢复。
- 退出与父进程管道EOF回收后端，无空闲轮询；应用资源与用户数据隔离。
- 普通／彻底卸载的确认与废纸篓恢复流程，源文件及生成文件保留；尚未实际执行完整安装／卸载矩阵。

## 开发与验证

开发浏览器入口：`python3 -m pip install -r requirements.txt`，然后 `python3 server.py --open`。桌面构建要求见desktop目录，不依赖客户机器上的Python。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

本机最终以内置运行环境完成184项全功能测试、23项新界面行为和16项外观测试，全部通过。包括9项桌面后端、5项现成App独立性／只读资源／元数据和源码一致性验证。真实原生操作覆盖Excel选择、目录取消、历史、CSV Blob保存、重启设置恢复、正常退出与强退回收。DMG校验、只读挂载及逐文件一致性通过。源码仓库不含业务原表，部分本机验收案例仅本机运行；现成App测试在未构建时明确跳过。

用户业务真实对账继续推进，技术测试不替代业务验收。已知原生下载的exFAT/FAT移动盘兼容、Apple Silicon、Windows、正式公证和完整安装升级／卸载测试留待对应环境完成。

## 长期工程约定

每日疑问最多5项，优先解决影响主线的问题；低优先级记录后推进阶段目标。源码与概览持续同步GitHub，原始资料、运行数据、凭据、测试产物与构建二进制不上传。
