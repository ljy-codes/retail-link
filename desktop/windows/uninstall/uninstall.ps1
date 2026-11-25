param(
    [string]$InstallDir = '',
    [ValidateSet('Prompt', 'Normal', 'Complete')]
    [string]$Mode = 'Prompt',
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic

$applicationName = '零售通'
$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$applicationData = Join-Path $localAppData $applicationName
$documents = [Environment]::GetFolderPath('MyDocuments')
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\零售通.lnk'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) '零售通.lnk'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RetailLink'
$registryUninstallPath = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\RetailLink'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$uninstallRecordDirectory = Join-Path $documents '零售通\卸载记录'
$logPath = Join-Path $uninstallRecordDirectory ("卸载-$timestamp.log")
$script:deleted = New-Object 'System.Collections.Generic.List[string]'
$script:retained = New-Object 'System.Collections.Generic.List[string]'
$script:failed = New-Object 'System.Collections.Generic.List[string]'
$script:selectedMode = $Mode

function Add-Result {
    param(
        [ValidateSet('Deleted', 'Retained', 'Failed')]
        [string]$Kind,
        [string]$Message
    )
    if ($Kind -eq 'Deleted') { $script:deleted.Add($Message) }
    elseif ($Kind -eq 'Retained') { $script:retained.Add($Message) }
    else { $script:failed.Add($Message) }
}

function Write-UninstallRecord {
    param([string]$Text)
    try {
        if (-not (Test-Path -LiteralPath $uninstallRecordDirectory)) {
            New-Item -ItemType Directory -Path $uninstallRecordDirectory -Force | Out-Null
        }
        Add-Content -LiteralPath $logPath -Value $Text -Encoding UTF8
    } catch {
        # The result form still shows the in-memory result if the log cannot be written.
    }
}

function Test-ProtectedPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $true }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    $protected = @(
        $root,
        [IO.Path]::GetFullPath($documents).TrimEnd('\'),
        [IO.Path]::GetFullPath($applicationData).TrimEnd('\')
    )
    return $protected -contains $full
}

function Get-RegisteredInstallDirectory {
    $registered = $null
    try {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($registryUninstallPath, $false)
        if ($null -ne $key) {
            $registered = $key.GetValue(
                'InstallLocation',
                $null,
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $key.Close()
        }
    } catch {
        Write-UninstallRecord "无法读取 HKCU InstallLocation：$($_.Exception.Message)"
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$registered)) {
        return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables([string]$registered))
    }
    if (-not [string]::IsNullOrWhiteSpace($InstallDir)) {
        return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($InstallDir))
    }
    return $null
}

function Move-ToRecoverableLocation {
    param(
        [string]$Path,
        [string]$Label,
        [switch]$Directory
    )
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
        Add-Result 'Deleted' "$Label（已移入回收站）"
        Write-UninstallRecord "已删除：$Path（回收站）"
    } catch {
        try {
            $holding = Join-Path $env:TEMP ("零售通-卸载-" + [Guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $holding -Force | Out-Null
            $destination = Join-Path $holding (Split-Path -Leaf $Path)
            Move-Item -LiteralPath $Path -Destination $destination
            Add-Result 'Deleted' "$Label（已移动到可恢复位置：$destination）"
            Write-UninstallRecord "已删除：$Path（可恢复位置：$destination）"
        } catch {
            Add-Result 'Failed' "$Label：$($_.Exception.Message)"
            Write-UninstallRecord "失败：$Path：$($_.Exception.Message)"
        }
    }
}

function Stop-ApplicationForUninstall {
    $processes = @(Get-Process -Name $applicationName -ErrorAction SilentlyContinue)
    if ($processes.Count -eq 0) { return $true }
    if (-not $Yes) {
        $choice = [System.Windows.Forms.MessageBox]::Show(
            "零售通正在运行。卸载前需要关闭应用，未保存的页面操作可能丢失。`n`n是否关闭零售通并继续？",
            '零售通正在运行',
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) { return $false }
    }
    foreach ($process in $processes) {
        try { [void]$process.CloseMainWindow() } catch {}
    }
    Start-Sleep -Milliseconds 1200
    foreach ($process in $processes) {
        try {
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction Stop }
        } catch {
            Add-Result 'Failed' "关闭运行中的零售通：$($_.Exception.Message)"
        }
    }
    return $true
}

