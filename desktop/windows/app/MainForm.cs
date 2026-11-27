using System.Diagnostics;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace RetailLink.Windows;

internal sealed class MainForm : Form
{
    private readonly WebView2 _webView = new() { Dock = DockStyle.Fill, Visible = false };
    private readonly Panel _loading = new() { Dock = DockStyle.Fill, BackColor = Color.FromArgb(247, 248, 250) };
    private readonly Label _status = new();
    private readonly Label _detail = new();
    private readonly Label _stages = new();
    private readonly Button _retry = new();
    private readonly Button _quit = new();
    private readonly BackendManager _backend = new();
    private readonly Dictionary<Form, FeatureWindow> _featureWindows = new();
    private readonly string _version;
    private readonly Stopwatch _startupWatch = new();
    private NativeBridge? _bridge;
    private CoreWebView2Environment? _webViewEnvironment;
    private TaskCompletionSource<bool>? _navigationCompleted;
    private TaskCompletionSource<bool>? _frontendReady;
    private CancellationTokenSource? _startupCancellation;
    private Exception? _webViewFailure;
    private string _startupRunId = "";
    private bool _startupRunning;
    private bool _closing;
    private bool _allowClose;

    public MainForm()
    {
        Text = "零售通";
        Icon = LoadApplicationIcon();
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(900, 640);
        ClientSize = new Size(1280, 850);
        BackColor = Color.FromArgb(247, 248, 250);
        Font = new Font("Microsoft YaHei UI", 9F);
        _version = ReadVersion();
        BuildMenu();
        BuildLoadingView();
        Controls.Add(_loading);
        Controls.Add(_webView);
        _loading.BringToFront();
        _backend.Exited += OnBackendExited;
        Shown += async (_, _) => await StartWorkspaceAsync();
    }

    private void BuildMenu()
    {
        var menu = new MenuStrip { Dock = DockStyle.Top };
        var application = new ToolStripMenuItem("应用");
        application.DropDownItems.Add("关于零售通", null, (_, _) => ShowAbout());
        application.DropDownItems.Add(new ToolStripSeparator());
        application.DropDownItems.Add("安装与升级…", null, (_, _) => OpenInstallUpgrade());
        application.DropDownItems.Add("使用与数据说明…", null, (_, _) => ShowOnboarding());
        application.DropDownItems.Add("显示启动诊断", null, (_, _) => OpenStartupDiagnostics());
        application.DropDownItems.Add(new ToolStripSeparator());
        application.DropDownItems.Add("卸载零售通…", null, (_, _) => OpenUninstallSettings());
        application.DropDownItems.Add(new ToolStripSeparator());
        application.DropDownItems.Add("退出", null, (_, _) => Close());
        menu.Items.Add(application);

        var edit = new ToolStripMenuItem("编辑");
        edit.DropDownItems.Add(EditorMenuItem("撤销", Keys.Control | Keys.Z, "undo"));
        edit.DropDownItems.Add(EditorMenuItem("剪切", Keys.Control | Keys.X, "cut"));
        edit.DropDownItems.Add(EditorMenuItem("复制", Keys.Control | Keys.C, "copy"));
        edit.DropDownItems.Add(EditorMenuItem("粘贴", Keys.Control | Keys.V, "paste"));
        edit.DropDownItems.Add(EditorMenuItem("全选", Keys.Control | Keys.A, "selectAll"));
        menu.Items.Add(edit);

        var window = new ToolStripMenuItem("窗口");
        window.DropDownItems.Add("最小化", null, (_, _) => MinimizeActiveWindow());
        window.DropDownItems.Add("缩放", null, (_, _) => ToggleActiveWindowSize());
        window.DropDownItems.Add(new ToolStripSeparator());
        window.DropDownItems.Add("前置全部窗口", null, (_, _) => BringAllWindowsToFront());
        menu.Items.Add(window);

        var help = new ToolStripMenuItem("帮助");
        help.DropDownItems.Add("显示本地数据位置", null, (_, _) => ShowDataDirectory());
        help.DropDownItems.Add("打开使用说明", null, (_, _) => OpenManual());
        help.DropDownItems.Add("显示启动诊断", null, (_, _) => OpenStartupDiagnostics());
        menu.Items.Add(help);

        MainMenuStrip = menu;
        Controls.Add(menu);
    }

    private ToolStripMenuItem EditorMenuItem(string title, Keys shortcut, string command)
    {
        var item = new ToolStripMenuItem(title)
        {
            ShortcutKeys = shortcut,
        };
        item.Click += (_, _) => ExecuteEditorCommand(command);
        return item;
    }

