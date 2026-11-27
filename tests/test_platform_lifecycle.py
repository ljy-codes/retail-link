"""Isolated Windows lifecycle regressions; never run an installer/uninstaller entry point.

The console builds the real BackendManager and selected, unchanged MainForm methods
with UI/OS boundary doubles. The PowerShell harness executes the real cleanup AST
with a temporary-file move boundary, and substitutes only the registry boundary.
This does not replace native WebView2 integration testing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from temp_support import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "desktop" / "windows"
MAX_FILE_BYTES = 314572800


def checked_read(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Refusing oversized file: {path}")
    return path.read_text(encoding="utf-8-sig")


def method(source: str, name: str, *, optional: bool = False) -> str:
    # These methods have a four-space class indent. Preserve the actual bodies,
    # including async/catch branches; only UI/OS dependencies are substituted.
    matches = re.findall(
        rf"^    private [^\n]*\b{re.escape(name)}\([^\n]*\)\s*\n"
        rf"    \{{.*?^    \}}",
        source, re.MULTILINE | re.DOTALL,
    )
    if optional and not matches:
        return ""
    if len(matches) != 1:
        raise AssertionError(f"Expected one MainForm method: {name}")
    return matches[0]


POWERSHELL_HARNESS = r"""
param([string]$Source, [string]$Fixture, [string]$Case, [string]$Mode)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
if ((Get-Item -LiteralPath $Source).Length -gt 314572800) { throw 'Oversize source' }
$tokens = $null; $errors = $null
$text = [IO.File]::ReadAllText($Source, [Text.Encoding]::UTF8)
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$fixtureFull = [IO.Path]::GetFullPath($Fixture).TrimEnd('\') + '\'
$applicationData = Join-Path $Fixture 'AppData\Local\零售通'
$InstallDir = Join-Path $Fixture 'Local programs\零售通'
$env:APPDATA = Join-Path $Fixture 'AppData\Roaming'
$env:LOCALAPPDATA = Join-Path $Fixture 'AppData\Local'
$script:moved = New-Object 'System.Collections.Generic.List[string]'
function Add-Result { param([string]$Kind, [string]$Message) }
function Write-UninstallRecord { param([string]$Text) }
function Move-ToRecoverableLocation {
    param([string]$Path, [string]$Label, [switch]$Directory)
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($fixtureFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe source: $full"
    }
    $relative = $full.Substring(([IO.Path]::GetFullPath($applicationData).TrimEnd('\') + '\').Length)
    $destination = [IO.Path]::GetFullPath((Join-Path (Join-Path $Fixture 'holding') $relative))
    if (-not $destination.StartsWith($fixtureFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe destination: $destination"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Move-Item -LiteralPath $full -Destination $destination
    $script:moved.Add($relative)
}
if ($Case -eq 'cleanup') {
    # Select only the top-level data cleanup, never process stopping, shortcuts,
    # registry deletion, installation-directory removal, dialogs or exit calls.
    $blocks = @($ast.EndBlock.Statements | Where-Object {
        $_ -is [System.Management.Automation.Language.IfStatementAst] -and
        $_.Extent.Text.StartsWith("if (`$script:selectedMode -eq 'Complete' -and (Test-Path ")
    })
    if ($blocks.Count -ne 1) { throw 'Expected one cleanup block' }
    $script:selectedMode = $Mode
    & ([scriptblock]::Create($blocks[0].Extent.Text))
    ConvertTo-Json -Compress -InputObject @($script:moved)
} elseif ($Case -eq 'install-directory') {
    # The resolver runs unchanged except for the Microsoft registry boundary.
    # No real registry API is accessed, even on the pre-fix failure path.
    Add-Type -TypeDefinition @'
public sealed class LifecycleRegistryBoundary {
    public static string RegisteredPath;
    public static int Reads;
    public static LifecycleRegistryBoundary CurrentUser {
        get { Reads++; return new LifecycleRegistryBoundary(); }
    }
    public LifecycleRegistryBoundary OpenSubKey(string path, bool writable) { return this; }
    public object GetValue(string name, object fallback, Microsoft.Win32.RegistryValueOptions options) {
        return RegisteredPath;
    }
    public void Close() {}
}
'@
    $functions = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-RegisteredInstallDirectory'
    }, $false))
    if ($functions.Count -ne 1) { throw 'Expected one install resolver' }
    $body = $functions[0].Extent.Text.Replace(
        '[Microsoft.Win32.Registry]::CurrentUser', '[LifecycleRegistryBoundary]::CurrentUser')
    . ([scriptblock]::Create($body))
    [LifecycleRegistryBoundary]::RegisteredPath = Join-Path $Fixture 'stale install'
    $registryUninstallPath = 'test-only'
    $explicit = Get-RegisteredInstallDirectory
    $explicitReads = [LifecycleRegistryBoundary]::Reads
    $InstallDir = ''
    $fallback = Get-RegisteredInstallDirectory
    @{ explicit = $explicit; explicitReads = $explicitReads; fallback = $fallback } |
        ConvertTo-Json -Compress
} else { throw 'Unknown harness case' }
"""


CONSOLE_HARNESS = r"""
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
namespace RetailLink.Windows;

internal static class LifecycleProgram
{
    private static string Root => Environment.GetEnvironmentVariable("RETAIL_LINK_TEST_ROOT")!;
    public static async Task<int> Main(string[] args)
    {
        if (args.Contains("-B")) return await Child(args);
        try
        {
            switch (args.Single())
            {
                case "backend-exit": await BackendExit(); break;
                case "runtime-missing": await RuntimeMissing(); break;
                case "renderer-ready": await Renderer(false); break;
                case "renderer-startup": await Renderer(true); break;
                case "renderer-before-backend-ready": await RendererBeforeBackendReady(); break;
                case "gpu-ignored": await GpuIgnored(); break;
                case "uninstall-arguments": UninstallArguments(); break;
                default: throw new Exception("Unknown case");
            }
            Console.WriteLine("PASS " + args[0]);
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static async Task<int> Child(string[] args)
    {
        var ready = args[Array.IndexOf(args, "--ready-file") + 1];
        File.WriteAllText(Path.Combine(Root, "child.pid"), Environment.ProcessId.ToString());
        var stdin = Task.Run(() => Console.In.ReadToEnd());
        while (File.Exists(Path.Combine(Root, "hold-ready")) && !stdin.IsCompleted)
            await Task.Delay(10);
        if (stdin.IsCompleted) return 0;
        File.WriteAllText(ready, JsonSerializer.Serialize(new {
            url = $"http://127.0.0.1:{20000 + Environment.ProcessId % 30000}/",
            pid = Environment.ProcessId, version = "lifecycle-test"
        }));
        while (!stdin.IsCompleted)
        {
            if (File.Exists(Path.Combine(Root, "crash"))) return 23;
            await Task.Delay(10);
        }
        return 0;
    }

    internal static void Check(bool value, string message)
    {
        if (!value) throw new Exception(message);
    }
    internal static async Task Until(Func<bool> predicate)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        while (!predicate()) await Task.Delay(10, deadline.Token);
    }
    private static bool ChildExited()
    {
        var marker = Path.Combine(Root, "child.pid");
        if (!File.Exists(marker)) return true;
        var pid = int.Parse(File.ReadAllText(marker));
        try { using var process = Process.GetProcessById(pid); return process.HasExited; }
        catch (ArgumentException) { return true; }
    }
    private static void Cleaned(BackendManager backend)
    {
        Check(backend.BackendUri is null, "BackendUri must be invalid after failure/stop");
        Check(ChildExited(), "Backend child still running");
        var temporary = RuntimeLocations.Create().Temporary;
        Check(!Directory.Exists(temporary) || !Directory.EnumerateDirectories(temporary).Any(),
            "Per-launch temporary directory was not reclaimed");
    }
    private static async Task BackendExit()
    {
        await using var backend = new BackendManager();
        var observed = new TaskCompletionSource<(int Code, bool Invalid)>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var events = 0;
        backend.Exited += (code, _) => {
            Interlocked.Increment(ref events);
            observed.TrySetResult((code, backend.BackendUri is null));
        };
        await backend.StartAsync("lifecycle-test", CancellationToken.None);
        Check(backend.BackendUri is not null, "No ready address");
        File.WriteAllText(Path.Combine(Root, "crash"), "");
        var result = await observed.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Check(result.Code == 23, "Unexpected child exit status");
        Check(result.Invalid, "Exited callback observed a stale trusted BackendUri");
        await backend.StopAsync();
        Cleaned(backend);
        File.Delete(Path.Combine(Root, "crash"));
        await backend.StartAsync("lifecycle-test", CancellationToken.None);
        await backend.StopAsync();
        Cleaned(backend);
        Check(events == 1, "Intentional stop emitted an abnormal-exit event");
    }
    private static async Task RuntimeMissing()
    {
        await using var form = new MainForm();
        CoreWebView2Environment.Failure = new WebView2RuntimeNotFoundException();
        await form.Start();
        Check(form.FailureShown && form.Retry.Enabled, "Missing-runtime failure/retry path absent");
        Cleaned(form.Backend);
        CoreWebView2Environment.Failure = null;
        await form.Start();
        Check(form.WebView.Visible && !form.FailureShown, "Retry after runtime recovery failed");
    }
    private static async Task Renderer(bool duringStartup)
    {
        await using var form = new MainForm();
        form.AutoReady = !duringStartup;
        var startup = form.Start();
        await Until(() => form.WebView.CoreWebView2?.Navigated == true);
        if (!duringStartup) await startup;
        form.WebView.CoreWebView2!.Raise(CoreWebView2ProcessFailedKind.RenderProcessExited);
        if (duringStartup) await startup.WaitAsync(TimeSpan.FromSeconds(10));
        await Until(() => form.FailureShown && form.Retry.Enabled);
        Cleaned(form.Backend);
        Check(!form.WebView.Visible, "Crashed page still visible");
        form.AutoReady = true;
        await form.Start();
        Check(form.WebView.Visible && !form.FailureShown && form.Backend.BackendUri is not null,
            "Retry after renderer failure failed");
    }
    private static async Task RendererBeforeBackendReady()
    {
        var hold = Path.Combine(Root, "hold-ready");
        File.WriteAllText(hold, "");
        await using var form = new MainForm();
        var startup = form.Start();
        await Until(() => form.WebView.CoreWebView2 is not null &&
            File.Exists(Path.Combine(Root, "child.pid")));
        Check(form.Backend.BackendUri is null, "Backend readiness was not held");
        form.WebView.CoreWebView2!.Raise(CoreWebView2ProcessFailedKind.RenderProcessExited);
        await startup.WaitAsync(TimeSpan.FromSeconds(10));
        Check(form.FailureShown && form.Retry.Enabled, "Early renderer failure was not reported");
        Check(form.LastFailure.Contains("渲染进程"), "Renderer failure was reported as a timeout");
        Cleaned(form.Backend);
        File.Delete(hold);
        await form.Start();
        Check(form.WebView.Visible && !form.FailureShown, "Retry after early renderer failure failed");
    }
    private static async Task GpuIgnored()
    {
        await using var form = new MainForm();
        await form.Start();
        var uri = form.Backend.BackendUri;
        form.WebView.CoreWebView2!.Raise(CoreWebView2ProcessFailedKind.GpuProcessExited);
        Check(form.WebView.Visible && form.Backend.BackendUri == uri && !form.FailureShown,
            "Automatically recovered GPU failure incorrectly stopped workspace");
    }
    [DllImport("shell32.dll", SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(
        [MarshalAs(UnmanagedType.LPWStr)] string commandLine, out int count);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr pointer);
    private static void UninstallArguments()
    {
        using var sentinel = File.Create(Path.Combine(AppContext.BaseDirectory, "uninstall.ps1"));
        var form = new MainForm();
        form.Uninstall();
        var start = CapturedProcess.StartInfo ?? throw new Exception("Uninstall command missing");
        Check(start.FileName == "powershell.exe" && start.UseShellExecute &&
            start.WindowStyle == ProcessWindowStyle.Hidden, "Uninstall launch behavior changed");
        var pointer = CommandLineToArgvW("powershell.exe " + start.Arguments, out var count);
        Check(pointer != IntPtr.Zero, "Could not parse uninstall command");
        try
        {
            var arguments = Enumerable.Range(0, count)
                .Select(i => Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, i * IntPtr.Size))!)
                .ToArray();
            var index = Array.IndexOf(arguments, "-InstallDir");
            Check(index >= 0 && index + 1 < arguments.Length,
                "App uninstall did not explicitly pass -InstallDir");
            Check(arguments[index + 1] == Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory),
                "InstallDir did not survive Windows command-line quoting");
            Check(!arguments.Contains("-Yes"), "App bypassed uninstall confirmation");
        }
        finally { LocalFree(pointer); }
    }
}

internal sealed class Widget
{
    public bool Enabled { get; set; } = true;
    public bool Visible { get; set; }
}
internal partial class MainForm : IAsyncDisposable
{
    private readonly BackendManager _backend = new();
    private readonly FakeWebView _webView;
    private readonly Widget _retry = new(), _quit = new(), _loading = new();
    private readonly Stopwatch _startupWatch = new();
    private readonly string _version = "lifecycle-test";
    private NativeBridge? _bridge;
    private TaskCompletionSource<bool>? _navigationCompleted, _frontendReady;
    private string _startupRunId = "";
    private bool _startupRunning, _closing;
    // Field declarations are taken from production when present.
    // The production form shares these helpers with detachable windows;
    // keep this harness focused on the main-window lifecycle path.
    private CoreWebView2Environment? _webViewEnvironment;
    FIELD_DECLARATIONS
    public bool AutoReady = true;
    public bool FailureShown;
    public string LastFailure = "";
    public MainForm() {
        _webView = new FakeWebView(() => {
            if (AutoReady) {
                _navigationCompleted?.TrySetResult(true);
                _bridge?.Ready();
            }
        });
    }
    public FakeWebView WebView => _webView;
    public Widget Retry => _retry;
    public BackendManager Backend => _backend;
    public Task Start() { FailureShown = false; return StartWorkspaceAsync(); }
    public void Uninstall() => OpenUninstallSettings();
    public async ValueTask DisposeAsync() { _closing = true; await _backend.DisposeAsync(); }
    private void RecordStartupStage(string stage, string detail) {}
    private void ShowStage(string stage, string detail, string reached) {
        _webView.Visible = false; _loading.Visible = true;
    }
    private void ShowLoading(string status, string detail, bool failed = false) {
        FailureShown = failed; _webView.Visible = false; _loading.Visible = true;
        LastFailure = detail;
        _retry.Visible = failed; _quit.Visible = failed;
    }
    private void OnNewWindowRequested(object? sender, EventArgs args) {}
    private void OnNavigationStarting(object? sender, EventArgs args) {}
    private void OnNavigationCompleted(object? sender, EventArgs args) {}
    private void OnDownloadStarting(object? sender, EventArgs args) {}
    private void ConfigureWebView(CoreWebView2 core) =>
        core.ProcessFailed += OnWebViewProcessFailed;
    private Task<string> InstallDesktopScriptAsync(CoreWebView2 core) =>
        Task.FromResult("");
    private FeatureWindow? FindFeatureWindow(object? sender) => null;
    private Task CloseFeatureWindowAsync(FeatureWindow featureWindow, string? message = null) =>
        Task.CompletedTask;
    private sealed class FeatureWindow {}
}
internal sealed class FakeWebView(Action navigated)
{
    public bool Visible { get; set; }
    public CoreWebView2? CoreWebView2 { get; private set; }
    public Task EnsureCoreWebView2Async(CoreWebView2Environment environment) {
        CoreWebView2 = new CoreWebView2(navigated); return Task.CompletedTask;
    }
}
internal sealed class CoreWebView2(Action navigated)
{
    public dynamic Settings { get; } = new System.Dynamic.ExpandoObject();
    public bool Navigated;
    public event EventHandler? NewWindowRequested, NavigationStarting, NavigationCompleted, DownloadStarting;
    public event EventHandler<CoreWebView2ProcessFailedEventArgs>? ProcessFailed;
    public Task AddScriptToExecuteOnDocumentCreatedAsync(string script) => Task.CompletedTask;
    public void Navigate(string uri) { Navigated = true; navigated(); }
    public void Raise(CoreWebView2ProcessFailedKind kind) =>
        ProcessFailed?.Invoke(this, new CoreWebView2ProcessFailedEventArgs(kind));
}
internal sealed class CoreWebView2Environment
{
    public static Exception? Failure;
    public static Task<CoreWebView2Environment> CreateAsync(string? executable, string data) =>
        Failure is null ? Task.FromResult(new CoreWebView2Environment()) :
        Task.FromException<CoreWebView2Environment>(Failure);
}
internal sealed class WebView2RuntimeNotFoundException : Exception {}
internal enum CoreWebView2ProcessFailedKind { RenderProcessExited, GpuProcessExited }
internal sealed class CoreWebView2ProcessFailedEventArgs(CoreWebView2ProcessFailedKind kind) : EventArgs
{
    public CoreWebView2ProcessFailedKind ProcessFailedKind => kind;
}
internal sealed class NativeBridge(FakeWebView view, MainForm form, Func<int?> port)
{
    public event Action? FrontendReady;
    public void Ready() => FrontendReady?.Invoke();
}
internal static class CapturedProcess
{
    public static ProcessStartInfo? StartInfo;
    public static void Start(ProcessStartInfo start) { StartInfo = start; }
}
internal enum MessageBoxButtons { OK }
internal enum MessageBoxIcon { Information }
internal static class MessageBox
{
    public static void Show(object owner, string text, string title,
        MessageBoxButtons buttons, MessageBoxIcon icon) => throw new Exception(text);
}
"""


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
class UninstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="零售通 lifecycle ")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name).resolve()
        self.harness = self.fixture / "isolated-cleanup.ps1"
        self.harness.write_text(POWERSHELL_HARNESS, encoding="utf-8-sig")
        self.data = self.fixture / "AppData/Local/零售通"

    def run_harness(self, case, mode="Complete"):
        environment = dict(os.environ, APPDATA=str(self.fixture / "AppData/Roaming"),
                           LOCALAPPDATA=str(self.fixture / "AppData/Local"))
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.harness),
             "-Source", str(WINDOWS / "uninstall/uninstall.ps1"),
             "-Fixture", str(self.fixture), "-Case", case, "-Mode", mode],
            capture_output=True, encoding="utf-8", errors="replace", env=environment, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def seed(self):
        removed = [
            "daily.sqlite3", "daily.sqlite3-wal", "daily.sqlite3-shm", "import_templates.json",
            "preferences.json", "operation.sqlite3", "operation.sqlite3-wal", "operation.sqlite3-shm",
            "history/example.json", "history_deleted/example.json", "import_sessions/preview.json",
            "WebView2/cache", "operations/state.json", "writeback/settings.json", "writeback/templates.json",
        ]
        retained = [
            "原始资料/original.xlsx", "导入文件/source.xlsx", "生成文件/export.xlsx",
            "operations/user.xlsx", "writeback/backups/original.xlsx",
            "writeback/operations/recovery.json", "unknown-user-file.txt",
        ]
        for relative in removed + retained:
            target = self.data / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(relative.encode("utf-8"))
        document = self.fixture / "Documents/零售通/生成文件/output.xlsx"
        document.parent.mkdir(parents=True)
        document.write_bytes(b"preserved document output")
        return removed, retained, document

    def test_complete_removes_actual_database_and_templates_but_preserves_user_files(self):
        removed, retained, document = self.seed()
        self.run_harness("cleanup")
        for relative in removed:
            with self.subTest(removed=relative):
                self.assertFalse((self.data / relative).exists(), relative)
                self.assertEqual((self.fixture / "holding" / relative).read_bytes(),
                                 relative.encode("utf-8"))
        for relative in retained:
            self.assertEqual((self.data / relative).read_bytes(), relative.encode("utf-8"))
        self.assertEqual(document.read_bytes(), b"preserved document output")

    def test_normal_uninstall_keeps_all_application_data(self):
        removed, retained, document = self.seed()
        self.assertEqual(self.run_harness("cleanup", "Normal"), [])
        for relative in removed + retained:
            self.assertEqual((self.data / relative).read_bytes(), relative.encode("utf-8"))
        self.assertEqual(document.read_bytes(), b"preserved document output")

    def test_explicit_current_install_wins_over_stale_registration(self):
        result = self.run_harness("install-directory")
        self.assertEqual(Path(result["explicit"]), self.fixture / "Local programs/零售通")
        self.assertEqual(result["explicitReads"], 0)
        self.assertEqual(Path(result["fallback"]), self.fixture / "stale install")


@unittest.skipUnless(os.name == "nt" and shutil.which("dotnet"), "Windows .NET SDK required")
class WindowsBackendLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory(prefix="零售通 lifecycle console ")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.build_root = Path(cls.temporary.name)
        cls.output = cls.build_root / "out"
        backend = checked_read(WINDOWS / "app/BackendManager.cs")
        form = checked_read(WINDOWS / "app/MainForm.cs")
        # Only a package-free console is built, never the full application.
        (cls.build_root / "BackendManager.cs").write_text(backend, encoding="utf-8")
        fields = "\n".join(re.findall(
            r"^    private (?:CancellationTokenSource\?|Exception\?) "
            r"(?:_startupCancellation|_webViewFailure);$", form, re.MULTILINE,
        ))
        (cls.build_root / "Program.cs").write_text(
            CONSOLE_HARNESS.replace("FIELD_DECLARATIONS", fields), encoding="utf-8")
        methods = "\n".join([
            method(form, "StartWorkspaceAsync"), method(form, "InitializeWebViewAsync"),
            method(form, "OnWebViewProcessFailed", optional=True), method(form, "OpenUninstallSettings"),
        ])
        (cls.build_root / "FormMethods.cs").write_text(
            "using System.Diagnostics;\nusing System.Text.Json;\n"
            "using Process = RetailLink.Windows.CapturedProcess;\n"
            "namespace RetailLink.Windows;\ninternal partial class MainForm {\n" + methods + "\n}",
            encoding="utf-8")
        (cls.build_root / "Lifecycle.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            '<OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework>'
            '<ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable>'
            '</PropertyGroup></Project>', encoding="utf-8")
        config = cls.build_root / "NuGet.Config"
        config.write_text("<configuration><packageSources><clear /></packageSources></configuration>",
                          encoding="utf-8")
        environment = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1",
                           DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1")
        build = subprocess.run(
            ["dotnet", "build", str(cls.build_root / "Lifecycle.csproj"),
             "--configfile", str(config), "-o", str(cls.output), "--nologo"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60, env=environment,
        )
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        runtime = cls.output / "runtime"
        runtime.mkdir()
        for source in cls.output.iterdir():
            if source.is_file():
                if source.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError(f"Oversized harness artifact: {source}")
                shutil.copy2(source, runtime / ("python.exe" if source.name == "Lifecycle.exe" else source.name))
        app = cls.output / "app"
        app.mkdir()
        (app / "server.py").write_text("# Child protocol double; Python is never invoked.\n",
                                       encoding="utf-8")

    def run_case(self, case):
        with TemporaryDirectory(prefix="零售通 lifecycle case ") as isolated:
            fixture = Path(isolated)
            environment = dict(
                os.environ, RETAIL_LINK_TEST_ROOT=isolated,
                APPDATA=str(fixture / "AppData/Roaming"), LOCALAPPDATA=str(fixture / "AppData/Local"),
            )
            result = subprocess.run(
                [str(self.output / "Lifecycle.exe"), case],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=35, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS " + case, result.stdout)

    def test_backend_exit_invalidates_uri_before_notifying_and_can_restart(self):
        self.run_case("backend-exit")

    def test_missing_runtime_reclaims_backend_and_allows_retry(self):
        self.run_case("runtime-missing")

    def test_renderer_exit_after_ready_reclaims_backend_and_allows_retry(self):
        self.run_case("renderer-ready")

    def test_renderer_exit_during_startup_reclaims_backend_and_allows_retry(self):
        self.run_case("renderer-startup")

    def test_renderer_exit_before_backend_ready_cancels_startup_and_allows_retry(self):
        self.run_case("renderer-before-backend-ready")

    def test_gpu_exit_keeps_workspace_running(self):
        self.run_case("gpu-ignored")

    def test_app_uninstall_passes_quoted_current_install_directory(self):
        self.run_case("uninstall-arguments")


if __name__ == "__main__":
    unittest.main()