function Show-ScopeForm {
    param([string]$ResolvedInstall)
    $form = New-Object System.Windows.Forms.Form
    $form.Text = '卸载零售通'
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object System.Drawing.Size(720, 550)
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $form.BackColor = [System.Drawing.Color]::White

    $header = New-Object System.Windows.Forms.Panel
    $header.Dock = 'Top'
    $header.Height = 112
    $header.BackColor = [System.Drawing.Color]::FromArgb(61, 17, 24)
    $title = New-Object System.Windows.Forms.Label
    $title.Text = '卸载零售通'
    $title.Dock = 'Top'
    $title.Height = 50
    $title.Padding = New-Object System.Windows.Forms.Padding(28, 19, 0, 0)
    $title.ForeColor = [System.Drawing.Color]::White
    $title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 21, [System.Drawing.FontStyle]::Bold)
    $header.Controls.Add($title)

    $subtitle = New-Object System.Windows.Forms.Label
    $subtitle.Text = "当前安装位置：$ResolvedInstall"
    $subtitle.Dock = 'Bottom'
    $subtitle.Height = 42
    $subtitle.Padding = New-Object System.Windows.Forms.Padding(30, 7, 28, 0)
    $subtitle.ForeColor = [System.Drawing.Color]::FromArgb(255, 209, 214)
    $subtitle.AutoEllipsis = $true
    $header.Controls.Add($subtitle)

    $body = New-Object System.Windows.Forms.Panel
    $body.Dock = 'Fill'
    $body.Padding = New-Object System.Windows.Forms.Padding(30, 18, 30, 12)
    $normalRadio = New-Object System.Windows.Forms.RadioButton
    $normalRadio.Text = '普通卸载（推荐）'
    $normalRadio.AutoSize = $true
    $normalRadio.Checked = $true
    $normalRadio.AccessibleName = '普通卸载'
    $normalRadio.Location = New-Object System.Drawing.Point(32, 22)
    $body.Controls.Add($normalRadio)

    $normalHint = New-Object System.Windows.Forms.Label
    $normalHint.Text = '只删除程序、快捷方式和卸载登记；保留历史、设置、日志、原始资料、导入文件和生成文件。'
    $normalHint.AutoSize = $true
    $normalHint.MaximumSize = New-Object System.Drawing.Size(620, 0)
    $normalHint.ForeColor = [System.Drawing.Color]::FromArgb(112, 98, 103)
    $normalHint.Location = New-Object System.Drawing.Point(54, 50)
    $body.Controls.Add($normalHint)

    $completeRadio = New-Object System.Windows.Forms.RadioButton
    $completeRadio.Text = '彻底卸载'
    $completeRadio.AutoSize = $true
    $completeRadio.AccessibleName = '彻底卸载'
    $completeRadio.Location = New-Object System.Drawing.Point(32, 96)
    $body.Controls.Add($completeRadio)

    $completeHint = New-Object System.Windows.Forms.Label
    $completeHint.Text = '在普通卸载基础上，另删除应用配置、历史、日志和缓存。仍需二次确认。'
    $completeHint.AutoSize = $true
    $completeHint.MaximumSize = New-Object System.Drawing.Size(620, 0)
    $completeHint.ForeColor = [System.Drawing.Color]::FromArgb(112, 98, 103)
    $completeHint.Location = New-Object System.Drawing.Point(54, 124)
    $body.Controls.Add($completeHint)

    $scopeTitle = New-Object System.Windows.Forms.Label
    $scopeTitle.Text = '确认页：本次范围'
    $scopeTitle.AutoSize = $true
    $scopeTitle.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10, [System.Drawing.FontStyle]::Bold)
    $scopeTitle.Location = New-Object System.Drawing.Point(32, 180)
    $body.Controls.Add($scopeTitle)

    $scope = New-Object System.Windows.Forms.Label
    $scope.AutoSize = $false
    $scope.Size = New-Object System.Drawing.Size(640, 170)
    $scope.Location = New-Object System.Drawing.Point(32, 210)
    $scope.Padding = New-Object System.Windows.Forms.Padding(14)
    $scope.BackColor = [System.Drawing.Color]::FromArgb(250, 247, 248)
    $scope.ForeColor = [System.Drawing.Color]::FromArgb(61, 46, 51)
    $body.Controls.Add($scope)

    function Update-Scope {
        if ($completeRadio.Checked) {
            $scope.Text = "将清理：程序目录、开始菜单/桌面快捷方式、HKCU 卸载登记、应用配置、历史、日志、缓存，以及写回设置与模板。`r`n`r`n始终保留：原始资料、导入文件、生成文件、Documents 用户文件、写回原件备份与恢复记录、未知业务文件。`r`n`r`n可恢复：删除项尽量移入回收站。"
        } else {
            $scope.Text = "将清理：程序目录、开始菜单/桌面快捷方式、HKCU 卸载登记。`r`n`r`n保留：历史、设置、日志、原始资料、导入文件、生成文件、Documents 用户文件与全部业务数据。`r`n`r`n可恢复：删除项尽量移入回收站。"
        }
    }
    $normalRadio.add_CheckedChanged({ Update-Scope })
    $completeRadio.add_CheckedChanged({ Update-Scope })
    Update-Scope

    $footer = New-Object System.Windows.Forms.FlowLayoutPanel
    $footer.Dock = 'Bottom'
    $footer.Height = 68
    $footer.FlowDirection = 'RightToLeft'
    $footer.WrapContents = $false
    $footer.Padding = New-Object System.Windows.Forms.Padding(24, 14, 24, 12)
    $footer.BackColor = [System.Drawing.Color]::FromArgb(250, 247, 248)
    $continue = New-Object System.Windows.Forms.Button
    $continue.Text = '继续卸载'
    $continue.AutoSize = $true
    $continue.Padding = New-Object System.Windows.Forms.Padding(18, 4, 18, 4)
    $continue.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $continue.BackColor = [System.Drawing.Color]::FromArgb(210, 38, 55)
    $continue.ForeColor = [System.Drawing.Color]::White
    $continue.FlatStyle = 'Flat'
    $footer.Controls.Add($continue)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = '取消'
    $cancel.AutoSize = $true
    $cancel.Padding = New-Object System.Windows.Forms.Padding(18, 4, 18, 4)
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $footer.Controls.Add($cancel)

    $form.AcceptButton = $continue
    $form.CancelButton = $cancel
    # WinForms docks controls in reverse z-order. Add the header last so it
    # remains at the top and the fill/footer regions do not cover it.
    $form.Controls.Add($body)
    $form.Controls.Add($footer)
    $form.Controls.Add($header)
    $result = $form.ShowDialog()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        $form.Dispose()
        return $null
    }
    $selected = if ($completeRadio.Checked) { 'Complete' } else { 'Normal' }
    $form.Dispose()
    return $selected
}

