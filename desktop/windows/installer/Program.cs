using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
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
    // The application project is RetailLink.Windows.csproj.
    // Per-user installation uses LOCALAPPDATA\Programs\零售通.

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
                    "零售通.exe /silent\n" +
                    "零售通.exe /verify-only\n" +
                    "零售通.exe /install-dir <目录> /no-registry /no-shortcuts",
                    "零售通安装程序", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }
            if (!options.Silent)
            {
                Application.Run(new InstallForm(options));
                return Environment.ExitCode;
            }
            Install(options);
            return 0;
        }
        catch (Exception error)
        {
            if (args.Contains("/silent", StringComparer.OrdinalIgnoreCase))
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

    internal static void Install(Options options)
    {
        var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
            ?? throw new InvalidOperationException("安装包载荷缺失，请重新下载完整安装程序。");
        using var payload = resource;
        var installDirectory = Path.GetFullPath(options.InstallDirectory);
        var localPrograms = Path.GetFullPath(Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs"));
        if (!installDirectory.StartsWith(localPrograms + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
            && !options.AllowCustomDirectory)
        {
            throw new InvalidOperationException("安装目录必须位于当前用户的 LocalAppData\\Programs 下。");
        }
        if (Path.GetFileName(installDirectory) != DisplayName && !options.AllowCustomDirectory)
        {
            throw new InvalidOperationException("安装目录名称必须为“零售通”。");
        }

        var running = Process.GetProcessesByName(DisplayName).FirstOrDefault();
        if (running is not null)
        {
            throw new InvalidOperationException("零售通正在运行，请退出应用后重新安装。");
        }

        var parent = Path.GetDirectoryName(installDirectory)
            ?? throw new InvalidOperationException("安装目录无效。");
        Directory.CreateDirectory(parent);
        var staging = Path.Combine(parent, $".零售通-安装-{Guid.NewGuid():N}");
        var backup = Path.Combine(parent, $".零售通-旧版-{Guid.NewGuid():N}");
        try
        {
            Directory.CreateDirectory(staging);
            ExtractPayload(payload, staging);
            VerifyPayload(staging);
            if (options.VerifyOnly)
            {
                Directory.Delete(staging, recursive: true);
                return;
            }

            if (Directory.Exists(installDirectory))
            {
                Directory.Move(installDirectory, backup);
            }
            try
            {
                Directory.Move(staging, installDirectory);
            }
            catch
            {
                if (Directory.Exists(backup) && !Directory.Exists(installDirectory))
                {
                    Directory.Move(backup, installDirectory);
                }
                throw;
            }

            var metadata = ReadMetadata(installDirectory);
            if (!options.NoShortcuts)
            {
                CreateShortcut(
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
                        "Programs", $"{DisplayName}.lnk"),
                    installDirectory);
                if (options.DesktopShortcut)
                {
                    CreateShortcut(
                        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                            $"{DisplayName}.lnk"),
                        installDirectory);
                }
            }
            if (!options.NoRegistry)
            {
                RegisterUninstall(installDirectory, metadata);
            }
            if (Directory.Exists(backup))
            {
                Directory.Delete(backup, recursive: true);
            }
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
        var uninstall = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script}\" -InstallDir \"{installDirectory}\"";
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
}

internal sealed class Options
{
    public bool Silent { get; private init; }
    public bool VerifyOnly { get; private init; }
    public bool NoRegistry { get; private init; }
    public bool NoShortcuts { get; private init; }
    public bool DesktopShortcut { get; private init; }
    public bool AllowCustomDirectory { get; private init; }
    public bool ShowHelp { get; private init; }
    public string InstallDirectory { get; private init; } = DefaultInstallDirectory();

    public static Options Parse(string[] args)
    {
        var silent = false;
        var verifyOnly = false;
        var noRegistry = false;
        var noShortcuts = false;
        var desktop = false;
        var allowCustom = false;
        var help = false;
        var installDirectory = DefaultInstallDirectory();
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
                case "/no-shortcuts":
                case "--no-shortcuts":
                    noShortcuts = true;
                    break;
                case "/desktop-shortcut":
                case "--desktop-shortcut":
                    desktop = true;
                    break;
                case "/allow-custom-directory":
                case "--allow-custom-directory":
                    allowCustom = true;
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
        return new Options
        {
            Silent = silent,
            VerifyOnly = verifyOnly,
            NoRegistry = noRegistry,
            NoShortcuts = noShortcuts,
            DesktopShortcut = desktop,
            AllowCustomDirectory = allowCustom,
            ShowHelp = help,
            InstallDirectory = installDirectory,
        };
    }

    private static string DefaultInstallDirectory() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Programs",
        "零售通");
}

internal sealed class InstallForm : Form
{
    private readonly Options _options;
    private readonly Label _status = new() { AutoSize = true, Text = "正在安装零售通…" };
    private readonly ProgressBar _progress = new()
    {
        Style = ProgressBarStyle.Marquee,
        MarqueeAnimationSpeed = 28,
        Width = 360,
        Height = 18,
    };

    public InstallForm(Options options)
    {
        _options = options;
        Text = "零售通安装程序";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(440, 150);
        MaximizeBox = false;
        MinimizeBox = false;
        Font = new Font("Microsoft YaHei UI", 9F);
        BackColor = Color.White;
        var layout = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
            Padding = new Padding(40, 32, 32, 20),
        };
        layout.Controls.Add(_status);
        layout.Controls.Add(_progress);
        Controls.Add(layout);
        Shown += (_, _) => BeginInstall();
    }

    private async void BeginInstall()
    {
        try
        {
            await Task.Run(() => Program.Install(_options));
            _progress.Style = ProgressBarStyle.Continuous;
            _progress.Value = 100;
            _status.Text = "安装完成。";
            MessageBox.Show(this,
                "零售通已安装。\n\n历史与应用配置位于本地应用数据目录；源文件和生成文件不会随升级删除。",
                "安装完成", MessageBoxButtons.OK, MessageBoxIcon.Information);
            Close();
        }
        catch (Exception error)
        {
            _progress.Style = ProgressBarStyle.Continuous;
            _status.Text = "安装未完成。";
            MessageBox.Show(this, error.Message, "安装未完成",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }
}
