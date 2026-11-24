using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace RetailLink.Windows;

internal sealed class BackendManager : IAsyncDisposable
{
    private readonly StringBuilder _output = new();
    private readonly object _outputLock = new();
    private Process? _process;
    private string? _launchDirectory;
    private bool _intentionalStop;
    private bool _disposed;

    public Uri? BackendUri { get; private set; }
    public Exception? StartupException { get; private set; }
    public string Output
    {
        get
        {
            lock (_outputLock)
            {
                return _output.ToString();
            }
        }
    }

    public event Action<int, string>? Exited;

    public async Task<Uri> StartAsync(string expectedVersion, CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await StopAsync().ConfigureAwait(false);
        StartupException = null;
        BackendUri = null;
        lock (_outputLock)
        {
            _output.Clear();
        }

        var installDirectory = AppContext.BaseDirectory;
        var python = Path.Combine(installDirectory, "runtime", "python.exe");
        var appDirectory = Path.Combine(installDirectory, "app");
        var server = Path.Combine(appDirectory, "server.py");
        if (!File.Exists(python) || !File.Exists(server))
        {
            throw new InvalidOperationException("内置运行环境不完整，请重新安装零售通。");
        }

        var locations = RuntimeLocations.Create();
        Directory.CreateDirectory(locations.Data);
        Directory.CreateDirectory(locations.Original);
        Directory.CreateDirectory(locations.Export);
        Directory.CreateDirectory(locations.Temporary);
        _launchDirectory = Path.Combine(locations.Temporary, $"retail-link-{Guid.NewGuid():N}");
        Directory.CreateDirectory(_launchDirectory);
        var readyFile = Path.Combine(_launchDirectory, "backend-ready.json");

        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            WorkingDirectory = appDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var argument in new[]
                 {
                     "-B", server, "--desktop", "--parent-stdin", "--port", "0",
                     "--data-dir", locations.Data,
                     "--original-dir", locations.Original,
                     "--export-dir", locations.Export,
                     "--ready-file", readyFile,
                 })
        {
            startInfo.ArgumentList.Add(argument);
        }
        startInfo.Environment.Remove("PYTHONHOME");
        startInfo.Environment.Remove("PYTHONPATH");
        startInfo.Environment["PYTHONNOUSERSITE"] = "1";
        startInfo.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        startInfo.Environment["PYTHONUTF8"] = "1";
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        if (Environment.GetEnvironmentVariable("RETAIL_LINK_TEST_ROOT") is not null)
        {
            startInfo.Environment["LOCALAPPDATA"] = locations.LocalAppData;
        }