function Show-ResultForm {
    param([string]$ResolvedInstall, [string]$SelectedMode)
    $form = New-Object System.Windows.Forms.Form
    $form.Text = '卸载结果 · 零售通'
    $form.StartPosition = 'CenterScreen'
    $form.ClientSize = New-Object System.Drawing.Size(760, 610)
    $form.MinimumSize = New-Object System.Drawing.Size(760, 610)
    $form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $form.BackColor = [System.Drawing.Color]::White

    $header = New-Object System.Windows.Forms.Panel
    $header.Dock = 'Top'
    $header.Height = 108
    $header.BackColor = [System.Drawing.Color]::FromArgb(61, 17, 24)
    $heading = New-Object System.Windows.Forms.Label
    $heading.Dock = 'Top'
    $heading.Height = 54
    $heading.Padding = New-Object System.Windows.Forms.Padding(28, 20, 0, 0)
    $heading.ForeColor = [System.Drawing.Color]::White
    $heading.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 21, [System.Drawing.FontStyle]::Bold)
    $heading.Text = if ($script:failed.Count -eq 0) { '卸载完成' } else { '卸载部分失败' }
    $header.Controls.Add($heading)

    $sub = New-Object System.Windows.Forms.Label
    $sub.Dock = 'Bottom'
    $sub.Height = 38
    $sub.Padding = New-Object System.Windows.Forms.Padding(30, 5, 28, 0)
    $sub.ForeColor = [System.Drawing.Color]::FromArgb(255, 209, 214)
    $sub.Text = "方式：$(if ($SelectedMode -eq 'Complete') {'彻底卸载'} else {'普通卸载'}) · 安装位置：$ResolvedInstall"
    $sub.AutoEllipsis = $true
    $header.Controls.Add($sub)

    $summary = New-Object System.Windows.Forms.TextBox
    $summary.Multiline = $true
    $summary.ReadOnly = $true
    $summary.ScrollBars = 'Vertical'
    $summary.Dock = 'Fill'
    $summary.Margin = New-Object System.Windows.Forms.Padding(26, 18, 26, 12)
    $summary.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $summary.BackColor = [System.Drawing.Color]::FromArgb(250, 247, 248)
    $summary.Text = @"
