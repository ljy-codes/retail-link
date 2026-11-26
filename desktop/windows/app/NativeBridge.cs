using System.Diagnostics;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace RetailLink.Windows;

internal sealed class NativeBridge
{
    // Results are delivered through RetailLinkDesktop.resolve on the local page.
    private readonly WebView2 _webView;
    private readonly Form _owner;
    private readonly Func<int?> _backendPort;

    public event Action? FrontendReady;

    public NativeBridge(WebView2 webView, Form owner, Func<int?> backendPort)
    {
        _webView = webView;
        _owner = owner;
        _backendPort = backendPort;
        _webView.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
    }

    private async void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        if (!Trusted(args.Source))
        {
            return;
        }

        JsonElement identifier = default;
        try
        {
            using var document = JsonDocument.Parse(args.WebMessageAsJson);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("action", out var actionNode)
                || actionNode.ValueKind != JsonValueKind.String)
            {
                return;
            }

            var action = actionNode.GetString();
            if (action == "frontendReady")
            {
                FrontendReady?.Invoke();
                return;
            }

            if (!root.TryGetProperty("id", out identifier))
            {
                return;
            }
            if (!ValidIdentifier(identifier))
            {
                return;
            }

            var result = action switch
            {
                "chooseDirectory" => ChooseDirectory(root),
                "chooseFile" => ChooseFile(root),
                "saveFile" => SaveFile(root),
                "showInFolder" => ShowInFolder(root),
                _ => Error("不支持此系统操作。"),
            };
            await ResolveAsync(identifier, result).ConfigureAwait(true);
        }
        catch (Exception error)
        {
            if (identifier.ValueKind is JsonValueKind.String or JsonValueKind.Number)
            {
                await ResolveAsync(identifier, Error(error.Message)).ConfigureAwait(true);
            }
        }
    }

    private Dictionary<string, object?> ChooseDirectory(JsonElement root)
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "选择文件夹",
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true,
            SelectedPath = ExistingDirectory(ReadString(root, "initialPath")),
        };
        return dialog.ShowDialog(_owner) == DialogResult.OK
            ? new Dictionary<string, object?> { ["path"] = dialog.SelectedPath }
            : new Dictionary<string, object?> { ["cancelled"] = true };
    }

    private Dictionary<string, object?> ChooseFile(JsonElement root)
    {
        using var dialog = new OpenFileDialog
        {
            Title = "选择文件",
            CheckFileExists = true,
            Multiselect = false,
            Filter = "Excel、CSV 或 ZIP 文件|*.xlsx;*.xlsm;*.xls;*.csv;*.zip|所有文件|*.*",
            InitialDirectory = ExistingDirectory(ReadString(root, "initialPath")),
        };
        return dialog.ShowDialog(_owner) == DialogResult.OK
            ? new Dictionary<string, object?> { ["path"] = dialog.FileName }
            : new Dictionary<string, object?> { ["cancelled"] = true };
    }

    private Dictionary<string, object?> SaveFile(JsonElement root)
    {
        var initial = ExistingDirectory(ReadString(root, "initialPath"));
        var filename = SafeFilename(ReadString(root, "filename"), "零售通.xlsx");
        using var dialog = new SaveFileDialog
        {
            Title = "选择保存位置",
            AddExtension = true,
            OverwritePrompt = true,
            CheckPathExists = true,
            FileName = filename,
            InitialDirectory = initial,
            Filter = FilterFor(filename),
            DefaultExt = Path.GetExtension(filename).TrimStart('.'),
        };
        return dialog.ShowDialog(_owner) == DialogResult.OK
            ? new Dictionary<string, object?> { ["path"] = dialog.FileName }
            : new Dictionary<string, object?> { ["cancelled"] = true };
    }

    private Dictionary<string, object?> ShowInFolder(JsonElement root)
    {
        var path = ReadString(root, "path") ?? ReadString(root, "initialPath");
        if (string.IsNullOrWhiteSpace(path))
        {
            return Error("文件或文件夹不存在。");
        }
        var full = Path.GetFullPath(Environment.ExpandEnvironmentVariables(path));
        if (Directory.Exists(full))
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{full}\"") { UseShellExecute = true });
            return new Dictionary<string, object?> { ["path"] = full };
        }
        if (File.Exists(full))
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{full}\"") { UseShellExecute = true });
            return new Dictionary<string, object?> { ["path"] = full };
        }
        return Error("文件或文件夹不存在。");
    }

    private async Task ResolveAsync(JsonElement identifier, object result)
    {
        if (_webView.CoreWebView2 is null)
        {
            return;
        }
        var idJson = identifier.ValueKind == JsonValueKind.Number
            ? identifier.GetRawText()
            : JsonSerializer.Serialize(identifier.GetString());
        var resultJson = JsonSerializer.Serialize(result);
        var script = $"window.RetailLinkDesktop && window.RetailLinkDesktop.resolve({idJson},{resultJson});";
        try
        {
            await _webView.CoreWebView2.ExecuteScriptAsync(script);
        }
        catch
        {
            // The page may have navigated or closed while a native dialog was open.
        }
    }

    private bool Trusted(string? source)
    {
        var port = _backendPort();
        return port is not null
            && Uri.TryCreate(source, UriKind.Absolute, out var uri)
            && uri.Scheme == Uri.UriSchemeHttp
            && uri.Host == "127.0.0.1"
            && uri.Port == port;
    }

    private static bool ValidIdentifier(JsonElement identifier)
    {
        if (identifier.ValueKind == JsonValueKind.String)
        {
            var value = identifier.GetString();
            return !string.IsNullOrWhiteSpace(value) && value.Length <= 128;
        }
        return identifier.ValueKind == JsonValueKind.Number && identifier.TryGetInt64(out _);
    }

    private static string? ReadString(JsonElement root, string name)
    {
        return root.TryGetProperty(name, out var node) && node.ValueKind == JsonValueKind.String
            ? node.GetString()
            : null;
    }

    private static string ExistingDirectory(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        }
        try
        {
            var full = Path.GetFullPath(Environment.ExpandEnvironmentVariables(value.Trim()));
            if (Directory.Exists(full))
            {
                return full;
            }
            var parent = Directory.Exists(full) ? full : Path.GetDirectoryName(full);
            while (!string.IsNullOrWhiteSpace(parent) && !Directory.Exists(parent))
            {
                parent = Path.GetDirectoryName(parent);
            }
            return parent ?? Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        }
        catch
        {
            return Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        }
    }

    private static string SafeFilename(string? value, string fallback)
    {
        var name = string.IsNullOrWhiteSpace(value) ? fallback : Path.GetFileName(value);
        foreach (var character in Path.GetInvalidFileNameChars())
        {
            name = name.Replace(character, '-');
        }
        name = name.Trim();
        if (name.Length == 0 || name is "." or "..")
        {
            name = fallback;
        }
        return name.Length > 180 ? name[..180] : name;
    }

    private static string FilterFor(string filename) => Path.GetExtension(filename).ToLowerInvariant() switch
    {
        ".zip" => "ZIP 迁移包|*.zip|所有文件|*.*",
        ".csv" => "CSV 文件|*.csv|所有文件|*.*",
        ".xlsx" or ".xlsm" or ".xls" => "Excel 文件|*.xlsx;*.xlsm;*.xls|所有文件|*.*",
        _ => "所有文件|*.*",
    };

    private static Dictionary<string, object?> Error(string message) =>
        new() { ["error"] = message };
}