    private void BuildLoadingView()
    {
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 7,
            Padding = new Padding(48),
            BackColor = _loading.BackColor,
        };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));

        var title = new Label
        {
            Text = "零售通",
            AutoSize = true,
            Anchor = AnchorStyles.None,
            Font = new Font("Microsoft YaHei UI", 28F, FontStyle.Bold),
            ForeColor = Color.FromArgb(24, 40, 56),
        };
        _status.Text = "正在启动本地工作空间…";
        _status.AutoSize = true;
        _status.Anchor = AnchorStyles.None;
        _status.Font = new Font("Microsoft YaHei UI", 12F, FontStyle.Bold);
        _status.ForeColor = Color.FromArgb(34, 156, 157);
        _detail.Text = "正在准备内置运行环境和本地数据，请稍候。";
        _detail.AutoSize = true;
        _detail.MaximumSize = new Size(720, 0);
        _detail.TextAlign = ContentAlignment.MiddleCenter;
        _detail.Anchor = AnchorStyles.None;
        _detail.ForeColor = Color.FromArgb(95, 105, 116);
        _stages.Text = "○ 运行环境    ○ 后端就绪    ○ 页面加载    ○ 可操作";
        _stages.AutoSize = true;
        _stages.Anchor = AnchorStyles.None;
        _stages.ForeColor = Color.FromArgb(120, 108, 112);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            Anchor = AnchorStyles.None,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = false,
        };
        _retry.Text = "重新启动";
        _retry.AutoSize = true;
        _retry.Padding = new Padding(10, 4, 10, 4);
        _retry.Visible = false;
        _retry.Click += async (_, _) => await StartWorkspaceAsync();
        _quit.Text = "退出";
        _quit.AutoSize = true;
        _quit.Padding = new Padding(10, 4, 10, 4);
        _quit.Visible = false;
        _quit.Click += (_, _) => Close();
        buttons.Controls.Add(_retry);
        buttons.Controls.Add(_quit);

        layout.Controls.Add(title, 0, 1);
        layout.Controls.Add(_status, 0, 2);
        layout.Controls.Add(_detail, 0, 3);
        layout.Controls.Add(_stages, 0, 4);
        layout.Controls.Add(buttons, 0, 5);
        _loading.Controls.Add(layout);
    }

    private async Task StartWorkspaceAsync()
    {
        if (_closing || _startupRunning)
        {
            return;
        }
        _startupRunning = true;
        _webViewFailure = null;
        _retry.Enabled = false;
        _quit.Visible = false;
        _startupWatch.Restart();
        _startupRunId = Guid.NewGuid().ToString("N")[..8];
        _navigationCompleted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        _frontendReady = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        // 记录启动阶段、实际耗时、失败原因和运行标识，供菜单“显示启动诊断”读取。
        RecordStartupStage("开始启动", $"版本 {_version}");
        ShowStage("运行环境", "正在准备 WebView2 和内置运行环境。", "running");
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(45));
        _startupCancellation = timeout;
        try
        {
            var webViewTask = InitializeWebViewAsync();
            ShowStage("后端就绪", "正在启动随应用安装的内置本地服务。", "environment");
            var address = await _backend.StartAsync(_version, timeout.Token);
            RecordStartupStage("后端就绪", address.AbsoluteUri);
            await webViewTask.WaitAsync(timeout.Token);
            RecordStartupStage("运行环境", "WebView2 已初始化");

            ShowStage("页面加载", "正在加载零售通工作空间，并等待关键数据与操作就绪。", "backend");
            _webView.CoreWebView2.Navigate(address.AbsoluteUri);
            var ready = Task.WhenAll(_navigationCompleted.Task, _frontendReady.Task);
            await ready.WaitAsync(timeout.Token);
            if (!_navigationCompleted.Task.Result)
            {
                throw new InvalidOperationException("页面加载未完成，请重新启动。");
            }
            RecordStartupStage("可操作", "页面已发出 frontendReady，关键数据和操作已就绪");
            ShowStage("可操作", "页面已可操作。", "ready");
            _webView.Visible = true;
            _loading.Visible = false;
            ShowOnboardingIfNeeded();
        }
        catch (WebView2RuntimeNotFoundException)
        {
            await _backend.StopAsync();
            RecordStartupStage("失败", "缺少 Microsoft Edge WebView2 Runtime");
            ShowLoading(
                "缺少 Microsoft Edge WebView2 运行环境",
                "请安装 Microsoft Edge WebView2 Runtime 后，点击“重新启动”。Windows 10/11 通常已随 Edge 提供。也可点击菜单“应用 → 显示启动诊断”查看日志。",
                failed: true);
        }
        catch (OperationCanceledException) when (_webViewFailure is null)
        {
            await _backend.StopAsync();
            RecordStartupStage("启动超时", "45 秒内没有收到页面 frontendReady");
            ShowLoading(
                "启动超时",
                "45 秒内未完成“运行环境 → 后端就绪 → 页面加载 → 可操作”。请点击“重新启动”；若重复失败，打开“应用 → 显示启动诊断”查看阶段日志。",
                failed: true);
        }
        catch (Exception error)
        {
            await _backend.StopAsync();
            error = _webViewFailure ?? error;
            RecordStartupStage("失败", error.ToString());
            ShowLoading("本地工作空间尚未就绪", error.Message, failed: true);
        }
        finally
        {
            _startupCancellation = null;
            _retry.Enabled = true;
            _startupRunning = false;
        }
    }

    private async Task InitializeWebViewAsync()
    {
        if (_webView.CoreWebView2 is not null)
        {
            return;
        }
        var locations = RuntimeLocations.Create();
        var userData = Path.Combine(locations.Data, "WebView2");
        Directory.CreateDirectory(userData);
        var environment = await CoreWebView2Environment.CreateAsync(null, userData);
        _webViewEnvironment = environment;
        await _webView.EnsureCoreWebView2Async(environment);
        var core = _webView.CoreWebView2
            ?? throw new InvalidOperationException("WebView2 未能初始化。");
        ConfigureWebView(core);
        _bridge = new NativeBridge(_webView, this, () => _backend.BackendUri?.Port);
        _bridge.FrontendReady += () =>
        {
            RecordStartupStage("可操作", "收到 frontendReady");
            _frontendReady?.TrySetResult(true);
        };
        await InstallDesktopScriptAsync(core);
    }

    private void ConfigureWebView(CoreWebView2 core)
    {
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreDefaultContextMenusEnabled = true;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.IsZoomControlEnabled = true;
        core.Settings.AreHostObjectsAllowed = false;
        core.Settings.IsGeneralAutofillEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.Settings.IsWebMessageEnabled = true;
        core.NewWindowRequested += OnNewWindowRequested;
        core.NavigationStarting += OnNavigationStarting;
        core.NavigationCompleted += OnNavigationCompleted;
        core.DownloadStarting += OnDownloadStarting;
        core.ProcessFailed += OnWebViewProcessFailed;
    }

    private Task<string> InstallDesktopScriptAsync(CoreWebView2 core)
    {
        return core.AddScriptToExecuteOnDocumentCreatedAsync(
            $$"""
            window.webkit = window.webkit || {};
            window.webkit.messageHandlers = window.webkit.messageHandlers || {};
            window.webkit.messageHandlers.retailLinkDesktop = {
              postMessage: function (message) { window.chrome.webview.postMessage(message); }
            };
            window.__RETAIL_LINK_DESKTOP__ = Object.freeze({platform:'windows',version:{{JsonSerializer.Serialize(_version)}}});
            """);
    }

    private async void OnWebViewProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs args)
    {
        var featureWindow = FindFeatureWindow(sender);
        if (featureWindow is not null)
        {
            if (args.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited)
            {
                await CloseFeatureWindowAsync(
                    featureWindow,
                    "独立工作窗口的页面进程已停止，页面已恢复到右侧展示区。");
            }
            return;
        }
        if (_closing || _webViewFailure is not null
            || args.ProcessFailedKind != CoreWebView2ProcessFailedKind.RenderProcessExited)
        {
            return;
        }
        var error = new InvalidOperationException("页面渲染进程已停止，请点击“重新启动”。");
        _webViewFailure = error;
        if (_startupCancellation is not null)
        {
            // Let the existing startup failure path reclaim the child exactly once.
            _startupCancellation.Cancel();
            return;
        }
        _retry.Enabled = false;
        try
        {
            await _backend.StopAsync();
            if (!_closing)
            {
                RecordStartupStage("失败", error.ToString());
                ShowLoading("本地工作空间尚未就绪", error.Message, failed: true);
            }
        }
        finally
        {
            _retry.Enabled = true;
        }
    }

    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs args)
    {
        if (IsLocal(args.Uri) || args.Uri == "about:blank")
        {
            return;
        }
        args.Cancel = true;
        OpenExternal(args.Uri);
    }

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs args)
    {
        if (!ReferenceEquals(sender, _webView.CoreWebView2))
        {
            return;
        }
        if (args.IsSuccess)
        {
            RecordStartupStage("页面加载", $"导航完成，耗时 {_startupWatch.Elapsed.TotalSeconds:0.00} 秒");
            _navigationCompleted?.TrySetResult(true);
            return;
        }
        RecordStartupStage("页面加载失败", $"WebView2 错误代码：{args.WebErrorStatus}");
        _navigationCompleted?.TrySetResult(false);
        ShowLoading("页面加载未完成", $"WebView2 错误代码：{args.WebErrorStatus}", failed: true);
    }

    private async void OnNewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs args)
    {
        using var deferral = args.GetDeferral();
        args.Handled = true;
        if (string.Equals(args.Uri, "about:blank", StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                var windowName = string.IsNullOrWhiteSpace(args.Name)
                    ? $"retail-link-feature-{Guid.NewGuid():N}"
                    : args.Name;
                var featureWindow = await CreateFeatureWindowAsync(windowName);
                args.NewWindow = featureWindow.WebView.CoreWebView2
                    ?? throw new InvalidOperationException("独立工作窗口的 WebView2 未能初始化。");
                featureWindow.Form.Activate();
            }
            catch (Exception error)
            {
                MessageBox.Show(this, $"无法打开独立工作窗口：{error.Message}",
                    "零售通", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            return;
        }

        var source = sender as CoreWebView2;
        if (IsLocal(args.Uri))
        {
            source?.Navigate(args.Uri);
        }
        else
        {
            OpenExternal(args.Uri);
        }
    }

    private void OnDownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs args)
    {
        var owner = FindFeatureWindow(sender)?.Form ?? this;
        if (!IsLocalDownload(args.DownloadOperation.Uri))
        {
            args.Cancel = true;
            MessageBox.Show(owner, "已阻止非本机下载。", "零售通",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        var export = RuntimeLocations.Create().Export;
        Directory.CreateDirectory(export);
        var name = SafeDownloadName(Path.GetFileName(args.ResultFilePath));
        using var dialog = new SaveFileDialog
        {
            Title = "下载文件",
            InitialDirectory = export,
            FileName = name,
            AddExtension = true,
            OverwritePrompt = true,
            CheckPathExists = true,
            Filter = "Excel、CSV 或 ZIP 文件|*.xlsx;*.csv;*.zip|所有文件|*.*",
        };
        if (dialog.ShowDialog(owner) != DialogResult.OK)
        {
            args.Cancel = true;
            return;
        }

        var destination = Path.GetFullPath(dialog.FileName);
        if (File.Exists(destination))
        {
            args.Cancel = true;
            MessageBox.Show(this, "目标文件已存在。原文件保持不变，请重新下载并选择新文件名。",
                "下载未完成", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        var staging = Path.Combine(Path.GetDirectoryName(destination)!,
            $".retail-link-download-{Guid.NewGuid():N}.part");
        args.ResultFilePath = staging;
        var operation = args.DownloadOperation;
        operation.StateChanged += (_, _) =>
        {
            if (operation.State == CoreWebView2DownloadState.Completed)
            {
                try
                {
                    File.Move(staging, destination, overwrite: false);
                }
                catch (Exception error)
                {
                    ShowDownloadError(owner, $"无法保存下载文件：{error.Message}\n已有文件保持不变。");
                }
                finally
                {
                    TryDelete(staging);
                }
            }
            else if (operation.State == CoreWebView2DownloadState.Interrupted)
            {
                TryDelete(staging);
                ShowDownloadError(owner, $"下载中断：{operation.InterruptReason}");
            }
        };
    }

    private async Task<FeatureWindow> CreateFeatureWindowAsync(string windowName)
    {
        if (_webViewEnvironment is null)
        {
            throw new InvalidOperationException("WebView2 运行环境尚未初始化。");
        }

        var form = new Form
        {
            Text = "零售通 · 独立工作窗口",
            StartPosition = FormStartPosition.CenterParent,
            MinimumSize = new Size(900, 640),
            ClientSize = new Size(1180, 820),
            BackColor = Color.FromArgb(247, 248, 250),
            Font = new Font("Microsoft YaHei UI", 9F),
        };
        form.Icon = CloneIcon(Icon);
        var webView = new WebView2
        {
            Dock = DockStyle.Fill,
        };
        form.Controls.Add(webView);
        var featureWindow = new FeatureWindow(windowName, form, webView);
        _featureWindows[form] = featureWindow;
        form.FormClosing += (_, e) => OnFeatureWindowClosing(featureWindow, e);
        form.FormClosed += (_, _) => _featureWindows.Remove(form);
        form.Show(this);

        try
        {
            await webView.EnsureCoreWebView2Async(_webViewEnvironment);
            var core = webView.CoreWebView2
                ?? throw new InvalidOperationException("独立工作窗口的 WebView2 未能初始化。");
            ConfigureWebView(core);
            core.WindowCloseRequested += (_, _) => _ = CloseFeatureWindowAsync(featureWindow);
            featureWindow.Bridge = new NativeBridge(webView, form, () => _backend.BackendUri?.Port);
            await InstallDesktopScriptAsync(core);
            return featureWindow;
        }
        catch
        {
            _featureWindows.Remove(form);
            featureWindow.AllowClose = true;
            form.Close();
            throw;
        }
    }

    private void OnFeatureWindowClosing(FeatureWindow featureWindow, FormClosingEventArgs args)
    {
        if (featureWindow.AllowClose)
        {
            return;
        }
        args.Cancel = true;
        if (!featureWindow.Closing)
        {
            _ = CloseFeatureWindowAsync(featureWindow);
        }
    }

    private async Task CloseFeatureWindowAsync(FeatureWindow featureWindow, string? message = null)
    {
        if (featureWindow.AllowClose || featureWindow.Closing)
        {
            return;
        }
        featureWindow.Closing = true;
        try
        {
            var route = await ReadFeatureRouteAsync(featureWindow);
            if (!_closing && _webView.CoreWebView2 is not null)
            {
                var restore = $"window.RetailLinkWorkspace?.restore({JsonSerializer.Serialize(route)},true);";
                await _webView.CoreWebView2.ExecuteScriptAsync(restore);
            }
            featureWindow.AllowClose = true;
            _featureWindows.Remove(featureWindow.Form);
            featureWindow.Form.Close();
            if (message is not null && !_closing)
            {
                MessageBox.Show(this, message, "零售通",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
        catch
        {
            featureWindow.AllowClose = true;
            _featureWindows.Remove(featureWindow.Form);
            if (!featureWindow.Form.IsDisposed)
            {
                featureWindow.Form.Close();
            }
        }
    }

    private async Task<string> ReadFeatureRouteAsync(FeatureWindow featureWindow)
    {
        var fallback = FeatureRoute(featureWindow.WindowName);
        var core = featureWindow.WebView.CoreWebView2;
        if (core is null)
        {
            return fallback;
        }
        try
        {
            var raw = await core.ExecuteScriptAsync("window.name");
            var name = JsonSerializer.Deserialize<string>(raw);
            return string.IsNullOrWhiteSpace(name) ? fallback : FeatureRoute(name);
        }
        catch
        {
            return fallback;
        }
    }

    private static string FeatureRoute(string windowName)
    {
        const string prefix = "retail-link-";
        var route = windowName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
            ? windowName[prefix.Length..]
            : windowName;
        return route.Length is > 0 and <= 96 ? route : "dashboard";
    }

    private FeatureWindow? FindFeatureWindow(object? sender)
    {
        if (sender is not CoreWebView2 core)
        {
            return null;
        }
        return _featureWindows.Values.FirstOrDefault(
            feature => ReferenceEquals(feature.WebView.CoreWebView2, core));
    }

    private bool IsLocal(string? value)
    {
        var backend = _backend.BackendUri;
        return backend is not null
            && Uri.TryCreate(value, UriKind.Absolute, out var uri)
            && uri.Scheme == Uri.UriSchemeHttp
            && uri.Host == "127.0.0.1"
            && uri.Port == backend.Port;
    }

    private bool IsLocalDownload(string? value)
    {
        if (IsLocal(value))
        {
            return true;
        }
        if (Uri.TryCreate(value, UriKind.Absolute, out var uri) && uri.Scheme == "blob")
        {
            return IsLocal(uri.AbsoluteUri.Length > 5 ? uri.AbsoluteUri[5..] : null);
        }
        return false;
    }

    private static void OpenExternal(string? value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri)
            || uri.Scheme is not ("http" or "https" or "mailto"))
        {
            return;
        }
        try
        {
            Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
        }
        catch
        {
            // External links are optional and never affect the local workspace.
        }
    }

    private void ShowLoading(string status, string detail, bool failed = false)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => ShowLoading(status, detail, failed));
            return;
        }
        _webView.Visible = false;
        _loading.Visible = true;
        _loading.BringToFront();
        _status.Text = status;
        _detail.Text = detail;
        _retry.Visible = failed;
        _quit.Visible = failed;
        if (failed)
        {
            _stages.Text = "启动未完成 · 请查看诊断日志";
            _stages.ForeColor = Color.FromArgb(188, 54, 65);
        }
    }

    private void ShowStage(string stage, string detail, string reached)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => ShowStage(stage, detail, reached));
            return;
        }
        _webView.Visible = false;
        _loading.Visible = true;
        _loading.BringToFront();
        _status.Text = stage;
        _detail.Text = detail;
        _retry.Visible = false;
        _quit.Visible = false;
        var environment = reached is "environment" or "backend" or "ready" ? "●" : "○";
        var backend = reached is "backend" or "ready" ? "●" : "○";
        var page = reached == "ready" ? "●" : "○";
        var ready = reached == "ready" ? "●" : "○";
        _stages.Text = $"{environment} 运行环境    {backend} 后端就绪    {page} 页面加载    {ready} 可操作";
        _stages.ForeColor = Color.FromArgb(95, 105, 116);
        RecordStartupStage(stage, detail);
    }

    private string StartupLogPath()
    {
        var data = RuntimeLocations.Create().Data;
        return Path.Combine(data, "日志", "启动诊断.log");
    }

    private void RecordStartupStage(string stage, string detail)
    {
        try
        {
            var path = StartupLogPath();
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var item = new
            {
                time = DateTimeOffset.Now.ToString("O"),
                run = _startupRunId,
                stage,
                elapsedSeconds = Math.Round(_startupWatch.Elapsed.TotalSeconds, 3),
                detail,
            };
            File.AppendAllText(
                path,
                JsonSerializer.Serialize(item) + Environment.NewLine,
                new System.Text.UTF8Encoding(false));
        }
        catch
        {
            // Startup diagnostics must never block the workspace.
        }
    }

    private void OpenStartupDiagnostics()
    {
        try
        {
            var path = StartupLogPath();
            if (!File.Exists(path))
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                File.WriteAllText(path,
                    $"[{DateTimeOffset.Now:O}] 还没有启动阶段记录。请重新启动零售通后查看。{Environment.NewLine}",
                    new System.Text.UTF8Encoding(false));
            }
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
        catch (Exception error)
        {
            MessageBox.Show(this,
                $"启动诊断日志位于：{StartupLogPath()}\n\n无法自动打开：{error.Message}",
                "启动诊断", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }

    private void OnBackendExited(int code, string output)
    {
        if (_closing)
        {
            return;
        }
        var detail = string.IsNullOrWhiteSpace(output)
            ? $"本地服务已停止（状态 {code}），可以重新启动。"
            : output[Math.Max(0, output.Length - 1800)..];
        ShowLoading("本地服务已停止", detail, failed: true);
    }

    private void ShowDownloadError(Form owner, string detail)
    {
        if (IsDisposed || _closing)
        {
            return;
        }
        BeginInvoke(() => MessageBox.Show(owner.IsDisposed ? this : owner, detail, "下载未完成",
            MessageBoxButtons.OK, MessageBoxIcon.Warning));
    }

    private static Icon LoadApplicationIcon()
    {
        try
        {
            return Icon.ExtractAssociatedIcon(Application.ExecutablePath)
                ?? (Icon)SystemIcons.Application.Clone();
        }
        catch
        {
            return (Icon)SystemIcons.Application.Clone();
        }
    }

    private static Icon? CloneIcon(Icon? icon)
    {
        return icon is null ? null : (Icon)icon.Clone();
    }

    private Form CreateLifecycleDialog(string title, Size size)
    {
        return new Form
        {
            Text = title,
            Icon = CloneIcon(Icon),
            StartPosition = FormStartPosition.CenterParent,
            ClientSize = size,
            MinimumSize = size,
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false,
            MinimizeBox = false,
            ShowInTaskbar = false,
            BackColor = Color.FromArgb(247, 248, 250),
            Font = new Font("Microsoft YaHei UI", 9F),
        };
    }

    private void ShowAbout()
    {
        using var dialog = CreateLifecycleDialog("关于零售通", new Size(520, 360));
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 4,
            Padding = new Padding(28),
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var title = new Label
        {
            Text = "零售通",
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 22F, FontStyle.Bold),
            ForeColor = Color.FromArgb(11, 79, 74),
        };
        var version = new Label
        {
            Text = $"Windows 试用版 · v{_version}",
            AutoSize = true,
            ForeColor = Color.FromArgb(80, 92, 102),
            Margin = new Padding(0, 4, 0, 12),
        };
        var body = new Label
        {
            Text = "本地经营工作空间\n\n销售、库存、计划、售后与货损数据在本机处理。程序资源与业务数据分开保存，导出文件由你选择位置并在保存前确认。",
            AutoSize = false,
            Dock = DockStyle.Fill,
            ForeColor = Color.FromArgb(70, 82, 92),
        };
        var close = new Button { Text = "关闭", AutoSize = true, Anchor = AnchorStyles.Right };
        close.Click += (_, _) => dialog.Close();
        layout.Controls.Add(title, 0, 0);
        layout.Controls.Add(version, 0, 1);
        layout.Controls.Add(body, 0, 2);
        layout.Controls.Add(close, 0, 3);
        dialog.AcceptButton = close;
        dialog.Controls.Add(layout);
        dialog.ShowDialog(this);
    }

    private void ShowOnboardingIfNeeded()
    {
        try
        {
            if (!File.Exists(OnboardingMarkerPath()))
            {
                BeginInvoke(ShowOnboarding);
            }
        }
        catch
        {
            // A missing onboarding marker must never prevent the workspace from opening.
        }
    }

    private string OnboardingMarkerPath()
    {
        return Path.Combine(RuntimeLocations.Create().Data, "desktop_onboarding.json");
    }

    private void MarkOnboardingSeen()
    {
        try
        {
            var path = OnboardingMarkerPath();
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(
                path,
                "{\"version\":2}\n",
                new System.Text.UTF8Encoding(false));
        }
        catch
        {
            // The guide can be opened again when the marker cannot be written.
        }
    }

    private void ShowOnboarding()
    {
        var locations = RuntimeLocations.Create();
        Directory.CreateDirectory(locations.Data);
        using var dialog = CreateLifecycleDialog("使用与数据说明", new Size(720, 620));
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 4,
            Padding = new Padding(28),
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var heading = new Label
        {
            Text = "欢迎使用零售通",
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 20F, FontStyle.Bold),
            ForeColor = Color.FromArgb(11, 79, 74),
        };
        var intro = new Label
        {
            Text = "首次使用前先确认数据位置、安装方式和本地处理范围。",
            AutoSize = true,
            ForeColor = Color.FromArgb(80, 92, 102),
            Margin = new Padding(0, 4, 0, 12),
        };
        var body = new RichTextBox
        {
            ReadOnly = true,
            BorderStyle = BorderStyle.FixedSingle,
            BackColor = Color.White,
            Dock = DockStyle.Fill,
            ScrollBars = RichTextBoxScrollBars.Vertical,
            Text =
                "首次使用\n" +
                "1. 选择经营工作簿。\n" +
                "2. 核对商品、仓库、日期和数据问题。\n" +
                "3. 查看分析，预览后导出结果。\n\n" +
                $"当前应用：\n{AppContext.BaseDirectory}\n\n" +
                $"本地数据：\n{locations.Data}\n\n" +
                $"默认导出：\n{locations.Export}\n\n" +
                "原始 Excel 默认只读；开启源写回后，每次仍需预览并确认。历史、导入文件和已生成文件与程序分开保存。\n\n" +
                "本地数据通过本机文件接入；本地服务只监听本机地址，不自动上传工作簿，不定时抓取线上数据。主动打开外部链接时交给系统浏览器。\n\n" +
                "安装与升级：Windows 通过新版安装包执行。选择当前安装目录继续升级，安装器会保留用户数据并在替换前检查目录。",
        };
        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false,
        };
        var start = new Button { Text = "开始使用", AutoSize = true };
        var data = new Button { Text = "打开数据目录", AutoSize = true };
        var install = new Button { Text = "安装与升级…", AutoSize = true };
        start.Click += (_, _) =>
        {
            MarkOnboardingSeen();
            dialog.Close();
        };
        data.Click += (_, _) => ShowDataDirectory();
        install.Click += (_, _) =>
        {
            dialog.Close();
            BeginInvoke(OpenInstallUpgrade);
        };
        buttons.Controls.Add(start);
        buttons.Controls.Add(data);
        buttons.Controls.Add(install);
        layout.Controls.Add(heading, 0, 0);
        layout.Controls.Add(intro, 0, 1);
        layout.Controls.Add(body, 0, 2);
        layout.Controls.Add(buttons, 0, 3);
        dialog.AcceptButton = start;
        dialog.Controls.Add(layout);
        dialog.ShowDialog(this);
    }

    private void OpenInstallUpgrade()
    {
        var installer = FindAdjacentInstaller();
        if (installer is not null)
        {
            try
            {
                Process.Start(new ProcessStartInfo(installer) { UseShellExecute = true });
                return;
            }
            catch
            {
                // Fall through to the actionable instructions when the adjacent package cannot start.
            }
        }

        using var dialog = CreateLifecycleDialog("安装与升级", new Size(620, 400));
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 3,
            Padding = new Padding(28),
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var heading = new Label
        {
            Text = "请使用新版安装包",
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 18F, FontStyle.Bold),
            ForeColor = Color.FromArgb(11, 79, 74),
        };
        var body = new Label
        {
            AutoSize = false,
            Dock = DockStyle.Fill,
            Text =
                "Windows 的安装与升级由独立安装程序执行。\n\n" +
                "请运行新版安装包，在“安装位置”选择当前程序目录。安装器会先检查应用是否退出、目录是否可替换和磁盘空间，再替换程序文件；用户数据、原始资料和导出文件保持在程序目录之外。\n\n" +
                $"当前程序位置：\n{AppContext.BaseDirectory}\n\n" +
                "新版安装包：下载后双击运行，不能通过本窗口直接生成或安装。",
            ForeColor = Color.FromArgb(70, 82, 92),
        };
        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false,
        };
        var close = new Button { Text = "关闭", AutoSize = true };
        var manual = new Button { Text = "打开使用说明", AutoSize = true };
        var folder = new Button { Text = "打开安装目录", AutoSize = true };
        close.Click += (_, _) => dialog.Close();
        manual.Click += (_, _) => OpenManual();
        folder.Click += (_, _) =>
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory)}\"")
            {
                UseShellExecute = true,
            });
        };
        buttons.Controls.Add(close);
        buttons.Controls.Add(manual);
        buttons.Controls.Add(folder);
        layout.Controls.Add(heading, 0, 0);
        layout.Controls.Add(body, 0, 1);
        layout.Controls.Add(buttons, 0, 2);
        dialog.AcceptButton = close;
        dialog.Controls.Add(layout);
        dialog.ShowDialog(this);
    }

    private static string? FindAdjacentInstaller()
    {
        var directories = new List<string> { AppContext.BaseDirectory };
        var parent = Directory.GetParent(Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory));
        if (parent is not null)
        {
            directories.Add(parent.FullName);
        }
        foreach (var directory in directories.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (!Directory.Exists(directory))
            {
                continue;
            }
            var installer = Directory.EnumerateFiles(directory, "*setup.exe", SearchOption.TopDirectoryOnly)
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
                .FirstOrDefault();
            if (installer is not null)
            {
                return installer;
            }
        }
        return null;
    }

    private WebView2? ActiveWebView()
    {
        return _featureWindows.Values.FirstOrDefault(item => item.Form.ContainsFocus)?.WebView ?? _webView;
    }

    private Form ActiveNativeWindow()
    {
        return _featureWindows.Keys.FirstOrDefault(form => form.ContainsFocus) ?? this;
    }

    private void ExecuteEditorCommand(string command)
    {
        var webView = ActiveWebView();
        if (webView?.CoreWebView2 is null)
        {
            return;
        }
        var script = $"document.execCommand({JsonSerializer.Serialize(command)});";
        _ = webView.CoreWebView2.ExecuteScriptAsync(script);
    }

    private void MinimizeActiveWindow()
    {
        ActiveNativeWindow().WindowState = FormWindowState.Minimized;
    }

    private void ToggleActiveWindowSize()
    {
        var window = ActiveNativeWindow();
        window.WindowState = window.WindowState == FormWindowState.Maximized
            ? FormWindowState.Normal
            : FormWindowState.Maximized;
    }

    private void BringAllWindowsToFront()
    {
        foreach (var window in _featureWindows.Keys.Append(this))
        {
            if (window.WindowState == FormWindowState.Minimized)
            {
                window.WindowState = FormWindowState.Normal;
            }
            window.BringToFront();
        }
        Activate();
    }

    private void ShowDataDirectory()
    {
        var data = RuntimeLocations.Create().Data;
        Directory.CreateDirectory(data);
        Process.Start(new ProcessStartInfo("explorer.exe", $"\"{data}\"") { UseShellExecute = true });
    }

    private void OpenManual()
    {
        var manual = Path.Combine(AppContext.BaseDirectory, "app", "使用说明.html");
        if (File.Exists(manual))
        {
            Process.Start(new ProcessStartInfo(manual) { UseShellExecute = true });
        }
    }

    private void OpenUninstallSettings()
    {
        try
        {
            var script = Path.Combine(AppContext.BaseDirectory, "uninstall.ps1");
            if (!File.Exists(script))
            {
                throw new FileNotFoundException("未找到卸载脚本。");
            }
            var start = new ProcessStartInfo("powershell.exe")
            {
                UseShellExecute = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                Arguments = $"-NoProfile -ExecutionPolicy Bypass -File \"{script}\" -InstallDir \"{Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory)}\"",
            };
            Process.Start(start);
        }
        catch
        {
            MessageBox.Show(this, "未找到卸载入口。请在 Windows 设置的“应用”中找到零售通并卸载。",
                "卸载零售通", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }

    private static string ReadVersion()
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "app", "版本.json");
            using var document = JsonDocument.Parse(File.ReadAllText(path));
            var version = document.RootElement.GetProperty("version").GetString();
            if (!string.IsNullOrWhiteSpace(version))
            {
                return version;
            }
        }
        catch
        {
            // A mismatched package is rejected when the backend publishes readiness.
        }
        return Application.ProductVersion;
    }

    private static string SafeDownloadName(string? value)
    {
        var name = string.IsNullOrWhiteSpace(value) ? "零售通下载.xlsx" : Path.GetFileName(value);
        foreach (var character in Path.GetInvalidFileNameChars())
        {
            name = name.Replace(character, '-');
        }
        return string.IsNullOrWhiteSpace(name) ? $"零售通-下载-{Guid.NewGuid():N}.xlsx" : name;
    }

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch
        {
            // A partial download is hidden and can be reclaimed by Windows later.
        }
    }

    protected override async void OnFormClosing(FormClosingEventArgs e)
    {
        if (_allowClose)
        {
            base.OnFormClosing(e);
            return;
        }
        e.Cancel = true;
        if (_closing)
        {
            return;
        }
        _closing = true;
        Hide();
        await CloseAllFeatureWindowsAsync();
        await _backend.DisposeAsync();
        _allowClose = true;
        Close();
    }

    private async Task CloseAllFeatureWindowsAsync()
    {
        foreach (var featureWindow in _featureWindows.Values.ToArray())
        {
            await CloseFeatureWindowAsync(featureWindow);
        }
    }

    private sealed class FeatureWindow
    {
        public FeatureWindow(string windowName, Form form, WebView2 webView)
        {
            WindowName = windowName;
            Form = form;
            WebView = webView;
        }

        public string WindowName { get; }
        public Form Form { get; }
        public WebView2 WebView { get; }
        public NativeBridge? Bridge { get; set; }
        public bool Closing { get; set; }
        public bool AllowClose { get; set; }
    }
}
