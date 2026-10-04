# dat-token.ps1 — đặt token Claude cho user agent và khởi động lại tác vụ remote-control.
#   powershell -ExecutionPolicy Bypass -File C:\agent\dat-token.ps1            (nhập token khi được hỏi)
#   powershell -ExecutionPolicy Bypass -File C:\agent\dat-token.ps1 -Token sk-ant-oat01-...
param([string]$Token, [string]$Ten)
if (-not $Token) { $sec = Read-Host -AsSecureString 'Dán token (claude setup-token)'; $Token = [Net.NetworkCredential]::new('', $sec).Password }
if (-not $Token) { Write-Error 'Không có token'; exit 1 }
[Environment]::SetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN', $Token, 'User')
if ($Ten) { [Environment]::SetEnvironmentVariable('AGENT_TEN', $Ten, 'User') }
Write-Host 'Đã lưu token vào biến môi trường người dùng.'
try { Stop-ScheduledTask -TaskName ClaudeRemoteControl -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName ClaudeRemoteControl; Write-Host 'Đã khởi động lại tác vụ ClaudeRemoteControl.' }
catch { Write-Warning "Không khởi động được tác vụ: $_ — đăng xuất/đăng nhập lại." }
Write-Host 'Xem nhật ký: Get-Content C:\agent\logs\remote-control-*.log -Tail 30 -Wait'
