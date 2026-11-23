# 零售通桌面版开发

版本配对按补丁号：Mac 奇数、Windows 紧随偶数；本轮 Mac 0.1.9 对应 Windows 0.1.10，统一功能级别 desktop-3。Mac 负责带头实现与全功能分析、覆盖测试；Windows 在 Windows 环境复用同一后端和前端，完成平台适配、对照验收与安装包。

macOS 使用 AppKit / WKWebView 原生壳，Python 后端仅监听系统分配的127.0.0.1端口；ready-file就绪后停止启动轮询。前端使用原生文件桥，所有设置持久化到应用数据，退出与父进程管道断开均回收后端。

构建需要 Intel Mac/macOS13+、Swift5.8+、可迁移的Python3.12运行环境、openpyxl、et_xmlfile及Pillow（仅绘制构建图标）。用户安装后不需要Python或构建工具。运行：

```sh
python3 desktop/build_macos.py --python-runtime /path/to/portable-python --publish
```

或运行开发空间/prepare_delivery.py。产品交付只保留最新DMG与对应HTML说明两文件。构建缓存、.app、磁盘镜像暂存均在desktop/build，测试日志与虚构资料均在测试空间，不上传GitHub。

测试时通过 RETAIL_LINK_TEST_ROOT 指向测试空间，隔离所有数据／导出／日志／临时目录；禁用持久WebKit数据。无该变量时使用系统用户目录。

当前产物仅覆盖 Intel Mac；Apple Silicon需内置arm64或universal运行环境并在相应设备原生验证。安装包用本机ad-hoc签名，Developer ID公证由正式分发阶段处理。Windows安装包由Windows环境完成，本仓库只提供共用功能协议与验收清单，不能把未执行平台标成已通过。
