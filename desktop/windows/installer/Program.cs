using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;

namespace RetailLink.Setup;

internal static class Program
{
    private const string DisplayName = "零售通";
    private const string Publisher = "零售通";
    private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\RetailLink";
    private const string ApplicationExecutable = "零售通.exe";
    private const long FreeSpaceReserve = 64L * 1024 * 1024;

    [STAThread]
    private static int Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        try
        {
            var options = Options.Parse(args);
            if (options.ShowHelp)
            {
                MessageBox.Show(
                    "零售通 Windows 安装程序\n\n" +
                    "双击打开时可选安装位置、启动和快捷方式。\n\n" +
                    "零售通.exe /silent [/install-dir <目录>] [/start] [/desktop-shortcut]\n" +
                    "零售通.exe /verify-only\n" +
                    "零售通.exe /no-registry /no-shortcuts",
                    "零售通安装程序", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }
            if (!options.Silent)
            {
                Application.Run(new InstallForm(options));
                return Environment.ExitCode;
            }

            var result = Install(options);
            if (options.LaunchAfterInstall)
            {
                LaunchApplication(result.InstallDirectory);
            }
            return 0;
        }
        catch (Exception error)
        {
            WriteInstallerLog(error);
            if (args.Contains("/silent", StringComparer.OrdinalIgnoreCase)
                || args.Contains("--silent", StringComparer.OrdinalIgnoreCase))
            {
                try
                {
                    Console.Error.WriteLine(error);
                }
                catch
                {
                    // A windowed process may not own a console.
                }
            }
            else
            {
                MessageBox.Show(error.Message, "零售通安装未完成",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            return 1;
        }
    }

    internal static InstallResult Install(Options options)
    {
        using var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
            ?? throw new InvalidOperationException("安装包载荷缺失，请重新下载完整安装程序。");

        var payloadBytes = EstimatePayloadBytes(payload);
        var installDirectory = ValidateInstallDirectory(options.InstallDirectory, payloadBytes);
        var registered = ExistingInstallDirectory();
        if (!string.IsNullOrWhiteSpace(registered)
            && Directory.Exists(registered)
            && !SamePath(registered, installDirectory))
        {
            throw new InvalidOperationException(
                $"零售通已安装在：{registered}\n\n为避免产生第二个无提示安装，请选择该目录继续升级，或先从原目录卸载后再迁移。");
        }

        EnsureApplicationStopped();
        InstallDirectoryGuard.VerifyExistingDirectoryCanBeReplaced(installDirectory);
        payload.Position = 0;
        var parent = Path.GetDirectoryName(installDirectory)
            ?? throw new InvalidOperationException("安装目录无效。");
        var staging = Path.Combine(parent, $".零售通-安装-{Guid.NewGuid():N}");
        var backup = Path.Combine(parent, $".零售通-旧版-{Guid.NewGuid():N}");
        var hadPrevious = Directory.Exists(installDirectory);
        try
        {
            Directory.CreateDirectory(staging);
            ExtractPayload(payload, staging);
            VerifyPayload(staging);
            var metadata = ReadMetadata(staging);
            if (options.VerifyOnly)
            {
                Directory.Delete(staging, recursive: true);
                return new InstallResult(installDirectory, metadata.GetValueOrDefault("version", ""));
            }

            if (hadPrevious)
            {
                try
                {
                    Directory.Move(installDirectory, backup);
                }
                catch (Exception error)
                {
                    throw new InvalidOperationException(
                        $"安装位置不可替换：{installDirectory}\n\n" +
                        "失败阶段：安装前目录占用/权限检查。\n" +
                        $"原因：{error.Message}\n\n" +
                        "请关闭占用该目录的程序，或选择其他目录。",
                        error);
                }
            }
            try
            {
                Directory.Move(staging, installDirectory);
            }
            catch (Exception error)
            {
                if (Directory.Exists(backup) && !Directory.Exists(installDirectory))
                {
                    Directory.Move(backup, installDirectory);
                }
                throw new InvalidOperationException(
                    $"新版文件无法落位：{installDirectory}\n\n" +
                    $"原因：{error.Message}\n\n" +
                    "旧版已尝试恢复；请关闭占用该目录的程序后重试，或选择其他目录。",
                    error);
            }

            ApplyShortcutChoices(installDirectory, options);
            if (!options.NoRegistry)
            {
                RegisterUninstall(installDirectory, metadata);
            }
            if (Directory.Exists(backup))
            {
                Directory.Delete(backup, recursive: true);
            }
            return new InstallResult(installDirectory, metadata.GetValueOrDefault("version", ""));
        }
        catch
        {
            if (Directory.Exists(staging))
            {
                Directory.Delete(staging, recursive: true);
            }
            if (Directory.Exists(backup) && !Directory.Exists(installDirectory))
            {
                Directory.Move(backup, installDirectory);
            }
            throw;
        }
    }

    internal static string ValidateInstallDirectory(string value, long requiredBytes)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException("请选择安装位置。");
        }
        var full = Path.GetFullPath(Environment.ExpandEnvironmentVariables(value.Trim()));
        var root = Path.GetPathRoot(full)?.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        if (string.IsNullOrWhiteSpace(root)
            || string.Equals(full.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                root, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("不能把磁盘根目录作为安装位置，请选择子目录。");
        }
        if (File.Exists(full))
        {
            throw new InvalidOperationException("所选安装位置是一个文件，请选择目录。");
        }
        if (Directory.Exists(full)
            && File.GetAttributes(full).HasFlag(FileAttributes.ReparsePoint))
        {
            throw new InvalidOperationException("所选安装目录是链接或重解析目录，请选择普通目录。");
        }

        var parent = Path.GetDirectoryName(full)
            ?? throw new InvalidOperationException("安装目录无效。");
        try
        {
            Directory.CreateDirectory(parent);
        }
        catch (Exception error)
        {
            throw new InvalidOperationException($"无法创建安装位置：{error.Message}", error);
        }

        var probe = Path.Combine(parent, $".零售通-写入检查-{Guid.NewGuid():N}.tmp");
        try
        {
            using var stream = new FileStream(
                probe, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096,
                FileOptions.DeleteOnClose);
            stream.WriteByte(1);
        }
        catch (Exception error)
        {
            throw new InvalidOperationException(
                $"所选目录不可写，请选择当前用户可写的安装位置：{parent}", error);
        }

        try
        {
            InstallDirectoryGuard.VerifySiblingDirectoryRename(parent);
            InstallDirectoryGuard.ValidateExistingDirectory(full);
        }
        catch (InvalidOperationException)
        {
            throw;
        }
        catch (Exception error)
        {
            throw new InvalidOperationException(
                $"安装位置安全检查失败：{full}\n\n" +
                $"原因：{error.Message}\n\n" +
                "请关闭占用该目录的程序，或选择其他目录。",
                error);
        }

        var driveRoot = Path.GetPathRoot(full);
        if (!string.IsNullOrWhiteSpace(driveRoot))
        {
            var drive = new DriveInfo(driveRoot);
            if (!drive.IsReady)
            {
                throw new InvalidOperationException("所选安装磁盘当前不可用。");
            }
            var needed = Math.Max(0, requiredBytes) + FreeSpaceReserve;
            if (drive.AvailableFreeSpace < needed)
            {
                throw new InvalidOperationException(
                    $"安装空间不足。预计需要至少 {FormatBytes(needed)}，当前可用 {FormatBytes(drive.AvailableFreeSpace)}。");
            }
        }
        return full;
    }

    internal static void LaunchApplication(string installDirectory)
    {
        var executable = Path.Combine(installDirectory, ApplicationExecutable);
        if (!File.Exists(executable))
        {
            throw new InvalidOperationException("安装目录中未找到零售通程序，无法启动。");
        }
        Process.Start(new ProcessStartInfo(executable)
        {
            WorkingDirectory = installDirectory,
            UseShellExecute = true,
        });
    }

    internal static string ExistingInstallDirectory()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(UninstallKey, writable: false);
            var saved = key?.GetValue("InstallLocation") as string;
            if (!string.IsNullOrWhiteSpace(saved))
            {
                return Path.GetFullPath(Environment.ExpandEnvironmentVariables(saved));
            }
        }
        catch
        {
            // A broken legacy registration must not block a normal install.
        }
        return DefaultInstallDirectory();
    }

    internal static string DefaultInstallDirectory() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Programs",
        DisplayName);

    private static void ApplyShortcutChoices(string installDirectory, Options options)
    {
        var startMenu = StartMenuShortcut();
        var desktop = DesktopShortcut();
        if (options.CreateStartMenuShortcut)
        {
            CreateShortcut(startMenu, installDirectory);
        }
        else
        {
            TryDelete(startMenu);
        }
        if (options.CreateDesktopShortcut)
        {
            CreateShortcut(desktop, installDirectory);
        }
        else
        {
            TryDelete(desktop);
        }
    }

    private static void EnsureApplicationStopped()
    {
        var running = Process.GetProcessesByName(DisplayName).ToList();
        if (running.Count == 0)
        {
            return;
        }
        foreach (var process in running)
        {
            try
            {
                process.CloseMainWindow();
            }
            catch
            {
                // The process may not have a visible main window.
            }
        }
        var deadline = DateTime.UtcNow.AddSeconds(6);
        while (DateTime.UtcNow < deadline && running.Any(process =>
                   {
                       try
                       {
                           return !process.HasExited;
                       }
                       catch
                       {
                           return false;
                       }
                   }))
        {
            Thread.Sleep(150);
        }
        if (running.Any(process =>
            {
                try
                {
                    return !process.HasExited;
                }
                catch
                {
                    return false;
                }
            }))
        {
            throw new InvalidOperationException("零售通仍在运行。请先保存正在处理的内容并退出应用，再重新安装。");
        }
    }

    private static long EstimatePayloadBytes(Stream payload)
    {
        if (!payload.CanSeek)
        {
            return 0;
        }
        var original = payload.Position;
        try
        {
            using var archive = new ZipArchive(payload, ZipArchiveMode.Read, leaveOpen: true);
            return archive.Entries.Sum(entry => Math.Max(0, entry.Length));
        }
        finally
        {
            payload.Position = original;
        }
    }

    private static void ExtractPayload(Stream payload, string destination)
    {
        using var archive = new ZipArchive(payload, ZipArchiveMode.Read, leaveOpen: true);
        var root = Path.GetFullPath(destination) + Path.DirectorySeparatorChar;
        foreach (var entry in archive.Entries)
        {
            var target = Path.GetFullPath(Path.Combine(destination, entry.FullName));
            if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException("安装包包含不安全路径。");
            }
            if (string.IsNullOrEmpty(entry.Name))
            {
                Directory.CreateDirectory(target);
                continue;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            entry.ExtractToFile(target, overwrite: false);
        }
    }

    private static void VerifyPayload(string directory)
    {
        foreach (var relative in new[]
                 {
                     ApplicationExecutable,
                     Path.Combine("runtime", "python.exe"),
                     Path.Combine("app", "server.py"),
                     Path.Combine("app", "backend", "analyzer.py"),
                     Path.Combine("app", "web", "index.html"),
                     Path.Combine("app", "使用说明.html"),
                     Path.Combine("app", "版本.json"),
                     "uninstall.ps1",
                 })
        {
            if (!File.Exists(Path.Combine(directory, relative)))
            {
                throw new InvalidDataException($"安装包缺少必要文件：{relative}");
            }
        }
    }

    private static Dictionary<string, string> ReadMetadata(string directory)
    {
        var path = Path.Combine(directory, "app", "版本.json");
        using var document = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
        var metadata = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var property in document.RootElement.EnumerateObject())
        {
            if (property.Value.ValueKind == JsonValueKind.String)
            {
                metadata[property.Name] = property.Value.GetString() ?? "";
            }
        }
        if (!metadata.TryGetValue("version", out var version)
            || !Version.TryParse(version, out _))
        {
            throw new InvalidDataException("应用版本信息无效。");
        }
        return metadata;
    }

    private static void RegisterUninstall(string installDirectory, Dictionary<string, string> metadata)
    {
        var executable = Path.Combine(installDirectory, ApplicationExecutable);
        var script = Path.Combine(installDirectory, "uninstall.ps1");
        var uninstall = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script}\"";
        using var key = Registry.CurrentUser.CreateSubKey(UninstallKey, writable: true)
            ?? throw new InvalidOperationException("无法写入当前用户卸载信息。");
        key.SetValue("DisplayName", DisplayName, RegistryValueKind.String);
        key.SetValue("DisplayVersion", metadata.GetValueOrDefault("version", ""), RegistryValueKind.String);
        key.SetValue("Publisher", Publisher, RegistryValueKind.String);
        key.SetValue("DisplayIcon", $"\"{executable}\",0", RegistryValueKind.String);
        key.SetValue("InstallLocation", installDirectory, RegistryValueKind.String);
        key.SetValue("UninstallString", uninstall, RegistryValueKind.String);
        key.SetValue("QuietUninstallString", uninstall + " -Mode Normal -Yes", RegistryValueKind.String);
        key.SetValue("NoModify", 1, RegistryValueKind.DWord);
        key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        key.SetValue("EstimatedSize", DirectorySizeKb(installDirectory), RegistryValueKind.DWord);
    }

    private static int DirectorySizeKb(string directory)
    {
        var bytes = new DirectoryInfo(directory).EnumerateFiles("*", SearchOption.AllDirectories)
            .Sum(file => file.Length);
        return Math.Max(1, (int)Math.Min(int.MaxValue, bytes / 1024));
    }

    private static void CreateShortcut(string path, string installDirectory)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var type = Type.GetTypeFromProgID("WScript.Shell")
            ?? throw new InvalidOperationException("无法创建快捷方式。");
        dynamic shell = Activator.CreateInstance(type)!;
        dynamic shortcut = shell.CreateShortcut(path);
        shortcut.TargetPath = Path.Combine(installDirectory, ApplicationExecutable);
        shortcut.WorkingDirectory = installDirectory;
        shortcut.IconLocation = Path.Combine(installDirectory, ApplicationExecutable) + ",0";
        shortcut.Description = "零售通本地经营工作空间";
        shortcut.Save();
    }

    private static string StartMenuShortcut() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs", $"{DisplayName}.lnk");

    private static string DesktopShortcut() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), $"{DisplayName}.lnk");

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch
        {
            // A stale shortcut can be replaced on the next successful install.
        }
    }

    private static bool SamePath(string left, string right) => string.Equals(
        Path.GetFullPath(left).TrimEnd(Path.DirectorySeparatorChar),
        Path.GetFullPath(right).TrimEnd(Path.DirectorySeparatorChar),
        StringComparison.OrdinalIgnoreCase);

    private static string FormatBytes(long bytes) => bytes >= 1024L * 1024 * 1024
        ? $"{bytes / 1024d / 1024 / 1024:0.0} GB"
        : $"{Math.Max(1, bytes / 1024d / 1024):0} MB";

    internal static string InstallerLogPath() => Path.Combine(
        Path.GetTempPath(), "RetailLink-Setup.log");

    internal static void WriteInstallerLog(Exception error)
    {
        try
        {
            File.AppendAllText(
                InstallerLogPath(),
                $"[{DateTimeOffset.Now:O}] {error}{Environment.NewLine}",
                new UTF8Encoding(false));
        }
        catch
        {
            // The user-facing error remains available if the temp folder is read-only.
        }
    }
}

