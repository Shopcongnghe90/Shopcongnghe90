# claude-remote-control.ps1 — vòng lặp giữ `claude remote-control` luôn chạy (tác vụ theo lịch, khi đăng nhập).
# Token lấy từ biến môi trường người dùng CLAUDE_CODE_OAUTH_TOKEN (đặt bằng dat-token.ps1).
# Nhật ký: C:\agent\logs\remote-control-YYYY-MM-DD.log
$Agent = 'C:\agent'
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path;$env:ProgramFiles\Git\cmd"
$Ten = if ($env:AGENT_TEN) { $env:AGENT_TEN } else { $env:COMPUTERNAME.ToLower() }
Set-Location "$Agent\work"
while ($true) {
  $log = "$Agent\logs\remote-control-$(Get-Date -Format yyyy-MM-dd).log"
  $token = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN','User')
  if (-not $token) {
    "$(Get-Date -Format s) Thiếu CLAUDE_CODE_OAUTH_TOKEN — chạy C:\agent\dat-token.ps1" | Out-File $log -Append
    Start-Sleep -Seconds 60; continue
  }
  $env:CLAUDE_CODE_OAUTH_TOKEN = $token
  if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    "$(Get-Date -Format s) Chưa có claude trong PATH — cài: irm https://claude.ai/install.ps1 | iex" | Out-File $log -Append
    Start-Sleep -Seconds 120; continue
  }
  "$(Get-Date -Format s) Khởi động claude remote-control --name $Ten" | Out-File $log -Append
  & claude remote-control --name $Ten 2>&1 | Out-File $log -Append
  "$(Get-Date -Format s) claude thoát (mã $LASTEXITCODE) — chạy lại sau 20 giây" | Out-File $log -Append
  Start-Sleep -Seconds 20
}
