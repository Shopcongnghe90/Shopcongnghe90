# setup-agent.ps1 — chạy MỘT LẦN ở lần đăng nhập đầu (FirstLogonCommands) trên máy Windows Zalo.
# Cài: Git (Claude Code trên Windows cần Git Bash), Google Chrome, Zalo PC, Claude Code;
# tạo C:\agent\work, tác vụ theo lịch chạy claude remote-control khi đăng nhập.
# Ghi nhật ký: C:\agent\setup.log. Chạy lại được (idempotent ở mức hợp lý).
$ErrorActionPreference = 'Continue'
$Agent = 'C:\agent'
New-Item -ItemType Directory -Force -Path "$Agent\work\ghi-chu","$Agent\work\kich-ban","$Agent\logs" | Out-Null
Start-Transcript -Path "$Agent\setup.log" -Append | Out-Null
Write-Host "== setup-agent.ps1 bắt đầu $(Get-Date) trên $env:COMPUTERNAME =="

# Chép các script đi kèm từ ISO vào C:\agent để dùng lâu dài
$Nguon = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($f in 'claude-remote-control.ps1','dat-token.ps1','CLAUDE.md') {
  if (Test-Path "$Nguon\$f") {
    $dich = if ($f -eq 'CLAUDE.md') { "$Agent\work\$f" } else { "$Agent\$f" }
    Copy-Item "$Nguon\$f" $dich -Force
  }
}

# --- Chờ winget sẵn sàng (App Installer đăng ký sau lần đăng nhập đầu, có thể mất vài phút) ---
function Cho-Winget {
  for ($i = 0; $i -lt 30; $i++) {
    if (Get-Command winget -ErrorAction SilentlyContinue) { return $true }
    Start-Sleep -Seconds 10
  }
  return $false
}
$CoWinget = Cho-Winget
if (-not $CoWinget) { Write-Warning "winget không sẵn sàng — dùng link tải trực tiếp." }

function Cai-Winget([string]$Id) {
  if (-not $CoWinget) { return $false }
  Write-Host "winget install $Id"
  $p = Start-Process winget -ArgumentList "install --id $Id -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity" -Wait -PassThru -NoNewWindow
  return ($p.ExitCode -eq 0 -or $p.ExitCode -eq -1978335189)  # 0 = OK, -1978335189 = đã cài
}
function Tai-Va-Cai([string]$Url, [string]$Tep, [string]$ThamSo) {
  $d = "$env:TEMP\$Tep"
  Write-Host "Tải $Url"
  try { Invoke-WebRequest -Uri $Url -OutFile $d -UseBasicParsing -MaximumRedirection 10 } catch { Write-Warning "Tải thất bại: $_"; return }
  Start-Process $d -ArgumentList $ThamSo -Wait
}

# --- Git for Windows (bắt buộc cho Claude Code) ---
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  if (-not (Cai-Winget 'Git.Git')) {
    Tai-Va-Cai 'https://github.com/git-for-windows/git/releases/latest/download/Git-64-bit.exe' 'git.exe' '/VERYSILENT /NORESTART'
  }
}

# --- Google Chrome ---
if (-not (Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe")) {
  if (-not (Cai-Winget 'Google.Chrome')) {
    Tai-Va-Cai 'https://dl.google.com/chrome/install/latest/chrome_installer.exe' 'chrome.exe' '/silent /install'
  }
}

# --- Zalo PC ---
# winget id: VNGCorp.Zalo (kiểm tra bằng `winget search Zalo` nếu đổi). Dự phòng: link tải chính thức (redirect tới bản mới nhất).
if (-not (Test-Path "$env:LOCALAPPDATA\Programs\Zalo\Zalo.exe")) {
  if (-not (Cai-Winget 'VNGCorp.Zalo')) {
    Tai-Va-Cai 'https://zalo.me/download/zalo-pc?utm=90000' 'ZaloSetup.exe' '/S'
  }
}

# --- Claude Code (bộ cài gốc cho Windows) ---
try {
  Write-Host "Cài Claude Code"
  Invoke-Expression (Invoke-RestMethod -Uri 'https://claude.ai/install.ps1')
} catch { Write-Warning "Cài Claude Code thất bại: $_ (chạy lại tay: irm https://claude.ai/install.ps1 | iex)" }
# Thêm đường dẫn claude vào PATH người dùng (bộ cài để ở %USERPROFILE%\.local\bin)
$binClaude = "$env:USERPROFILE\.local\bin"
$pathUser = [Environment]::GetEnvironmentVariable('Path','User')
if ($pathUser -notlike "*$binClaude*") { [Environment]::SetEnvironmentVariable('Path', "$pathUser;$binClaude", 'User') }

# --- Chrome với cổng gỡ lỗi 9222 khi đăng nhập (để Claude điều khiển qua chrome-devtools-mcp nếu cần) ---
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
if (Test-Path $chrome) {
  $lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\chrome-agent.lnk"
  $ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut($lnk)
  $s.TargetPath = $chrome
  $s.Arguments = "--remote-debugging-port=9222 --user-data-dir=$Agent\chrome-profile --no-first-run --no-default-browser-check --lang=vi about:blank"
  $s.Save()
}

# --- Tác vụ theo lịch: Claude Code remote-control khi đăng nhập user agent ---
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $Agent\claude-remote-control.ps1"
$trg = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\agent"
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName 'ClaudeRemoteControl' -Action $act -Trigger $trg -Settings $set -User "$env:COMPUTERNAME\agent" -RunLevel Limited -Force | Out-Null
Write-Host "Đã đăng ký tác vụ ClaudeRemoteControl (chạy khi đăng nhập; cần token: C:\agent\dat-token.ps1)"

# --- Thông tin mạng để quản trị RDP ---
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' } | Format-Table IPAddress,InterfaceAlias -AutoSize | Out-String | Write-Host
Write-Host "== setup-agent.ps1 xong $(Get-Date). Còn tay: đăng nhập Zalo (QR), dat-token.ps1, kích hoạt Windows =="
Stop-Transcript | Out-Null
