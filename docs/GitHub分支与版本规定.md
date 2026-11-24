# 零售通 GitHub 分支与版本规定

更新：2026-11-24。长期只保留 `main`、`macos`、`windows` 三条分支，默认分支为 `main`。

| 分支 | 用途 |
| --- | --- |
| main | 完整工程的共享基线：server、backend、web、跨平台功能清单、通用测试、概览与答疑。共享业务规则只维护一份。 |
| macos | 基于main的完整工程，维护Mac系统交互、应用与DMG构建、签名、Mac验证及发布记录。 |
| windows | 基于main的完整工程，维护Windows系统交互、安装卸载、Windows验证及发布记录。当前已生成 v0.1.10 安装包并完成 Windows 实机验收。 |

## 开发顺序

Mac带头做功能、业务分析和覆盖测试；共享改动先进入main，验证后同步两个平台分支。平台差异限定为系统接口、路径、容器、打包、签名与交付脚本，不能各自改一套库存、销量、计划或货损算法。平台分支保留完整工程，新增共享改动必须回到main再同步。

发布前同步main，使用同一功能清单与虚构样本对照结果，核对desktop/验收清单.md。临时feature/fix/codex分支完成合并或交付后清理，旧版通过提交历史追溯，不另建长期版本分支。

## 本项目版本规则

- 采用X.Y.Z；Mac的Z为奇数，Windows为紧随的偶数。当前交付 Mac0.1.9 与 Windows0.1.10，功能基线desktop-3。
- 标签必须带平台后缀：`v<X.Y.Z>-macos`、`v<X.Y.Z>-windows`。
- Mac源码标签为`v0.1.9-macos`；Windows0.1.10完成构建、GUI、升级和两类卸载验收后创建`v0.1.10-windows`。旧版本源码继续从Git提交历史追溯，不重写历史提交。
- Windows 分支根`版本.json`写入`version=0.1.10`、`platform=windows`、`featureLevel=desktop-3`、`pairedMacVersion=0.1.9`，并补充`buildStatus=built-verified`；共享代码和Windows工程同步到main。
- 每个平台长期只保留最新交付标签和下载入口；删除旧入口前先确认新版完整。历史源码始终从Git提交记录追溯，不重写历史提交。

## 交付与同步

客户本地“产品交付”只保留最新安装包和对应HTML说明两文件。App、DMG、运行环境二进制、业务原表、运行数据、凭据及测试产物不提交到源码树。用户答疑和概览按既有授权及时同步；答疑已确认不等于功能已实现。

Windows0.1.10经用户授权补齐发布：同步共享代码、Windows工程、文档与根版本元数据到`windows`和`main`，不修改`macos`分支，不更新Mac交付。创建`v0.1.10-windows`标签和Release，Release只上传安装包与HTML说明两个资产，并标明平台版本、共享源码、构建环境、验证范围、SHA-256、WebView2依赖及未签名风险。未完成平台不得放入已发布列表。

规则参考：https://github.com/ljy-codes/api-workbench/blob/main/docs/platform-branch-and-version-policy.md 。本项目的Mac奇数／Windows偶数和“两文件交付”沿用用户已确定的约定。