internal sealed record InstallResult(string InstallDirectory, string Version);

internal sealed class Options
{
    public bool Silent { get; init; }
    public bool VerifyOnly { get; init; }
    public bool NoRegistry { get; init; }
    public bool LaunchAfterInstall { get; init; } = true;
    public bool CreateDesktopShortcut { get; init; }
    public bool CreateStartMenuShortcut { get; init; } = true;
    public bool ShowHelp { get; init; }
    public string InstallDirectory { get; init; } = Program.ExistingInstallDirectory();

    public static Options Parse(string[] args)
    {
        var silent = false;
        var verifyOnly = false;
        var noRegistry = false;
        var launch = true;
        var desktop = false;
        var startMenu = true;
        var help = false;
        var installDirectory = Program.ExistingInstallDirectory();
        var launchSpecified = false;
        for (var index = 0; index < args.Length; index++)
        {
            var argument = args[index];
            switch (argument.ToLowerInvariant())
            {
                case "/silent":
                case "--silent":
                    silent = true;
                    break;
                case "/verify-only":
                case "--verify-only":
                    verifyOnly = true;
                    silent = true;
                    break;
                case "/no-registry":
                case "--no-registry":
                    noRegistry = true;
                    break;
                case "/start":
                case "--start":
                    launch = true;
                    launchSpecified = true;
                    break;
                case "/no-launch":
                case "--no-launch":
                    launch = false;
                    launchSpecified = true;
                    break;
                case "/desktop-shortcut":
                case "--desktop-shortcut":
                    desktop = true;
                    break;
                case "/no-desktop-shortcut":
                case "--no-desktop-shortcut":
                    desktop = false;
                    break;
                case "/start-menu-shortcut":
                case "--start-menu-shortcut":
                    startMenu = true;
                    break;
                case "/no-start-menu":
                case "--no-start-menu":
                    startMenu = false;
                    break;
                case "/no-shortcuts":
                case "--no-shortcuts":
                    desktop = false;
                    startMenu = false;
                    break;
                case "/?":
                case "/help":
                case "--help":
                    help = true;
                    break;
                case "/install-dir":
                case "--install-dir":
                    if (++index >= args.Length)
                    {
                        throw new ArgumentException("缺少安装目录。");
                    }
                    installDirectory = Path.GetFullPath(args[index]);
                    break;
                default:
                    throw new ArgumentException($"无法识别安装参数：{argument}");
            }
        }
        if (silent && !launchSpecified)
        {
            launch = false;
        }
        return new Options
        {
            Silent = silent,
            VerifyOnly = verifyOnly,
            NoRegistry = noRegistry,
            LaunchAfterInstall = launch,
            CreateDesktopShortcut = desktop,
            CreateStartMenuShortcut = startMenu,
            ShowHelp = help,
            InstallDirectory = installDirectory,
        };
    }
}

