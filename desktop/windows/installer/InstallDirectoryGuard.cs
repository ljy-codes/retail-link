using System.Text.Json;

namespace RetailLink.Setup;

internal static class InstallDirectoryGuard
{
    private const string DisplayName = "零售通";
    private const string ApplicationExecutable = "零售通.exe";
    private const string Uninstaller = "uninstall.ps1";

    internal static void ValidateExistingDirectory(string directory)
    {
        if (!Directory.Exists(directory))
        {
            return;
        }

        string[] entries;
        try
        {
            entries = Directory
                .EnumerateFileSystemEntries(directory, "*", SearchOption.TopDirectoryOnly)
                .Take(1)
                .ToArray();
        }
        catch (Exception error)
        {
            throw new InvalidOperationException(
                $"安装位置不可用：{directory}\n\n" +
                "失败阶段：安装前目录权限检查。\n" +
                $"原因：{error.Message}\n\n" +
                "请点击“浏览…”选择当前用户可写的其他目录。",
                error);
        }

        if (entries.Length == 0)
        {
            return;
        }

        if (!IsRetailLinkInstallDirectory(directory))
        {
            var parent = Path.GetDirectoryName(directory);
            var suggested = string.IsNullOrWhiteSpace(parent)
                ? directory
                : Path.Combine(parent, DisplayName);
            throw new InvalidOperationException(
                $"安装位置不可用：{directory}\n\n" +
                "该目录已经存在且包含其他文件，或者不是零售通安装目录。\n" +
                "为避免覆盖、搬移或删除已有文件，安装器不会继续。\n\n" +
                "请点击“浏览…”选择一个新的空文件夹，或直接输入一个尚不存在的目录，例如：\n" +
                suggested);
        }

        VerifyDirectoryWritable(directory);
    }

    internal static void VerifyExistingDirectoryCanBeReplaced(string directory)
    {
        if (!Directory.Exists(directory))
        {
            return;
        }

        var parent = Path.GetDirectoryName(directory)
            ?? throw new InvalidOperationException($"安装位置无效：{directory}");
        var probe = Path.Combine(parent, $".零售通-替换检查-{Guid.NewGuid():N}");
        var temporarilyMoved = false;
        try
        {
            Directory.Move(directory, probe);
            temporarilyMoved = true;
            Directory.Move(probe, directory);
            temporarilyMoved = false;
        }
        catch (Exception error)
        {
            if (temporarilyMoved && Directory.Exists(probe) && !Directory.Exists(directory))
            {
                try
                {
                    Directory.Move(probe, directory);
                    temporarilyMoved = false;
                }
                catch (Exception restoreError)
                {
                    throw new InvalidOperationException(
                        $"安装位置替换检查失败，且自动恢复未完成。\n\n" +
                        $"原目录已临时移动到：{probe}\n" +
                        $"应恢复为：{directory}\n\n" +
                        "请关闭占用该目录的程序后，将上述临时目录改回原路径。\n" +
                        $"原因：{error.Message}\n" +
                        $"恢复失败：{restoreError.Message}",
                        new AggregateException(error, restoreError));
                }
            }

            throw new InvalidOperationException(
                $"安装位置不可替换：{directory}\n\n" +
                "失败阶段：安装前目录占用/权限检查。\n" +
                $"原因：{error.Message}\n\n" +
                "该目录可能正被资源管理器、编辑器或其他程序占用。" +
                "请关闭占用该目录的程序，或点击“浏览…”选择其他目录。",
                error);
        }
    }

    internal static void VerifySiblingDirectoryRename(string parent)
    {
        Directory.CreateDirectory(parent);
        var probe = Path.Combine(parent, $".零售通-目录检查-{Guid.NewGuid():N}");
        var renamed = probe + "-renamed";
        try
        {
            Directory.CreateDirectory(probe);
            Directory.Move(probe, renamed);
        }
        catch (Exception error)
        {
            throw new InvalidOperationException(
                $"所选目录不可写，或无法在安装位置创建/替换文件：{parent}\n\n" +
                $"原因：{error.Message}\n\n" +
                "请选择当前用户可写的其他目录。",
                error);
        }
        finally
        {
            TryDeleteDirectory(probe);
            TryDeleteDirectory(renamed);
        }
    }

    internal static bool IsRetailLinkInstallDirectory(string directory)
    {
        try
        {
            var executable = Path.Combine(directory, ApplicationExecutable);
            var uninstaller = Path.Combine(directory, Uninstaller);
            var metadataPath = Path.Combine(directory, "app", "版本.json");
            if (!File.Exists(executable)
                || !File.Exists(uninstaller)
                || !File.Exists(metadataPath))
            {
                return false;
            }

            using var document = JsonDocument.Parse(File.ReadAllText(metadataPath));
            var root = document.RootElement;
            return root.TryGetProperty("name", out var name)
                   && name.ValueKind == JsonValueKind.String
                   && name.GetString() == DisplayName
                   && root.TryGetProperty("platform", out var platform)
                   && platform.ValueKind == JsonValueKind.String
                   && string.Equals(platform.GetString(), "windows", StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    private static void VerifyDirectoryWritable(string directory)
    {
        var probe = Path.Combine(directory, $".零售通-写入检查-{Guid.NewGuid():N}.tmp");
        try
        {
            using var stream = new FileStream(
                probe,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                4096,
                FileOptions.DeleteOnClose);
            stream.WriteByte(1);
        }
        catch (Exception error)
        {
            throw new InvalidOperationException(
                $"安装位置不可写：{directory}\n\n" +
                $"原因：{error.Message}\n\n" +
                "请点击“浏览…”选择当前用户可写的其他目录。",
                error);
        }
        finally
        {
            TryDeleteFile(probe);
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch
        {
            // The primary validation result is more useful than cleanup noise.
        }
    }

    private static void TryDeleteDirectory(string path)
    {
        try
        {
            if (Directory.Exists(path))
            {
                Directory.Delete(path, recursive: true);
            }
        }
        catch
        {
            // The primary validation result is more useful than cleanup noise.
        }
    }
}