卸载结果：$(if ($script:failed.Count -eq 0) {'成功'} else {'部分失败，需要查看失败项'})

已删除：
$(if ($script:deleted.Count) { ($script:deleted | ForEach-Object { "  - $_" }) -join "`r`n" } else { '  - 无' })

已保留：
$(if ($script:retained.Count) { ($script:retained | ForEach-Object { "  - $_" }) -join "`r`n" } else { '  - 无' })

失败项：
$(if ($script:failed.Count) { ($script:failed | ForEach-Object { "  - $_" }) -join "`r`n" } else { '  - 无' })

日志位置：
$logPath
"@
    $footer = New-Object System.Windows.Forms.FlowLayoutPanel
    $footer.Dock = 'Bottom'
    $footer.Height = 68
    $footer.FlowDirection = 'RightToLeft'
    $footer.WrapContents = $false
    $footer.Padding = New-Object System.Windows.Forms.Padding(24, 14, 24, 12)
    $footer.BackColor = [System.Drawing.Color]::FromArgb(250, 247, 248)
    $form.Controls.Add($footer)

    $close = New-Object System.Windows.Forms.Button
    $close.Text = '关闭'
    $close.AutoSize = $true
    $close.Padding = New-Object System.Windows.Forms.Padding(18, 4, 18, 4)
    $close.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $footer.Controls.Add($close)

    $openLog = New-Object System.Windows.Forms.Button
    $openLog.Text = '打开日志位置'
    $openLog.AutoSize = $true
    $openLog.Padding = New-Object System.Windows.Forms.Padding(16, 4, 16, 4)
    $openLog.Add_Click({
        try {
            if (Test-Path -LiteralPath $logPath) {
                Start-Process -FilePath notepad.exe -ArgumentList ('"' + $logPath + '"')
            } elseif (Test-Path -LiteralPath $uninstallRecordDirectory) {
                Start-Process -FilePath explorer.exe -ArgumentList ('"' + $uninstallRecordDirectory + '"')
            }
        } catch {}
    })
    $footer.Controls.Add($openLog)
    $form.AcceptButton = $close
    $form.Controls.Add($summary)
    $form.Controls.Add($footer)
    $form.Controls.Add($header)
    [void]$form.ShowDialog()
    $form.Dispose()
}

