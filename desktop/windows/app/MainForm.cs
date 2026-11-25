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
    private readonly string _version;
    private readonly Stopwatch _startupWatch = new();
    private NativeBridge? _bridge;
    private TaskCompletionSource<bool>? _navigationCompleted;
    private TaskCompletionSource<bool>? _frontendReady;
    private string _startupRunId = "";
    private bool _startupRunning;
    private bool _closing;
    private bool _allowClose;

    public MainForm()
    {
        Text = "零售通";
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
        application.DropDownItems.Add("显示本地数据位置", null, (_, _) => ShowDataDirectory());
        application.DropDownItems.Add("打开使用说明", null, (_, _) => OpenManual());
        application.DropDownItems.Add("显示启动诊断", null, (_, _) => OpenStartupDiagnostics());
        application.DropDownItems.Add(new ToolStripSeparator());
        application.DropDownItems.Add("卸载零售通…", null, (_, _) => OpenUninstallSettings());
        application.DropDownItems.Add(new ToolStripSeparator());
        application.DropDownItems.Add("退出", null, (_, _) => Close());
        menu.Items.Add(application);
        MainMenuStrip = menu;
        Controls.Add(menu);
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
        }
        catch (WebView2RuntimeNotFoundException)
        {
            RecordStartupStage("失败", "缺少 Microsoft Edge WebView2 Runtime");
            ShowLoading(
                "缺少 Microsoft Edge WebView2 运行环境",
                "请安装 Microsoft Edge WebView2 Runtime 后，点击“重新启动”。Windows 10/11 通常已随 Edge 提供。也可点击菜单“应用 → 显示启动诊断”查看日志。",
                failed: true);
        }
        catch (OperationCanceledException)
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
            RecordStartupStage("失败", error.ToString());
            ShowLoading("本地工作空间尚未就绪", error.Message, failed: true);
        }
        finally
        {
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
        await _webView.EnsureCoreWebView2Async(environment);
        var core = _webView.CoreWebView2
            ?? throw new InvalidOperationException("WebView2 未能初始化。");
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
        _bridge = new NativeBridge(_webView, this, () => _backend.BackendUri?.Port);
        _bridge.FrontendReady += () =>
        {
            RecordStartupStage("可操作", "收到 frontendReady");
            _frontendReady?.TrySetResult(true);
        };
        await core.AddScriptToExecuteOnDocumentCreatedAsync(
            $$"""
            window.webkit = window.webkit || {};
            window.webkit.messageHandlers = window.webkit.messageHandlers || {};
            window.webkit.messageHandlers.retailLinkDesktop = {
              postMessage: function (message) { window.chrome.webview.postMessage(message); }
            };
            window.__RETAIL_LINK_DESKTOP__ = Object.freeze({platform:'windows',version:{{JsonSerializer.Serialize(_version)}}});
            """);
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

    private void OnNewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs args)
    {
        args.Handled = true;
        if (IsLocal(args.Uri))
        {
            _webView.CoreWebView2.Navigate(args.Uri);
        }
        else
        {
            OpenExternal(args.Uri);
        }
    }

    private void OnDownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs args)
    {
        if (!IsLocalDownload(args.DownloadOperation.Uri))
        {
            args.Cancel = true;
            MessageBox.Show(this, "已阻止非本机下载。", "零售通", MessageBoxButtons.OK, MessageBoxIcon.Warning);
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
        if (dialog.ShowDialog(this) != DialogResult.OK)
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
                    ShowDownloadError($"无法保存下载文件：{error.Message}\n已有文件保持不变。");
                }
                finally
                {
                    TryDelete(staging);
                }
            }
            else if (operation.State == CoreWebView2DownloadState.Interrupted)
            {
                TryDelete(staging);
                ShowDownloadError($"下载中断：{operation.InterruptReason}");
            }
        };
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

    private void ShowDownloadError(string detail)
    {
        if (IsDisposed || _closing)
        {
            return;
        }
        BeginInvoke(() => MessageBox.Show(this, detail, "下载未完成",
            MessageBoxButtons.OK, MessageBoxIcon.Warning));
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
                Arguments = $"-NoProfile -ExecutionPolicy Bypass -File \"{script}\"",
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
        await _backend.DisposeAsync();
        _allowClose = true;
        Close();
    }
}