internal sealed class InstallForm : Form
{
    private readonly Options _options;
    private readonly Panel _pageHost = new() { Dock = DockStyle.Fill };
    private readonly TextBox _path = new();
    private readonly CheckBox _launch = new();
    private readonly CheckBox _desktop = new();
    private readonly CheckBox _startMenu = new();
    private bool _installing;

    public InstallForm(Options options)
    {
        _options = options;
        Text = "零售通安装程序";
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(700, 650);
        MinimumSize = new Size(700, 650);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        Font = new Font("Microsoft YaHei UI", 9F);
        BackColor = Color.White;
        Controls.Add(_pageHost);
        ShowOptionsPage();
        FormClosing += OnFormClosing;
    }

    private void ShowOptionsPage()
    {
        var page = NewPage();
        var header = Header("安装零售通", $"Windows 本地经营工作空间 · v{Assembly.GetExecutingAssembly().GetName().Version?.ToString(3)}");
        var description = new Label
        {
            Text = "零售通把日常经营 Excel 整理成本地工作空间，帮助核对商品、库存、销量、计划、售后与货损。",
            Dock = DockStyle.Top,
            Height = 54,
            Padding = new Padding(32, 8, 32, 0),
            ForeColor = Color.FromArgb(95, 80, 85),
        };

        var body = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            ColumnCount = 1,
            Padding = new Padding(32, 4, 32, 0),
        };
        body.Controls.Add(new Label
        {
            Text = "选择安装位置",
            AutoSize = true,
            Font = new Font(Font.FontFamily, 10F, FontStyle.Bold),
            ForeColor = Color.FromArgb(52, 31, 37),
            Margin = new Padding(0, 8, 0, 6),
        });
        var pathRow = new TableLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Top,
            ColumnCount = 2,
            Margin = new Padding(0),
        };
        pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        _path.Text = _options.InstallDirectory;
        _path.Dock = DockStyle.Fill;
        _path.AccessibleName = "安装位置";
        _path.Margin = new Padding(0, 0, 10, 0);
        pathRow.Controls.Add(_path, 0, 0);
        var browse = new Button
        {
            Text = "浏览…",
            AutoSize = true,
            Padding = new Padding(12, 3, 12, 3),
            AccessibleName = "浏览安装位置",
        };
        browse.Click += (_, _) => BrowseInstallDirectory();
        pathRow.Controls.Add(browse, 1, 0);
        body.Controls.Add(pathRow);
        body.Controls.Add(new Label
        {
            Text = "可安装到任意当前用户可写的目录，支持中文和空格。系统会在复制前检查权限与空间。",
            AutoSize = true,
            MaximumSize = new Size(610, 0),
            ForeColor = Color.FromArgb(112, 98, 103),
            Margin = new Padding(0, 7, 0, 17),
        });

        _launch.Text = "立即启动零售通";
        _launch.Checked = _options.LaunchAfterInstall;
        _launch.AutoSize = true;
        _launch.AccessibleName = "立即启动零售通";
        _launch.Margin = new Padding(0, 7, 0, 2);
        body.Controls.Add(_launch);
        body.Controls.Add(OptionHint("安装成功后由你点击“启动零售通”完成启动；取消勾选则不会自动启动。"));

        _desktop.Text = "创建桌面快捷方式";
        _desktop.Checked = _options.CreateDesktopShortcut;
        _desktop.AutoSize = true;
        _desktop.AccessibleName = "创建桌面快捷方式";
        _desktop.Margin = new Padding(0, 13, 0, 2);
        body.Controls.Add(_desktop);
        body.Controls.Add(OptionHint("在桌面创建“零售通”图标，便于直接从桌面打开。"));

        _startMenu.Text = "创建开始菜单快捷方式";
        _startMenu.Checked = _options.CreateStartMenuShortcut;
        _startMenu.AutoSize = true;
        _startMenu.AccessibleName = "创建开始菜单快捷方式";
        _startMenu.Margin = new Padding(0, 13, 0, 2);
        body.Controls.Add(_startMenu);
        body.Controls.Add(OptionHint("在开始菜单中创建入口；Windows 设置中的应用卸载入口始终登记。"));
        var footer = Footer();
        var cancel = new Button
        {
            Text = "取消",
            DialogResult = DialogResult.Cancel,
            AutoSize = true,
            Padding = new Padding(18, 4, 18, 4),
        };
        cancel.Click += (_, _) => Close();
        var install = new Button
        {
            Text = "开始安装",
            AutoSize = true,
            Padding = new Padding(20, 4, 20, 4),
            BackColor = Color.FromArgb(210, 38, 55),
            ForeColor = Color.White,
            FlatStyle = FlatStyle.Flat,
        };
        install.FlatAppearance.BorderSize = 0;
        install.Click += async (_, _) => await BeginInstallAsync();
        footer.Controls.Add(cancel);
        footer.Controls.Add(install);
        AcceptButton = install;
        CancelButton = cancel;
        // WinForms lays out docked controls in reverse z-order. Add the header
        // last so it owns the top edge and the fill/top content follows it.
        page.Controls.Add(footer);
        page.Controls.Add(body);
        page.Controls.Add(description);
        page.Controls.Add(header);
        ShowPage(page);
    }

    private async Task BeginInstallAsync()
    {
        if (_installing)
        {
            return;
        }
        _installing = true;
        var selected = new Options
        {
            InstallDirectory = _path.Text.Trim(),
            LaunchAfterInstall = _launch.Checked,
            CreateDesktopShortcut = _desktop.Checked,
            CreateStartMenuShortcut = _startMenu.Checked,
            NoRegistry = _options.NoRegistry,
        };
        var page = NewPage();
        var header = Header("正在安装零售通", "请稍候，安装完成前不要关闭窗口。");
        var status = new Label
        {
            Text = "正在检查安装位置并准备内置运行环境…",
            AutoSize = true,
            Margin = new Padding(32, 24, 32, 80),
            ForeColor = Color.FromArgb(82, 66, 71),
        };
        var progress = new ProgressBar
        {
            Style = ProgressBarStyle.Marquee,
            MarqueeAnimationSpeed = 24,
            Width = 580,
            Height = 14,
            Margin = new Padding(32, 12, 32, 0),
        };
        var layout = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
        };
        layout.Controls.Add(status);
        layout.Controls.Add(progress);
        page.Controls.Add(layout);
        page.Controls.Add(header);
        ShowPage(page);
        try
        {
            var result = await Task.Run(() => Program.Install(selected));
            ShowCompletePage(result);
        }
        catch (Exception error)
        {
            Program.WriteInstallerLog(error);
            ShowFailurePage(error);
        }
        finally
        {
            _installing = false;
        }
    }

    private void ShowCompletePage(InstallResult result)
    {
        var page = NewPage();
        var header = Header("安装完成", $"零售通 v{result.Version} 已准备就绪");
        var body = new Panel
        {
            Dock = DockStyle.Fill,
            AutoScroll = true,
            Padding = new Padding(32, 20, 32, 8),
        };
        var layout = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
        };
        layout.Controls.Add(InfoLabel($"实际安装位置：{result.InstallDirectory}", true));
        layout.Controls.Add(InfoLabel(
            "零售通把日常经营 Excel 整理成本地工作空间，帮助核对商品、库存、销量、计划、售后与货损。\n\n" +
            "首次使用：\n" +
            "1. 启动后选择或拖入日常经营 Excel。\n" +
            "2. 核对工作表、字段、仓库与业务日期。\n" +
            "3. 预览后生成看盘，并按需导出结果。"));
        layout.Controls.Add(InfoLabel(
            "本地数据与联网说明\n" +
            "应用数据默认保存在 %LOCALAPPDATA%\\零售通，生成文件默认保存在 Documents\\零售通\\生成文件。" +
            "业务工作簿在本机处理，本地服务只监听 127.0.0.1；本版不自动上传业务文件。",
            false,
            Color.FromArgb(255, 241, 242)));
        body.Controls.Add(layout);

        var footer = Footer();
        var open = new Button
        {
            Text = "打开安装目录",
            AutoSize = true,
            Padding = new Padding(16, 4, 16, 4),
        };
        open.Click += (_, _) => OpenDirectory(result.InstallDirectory);
        var finish = new Button
        {
            Text = _launch.Checked ? "启动零售通" : "完成",
            AutoSize = true,
            Padding = new Padding(20, 4, 20, 4),
            BackColor = Color.FromArgb(210, 38, 55),
            ForeColor = Color.White,
            FlatStyle = FlatStyle.Flat,
        };
        finish.FlatAppearance.BorderSize = 0;
        finish.Click += (_, _) =>
        {
            try
            {
                if (_launch.Checked)
                {
                    Program.LaunchApplication(result.InstallDirectory);
                }
                Close();
            }
            catch (Exception error)
            {
                Program.WriteInstallerLog(error);
                MessageBox.Show(this, error.Message, "零售通未能启动",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        };
        footer.Controls.Add(open);
        footer.Controls.Add(finish);
        AcceptButton = finish;
        page.Controls.Add(body);
        page.Controls.Add(footer);
        page.Controls.Add(header);
        ShowPage(page);
    }

    private void ShowFailurePage(Exception error)
    {
        var page = NewPage();
        var header = Header("安装未完成", "没有留下半安装，可以修正问题后重试。");
        var body = new Panel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(32, 24, 32, 16),
            AutoScroll = true,
        };
        body.Controls.Add(new Label
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            MaximumSize = new Size(620, 0),
            Text = $"{error.Message}\n\n诊断日志：{Program.InstallerLogPath()}",
            ForeColor = Color.FromArgb(120, 40, 48),
        });
        var footer = Footer();
        var retry = new Button
        {
            Text = "返回修改",
            AutoSize = true,
            Padding = new Padding(18, 4, 18, 4),
        };
        retry.Click += (_, _) => ShowOptionsPage();
        var close = new Button
        {
            Text = "关闭",
            AutoSize = true,
            Padding = new Padding(18, 4, 18, 4),
        };
        close.Click += (_, _) => Close();
        footer.Controls.Add(close);
        footer.Controls.Add(retry);
        page.Controls.Add(body);
        page.Controls.Add(footer);
        page.Controls.Add(header);
        ShowPage(page);
    }

    private void BrowseInstallDirectory()
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "选择零售通安装位置",
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true,
            SelectedPath = ExistingDirectory(_path.Text),
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            _path.Text = dialog.SelectedPath;
        }
    }

    private void ShowPage(Control page)
    {
        _pageHost.SuspendLayout();
        _pageHost.Controls.Clear();
        _pageHost.Controls.Add(page);
        _pageHost.ResumeLayout();
    }

    private static Panel NewPage() => new()
    {
        Dock = DockStyle.Fill,
        BackColor = Color.White,
    };

    private static Panel Header(string title, string subtitle)
    {
        var panel = new Panel
        {
            Dock = DockStyle.Top,
            Height = 132,
            BackColor = Color.FromArgb(61, 17, 24),
            Padding = new Padding(32, 25, 32, 18),
        };
        panel.Controls.Add(new Label
        {
            Text = subtitle,
            Dock = DockStyle.Bottom,
            Height = 30,
            ForeColor = Color.FromArgb(255, 205, 211),
            Font = new Font("Microsoft YaHei UI", 9F),
        });
        panel.Controls.Add(new Label
        {
            Text = title,
            Dock = DockStyle.Top,
            Height = 52,
            ForeColor = Color.White,
            Font = new Font("Microsoft YaHei UI", 22F, FontStyle.Bold),
        });
        return panel;
    }

    private static FlowLayoutPanel Footer()
    {
        var footer = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = 70,
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false,
            Padding = new Padding(24, 15, 24, 12),
            BackColor = Color.FromArgb(250, 247, 248),
        };
        return footer;
    }

    private static Label OptionHint(string text) => new()
    {
        Text = text,
        AutoSize = true,
        MaximumSize = new Size(600, 0),
        ForeColor = Color.FromArgb(118, 103, 108),
        Margin = new Padding(21, 0, 0, 0),
    };

    private static Label InfoLabel(string text, bool bold = false, Color? background = null) => new()
    {
        Text = text,
        AutoSize = true,
        MaximumSize = new Size(600, 0),
        Font = new Font("Microsoft YaHei UI", 9F, bold ? FontStyle.Bold : FontStyle.Regular),
        ForeColor = Color.FromArgb(61, 46, 51),
        BackColor = background ?? Color.White,
        Padding = background is null ? new Padding(0, 4, 0, 4) : new Padding(16, 12, 16, 12),
        Margin = new Padding(0, 4, 0, 10),
    };

    private static string ExistingDirectory(string value)
    {
        try
        {
            var full = Path.GetFullPath(value);
            while (!string.IsNullOrWhiteSpace(full) && !Directory.Exists(full))
            {
                full = Path.GetDirectoryName(full);
            }
            return string.IsNullOrWhiteSpace(full)
                ? Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)
                : full;
        }
        catch
        {
            return Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        }
    }

    private static void OpenDirectory(string directory)
    {
        if (!Directory.Exists(directory))
        {
            MessageBox.Show("安装目录不存在。", "零售通",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        Process.Start(new ProcessStartInfo("explorer.exe", $"\"{directory}\"")
        {
            UseShellExecute = true,
        });
    }

    private void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        if (!_installing)
        {
            return;
        }
        e.Cancel = true;
        MessageBox.Show(this, "安装正在进行，请等待完成。", "零售通安装程序",
            MessageBoxButtons.OK, MessageBoxIcon.Information);
    }
}
