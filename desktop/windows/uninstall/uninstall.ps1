param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir,
    [ValidateSet('Prompt', 'Normal', 'Complete')]
    [string]$Mode = 'Prompt',
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName Microsoft.VisualBasic

$applicationName = '零售通'
# Per-user program data lives below LOCALAPPDATA; Documents is never an uninstall target.
$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$expectedInstall = [IO.Path]::GetFullPath((Join-Path $localAppData 'Programs\零售通'))
$resolvedInstall = [IO.Path]::GetFullPath($InstallDir)
if ($resolvedInstall -ne $expectedInstall) {
    throw "拒绝卸载非标准安装目录：$resolvedInstall"
}

$applicationData = Join-Path $localAppData '零售通'
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\零售通.lnk'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) '零售通.lnk'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RetailLink'

function Move-ToRecycleBin {
    param([string]$Path, [switch]$Directory)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        if ($Directory) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
                $Path,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
        } else {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
                $Path,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
        }
    } catch {
        # Fall back to a recoverable holding folder when this volume cannot use the recycle bin.
        $holding = Join-Path $env:TEMP ("零售通-卸载-" + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $holding -Force | Out-Null
        Move-Item -LiteralPath $Path -Destination $holding
        Write-Warning "项目无法进入回收站，已移动到可恢复位置：$holding"
    }
}

if ($Mode -eq 'Prompt') {
    $choice = [System.Windows.Forms.MessageBox]::Show(
        "请选择卸载方式：`n`n普通卸载：只删除程序，保留历史、设置、日志和原始资料。`n`n彻底卸载：删除程序及零售通应用数据，但始终保留《原始资料》和 Documents 中的用户文件。`n`n卸载程序会尽量将内容移入回收站，以便误操作恢复。",
        "卸载零售通",
        [System.Windows.Forms.MessageBoxButtons]::YesNoCancel,
        [System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($choice -eq [System.Windows.Forms.DialogResult]::Cancel) { exit 0 }
    if ($choice -eq [System.Windows.Forms.DialogResult]::Yes) { $Mode = 'Normal' }
    elseif ($choice -eq [System.Windows.Forms.DialogResult]::No) { $Mode = 'Complete' }
}

if (-not $Yes) {
    $summary = if ($Mode -eq 'Complete') {
        "将删除程序与零售通应用数据（原始资料除外）。Documents 和用户源文件不会被删除。"
    } else {
        "将删除程序，保留历史、设置、日志和原始资料。"
    }
    $confirm = [System.Windows.Forms.MessageBox]::Show(
        "$summary`n`n是否继续？",
        "确认卸载零售通",
        [System.Windows.Forms.MessageBoxButtons]::OKCancel,
        [System.Windows.Forms.MessageBoxIcon]::Question)
    if ($confirm -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
}

Get-Process -Name 零售通 -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 300

Move-ToRecycleBin -Path $startMenuShortcut
Move-ToRecycleBin -Path $desktopShortcut
if (Test-Path -LiteralPath $uninstallKey) {
    Remove-Item -LiteralPath $uninstallKey -Recurse -Force
}

if ($Mode -eq 'Complete' -and (Test-Path -LiteralPath $applicationData)) {
    # Source files can be the user's only copy. Preserve them even in a full uninstall.
    Get-ChildItem -LiteralPath $applicationData -Force |
        Where-Object { $_.Name -notin @('原始资料', '导入文件', '生成文件') } |
        ForEach-Object {
            Move-ToRecycleBin -Path $_.FullName -Directory:$_.PSIsContainer
        }
}

# Documents and user-generated files are never part of either uninstall mode.
Move-ToRecycleBin -Path $resolvedInstall -Directory