        var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, args) => AppendOutput(args.Data);
        process.ErrorDataReceived += (_, args) => AppendOutput(args.Data);
        process.Exited += (_, _) =>
        {
            if (!_intentionalStop)
            {
                Exited?.Invoke(process.ExitCode, Output);
            }
        };
        _process = process;

        try
        {
            if (!process.Start())
            {
                throw new InvalidOperationException("无法启动内置本地服务。");
            }
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        catch (Exception error)
        {
            StartupException = error;
            await StopAsync().ConfigureAwait(false);
            throw;
        }

        var deadline = DateTime.UtcNow.AddSeconds(40);
        while (DateTime.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (process.HasExited)
            {
                var detail = Output.Trim();
                throw new InvalidOperationException(detail.Length == 0
                    ? $"本地服务已退出（状态 {process.ExitCode}）。"
                    : $"本地服务已退出（状态 {process.ExitCode}）。\n\n{detail}");
            }
            if (TryReadReady(readyFile, process.Id, expectedVersion, out var uri, out var problem))
            {
                BackendUri = uri!;
                return uri!;
            }
            if (problem is not null && DateTime.UtcNow > deadline.AddSeconds(-30))
            {
                throw new InvalidOperationException(problem);
            }
            await Task.Delay(100, cancellationToken).ConfigureAwait(false);
        }

        var output = Output.Trim();
        throw new InvalidOperationException(output.Length == 0
            ? "本地服务启动超时，请重试。"
            : $"本地服务启动超时，请重试。\n\n{output}");
    }

    public async Task StopAsync()
    {
        var process = _process;
        _process = null;
        _intentionalStop = true;
        BackendUri = null;
        if (process is not null)
        {
            try
            {
                if (!process.HasExited)
                {
                    try
                    {
                        process.StandardInput.Close();
                    }
                    catch
                    {
                        // The child may already have closed its end of the pipe.
                    }
                    using var timeout = new CancellationTokenSource(TimeSpan.FromMilliseconds(2500));
                    try
                    {
                        await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        process.Kill(entireProcessTree: true);
                        await process.WaitForExitAsync().ConfigureAwait(false);
                    }
                }
            }
            catch
            {
                // Shutdown is best effort; the parent stdin pipe still terminates the backend.
            }
            finally
            {
                process.Dispose();
            }
        }

        if (_launchDirectory is not null)
        {
            try
            {
                Directory.Delete(_launchDirectory, recursive: true);
            }
            catch
            {
                // Temporary files are harmless and can be reclaimed by Windows later.
            }
            _launchDirectory = null;
        }
        _intentionalStop = false;
    }

    private bool TryReadReady(
        string readyFile,
        int processId,
        string expectedVersion,
        out Uri? uri,
        out string? problem)
    {
        uri = null;
        problem = null;
        try
        {
            if (!File.Exists(readyFile))
            {
                return false;
            }
            using var document = JsonDocument.Parse(File.ReadAllText(readyFile, Encoding.UTF8));
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("url", out var urlNode)
                || urlNode.ValueKind != JsonValueKind.String
                || !Uri.TryCreate(urlNode.GetString(), UriKind.Absolute, out var endpoint))
            {
                problem = "本地服务返回的就绪信息无效。";
                return false;
            }
            var path = endpoint.AbsolutePath;
            if (endpoint.Scheme != Uri.UriSchemeHttp
                || endpoint.Host != "127.0.0.1"
                || endpoint.Port is < 1 or > 65535
                || !string.IsNullOrEmpty(endpoint.UserInfo)
                || !string.IsNullOrEmpty(endpoint.Query)
                || !string.IsNullOrEmpty(endpoint.Fragment)
                || (path.Length > 0 && path != "/"))
            {
                problem = "本地服务返回了不安全的就绪地址。";
                return false;
            }
            if (!root.TryGetProperty("pid", out var pidNode)
                || !pidNode.TryGetInt32(out var pid)
                || pid != processId)
            {
                problem = "本地服务进程身份校验失败。";
                return false;
            }
            if (!root.TryGetProperty("version", out var versionNode)
                || versionNode.ValueKind != JsonValueKind.String
                || versionNode.GetString() != expectedVersion)
            {
                problem = "本地服务版本与客户端不一致。";
                return false;
            }
            uri = endpoint;
            return true;
        }
        catch (IOException)
        {
            return false;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private void AppendOutput(string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return;
        }
        lock (_outputLock)
        {
            _output.AppendLine(text);
            if (_output.Length > 16000)
            {
                _output.Remove(0, _output.Length - 16000);
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        await StopAsync().ConfigureAwait(false);
    }
}

internal sealed record RuntimeLocations(
    string Data,
    string Original,
    string Export,
    string Temporary,
    string LocalAppData)
{
    public static RuntimeLocations Create()
    {
        var testRoot = Environment.GetEnvironmentVariable("RETAIL_LINK_TEST_ROOT");
        if (!string.IsNullOrWhiteSpace(testRoot))
        {
            var root = Path.GetFullPath(testRoot);
            var local = Path.Combine(root, "AppData", "Local");
            var data = Path.Combine(local, "零售通");
            return new RuntimeLocations(
                data,
                Path.Combine(data, "原始资料"),
                Path.Combine(root, "Documents", "零售通", "生成文件"),
                Path.Combine(root, "Temporary", "RetailLink"),
                local);
        }

        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var documents = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        if (string.IsNullOrWhiteSpace(documents))
        {
            documents = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Documents");
        }
        var applicationData = Path.Combine(localAppData, "零售通");
        return new RuntimeLocations(
            applicationData,
            Path.Combine(applicationData, "原始资料"),
            Path.Combine(documents, "零售通", "生成文件"),
            Path.Combine(Path.GetTempPath(), "零售通"),
            localAppData);
    }
}
