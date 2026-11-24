using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace RetailLink.Windows;

internal static class Program
{
    private const string MutexName = @"Local\RetailLink.Desktop.Windows";

    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, args) => ReportFatal(args.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            ReportFatal(args.ExceptionObject as Exception);

        using var mutex = new Mutex(true, MutexName, out var created);
        if (!created)
        {
            ActivateExistingWindow();
            return;
        }

        try
        {
            Application.Run(new MainForm());
        }
        finally
        {
            mutex.ReleaseMutex();
        }
    }

    private static void ActivateExistingWindow()
    {
        var current = Environment.ProcessId;
        var existing = Process.GetProcessesByName("零售通")
            .FirstOrDefault(process => process.Id != current);
        if (existing is not null && existing.MainWindowHandle != IntPtr.Zero)
        {
            ShowWindow(existing.MainWindowHandle, ShowWindowRestore);
            SetForegroundWindow(existing.MainWindowHandle);
        }
    }

    private static void ReportFatal(Exception? error)
    {
        var message = error?.Message ?? "发生未知错误。";
        try
        {
            var directory = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "零售通");
            Directory.CreateDirectory(directory);
            File.AppendAllText(
                Path.Combine(directory, "客户端错误.log"),
                $"[{DateTimeOffset.Now:O}] {error}{Environment.NewLine}",
                new UTF8Encoding(false));
        }
        catch
        {
            // The dialog remains available even when the log directory is read-only.
        }

        MessageBox.Show(
            $"零售通遇到错误，无法继续运行。\n\n{message}",
            "零售通",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }

    private const int ShowWindowRestore = 9;

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);
}