$resolvedInstall = Get-RegisteredInstallDirectory
if ([string]::IsNullOrWhiteSpace([string]$resolvedInstall)) {
    [System.Windows.Forms.MessageBox]::Show(
        "没有找到零售通的安装位置。`n`n请从 Windows 设置的应用列表重新运行卸载，或重新安装后再卸载。",
        '无法卸载零售通',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

if (Test-ProtectedPath -Path $resolvedInstall) {
    [System.Windows.Forms.MessageBox]::Show(
        "安装位置指向受保护目录，已停止卸载：$resolvedInstall",
        '卸载未开始',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

if (-not (Test-Path -LiteralPath $resolvedInstall -PathType Container)) {
    Add-Result 'Failed' "安装目录不存在：$resolvedInstall"
}

if ($Mode -eq 'Prompt' -and -not $Yes) {
    $selected = Show-ScopeForm -ResolvedInstall $resolvedInstall
    if ($null -eq $selected) { exit 0 }
    $script:selectedMode = $selected
} elseif ($Mode -eq 'Prompt') {
    $script:selectedMode = 'Normal'
}

if ($script:selectedMode -eq 'Complete' -and -not $Yes) {
    $confirm = [System.Windows.Forms.MessageBox]::Show(
        "二次确认：彻底卸载将删除应用配置、历史、日志和缓存。`n`n原始资料、导入文件、生成文件、Documents 用户文件、写回备份与恢复记录会始终保留。`n`n是否继续彻底卸载？",
        '确认彻底卸载',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { exit 0 }
}

Write-UninstallRecord "开始卸载：$resolvedInstall，方式：$($script:selectedMode)，时间：$timestamp"
if (-not (Stop-ApplicationForUninstall)) { exit 0 }

Move-ToRecoverableLocation -Path $startMenuShortcut -Label '开始菜单快捷方式'
Move-ToRecoverableLocation -Path $desktopShortcut -Label '桌面快捷方式'

if (Test-Path -LiteralPath $uninstallKey) {
    try {
        Remove-Item -LiteralPath $uninstallKey -Recurse -Force
        Add-Result 'Deleted' 'HKCU 卸载登记'
        Write-UninstallRecord '已删除 HKCU 卸载登记'
    } catch {
        Add-Result 'Failed' "HKCU 卸载登记：$($_.Exception.Message)"
    }
}

if ($script:selectedMode -eq 'Complete' -and (Test-Path -LiteralPath $applicationData -PathType Container)) {
    # Exact allowlist. Never select the operations/writeback parent directories.
    $completeTargets = @(
        'preferences.json',
        'history',
        'history_deleted',
        'import_sessions',
        'operation.sqlite3',
        'operation.sqlite3-wal',
        'operation.sqlite3-shm',
        '操作日志.jsonl',
        'templates.json',
        'WebView2',
        '缓存',
        '日志',
        'logs',
        'operations\state.json',
        'writeback\settings.json',
        'writeback\templates.json'
    )
    foreach ($relative in $completeTargets) {
        $target = Join-Path $applicationData $relative
        if (Test-Path -LiteralPath $target) {
            $isDirectory = Test-Path -LiteralPath $target -PathType Container
            Move-ToRecoverableLocation -Path $target -Label "应用数据：$relative" -Directory:$isDirectory
        }
    }
    Add-Result 'Retained' '原始资料、导入文件、生成文件、Documents 用户文件、写回原件备份与恢复记录'
    $remaining = @(Get-ChildItem -LiteralPath $applicationData -Force -ErrorAction SilentlyContinue)
    if ($remaining.Count -gt 0) {
        $names = ($remaining | Select-Object -First 20 | ForEach-Object { $_.Name }) -join '、'
        Add-Result 'Retained' "应用数据中未列入清理白名单的内容：$names"
    }
} else {
    Add-Result 'Retained' '历史、设置、日志、原始资料、导入文件和生成文件'
}

if (Test-Path -LiteralPath $resolvedInstall -PathType Container) {
    Move-ToRecoverableLocation -Path $resolvedInstall -Label '程序目录' -Directory
}

foreach ($item in $script:retained) { Write-UninstallRecord "已保留：$item" }
foreach ($item in $script:failed) { Write-UninstallRecord "失败项：$item" }
Write-UninstallRecord "结束：已删除 $($script:deleted.Count) 项，已保留 $($script:retained.Count) 项，失败 $($script:failed.Count) 项"

if (-not $Yes) {
    Show-ResultForm -ResolvedInstall $resolvedInstall -SelectedMode $script:selectedMode
}

if ($script:failed.Count -gt 0) {
    exit 1
}
exit 0
