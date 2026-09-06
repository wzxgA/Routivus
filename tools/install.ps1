# ============================================================================
# XG-CLI 原生安装器（Windows PowerShell）
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# 效果: 把 xg-cli 装进用户级虚拟环境 ~\.xg-cli\venv，
#       并自动把其 Scripts 目录加入「用户 PATH」（永久生效）。
#       完成后重开终端即可直接执行: xg-cli
#
# 与 Claude Code 类似的思路: 安装器负责把命令所在目录放进 PATH，
# 这样用户 pip 装完就能直接用，不用手动改系统 PATH。
# ============================================================================

param()

$ErrorActionPreference = "Stop"

$Package     = "xg-cli"
$Root        = if ($env:XG_CLI_HOME) { $env:XG_CLI_HOME } else { Join-Path $HOME ".xg-cli" }
$Venv        = Join-Path $Root "venv"
$VenvScripts = Join-Path $Venv "Scripts"
$VenvPython  = Join-Path $VenvScripts "python.exe"
$VenvPip     = Join-Path $VenvScripts "pip.exe"
$Exe         = Join-Path $VenvScripts "xg-cli.exe"

Write-Host "==> XG-CLI 安装器" -ForegroundColor Cyan

# ---- 1) 定位 python 3.11+ ----
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "!! 未找到 python。请先安装 Python 3.11+（安装时勾选 Add to PATH），再重跑本脚本。" -ForegroundColor Yellow
    exit 1
}
$pyVer = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0) { throw "无法读取 Python 版本" }
Write-Host "==> 使用 Python $pyVer ($($py.Source))"

# ---- 2) 创建虚拟环境（幂等） ----
if (-not (Test-Path $VenvPython)) {
    Write-Host "==> 创建虚拟环境 $Venv"
    & python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败" }
}

# ---- 3) 安装 / 升级 xg-cli ----
$mode = if (Test-Path $Exe) { "upgrade" } else { "install" }
Write-Host "==> $mode $Package ..."
& $VenvPip install --upgrade $Package
if ($LASTEXITCODE -ne 0) { throw "安装失败: pip install $Package" }
if (-not (Test-Path $Exe)) { throw "安装完成但找不到 xg-cli.exe，请检查输出" }

# ---- 4) 把 Scripts 目录加入用户 PATH（持久化，缺失才写） ----
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ([string]::IsNullOrEmpty($userPath)) { $userPath = "" }
$entries = $userPath.Split(';') | Where-Object { $_ -ne "" }

if ($entries -contains $VenvScripts) {
    Write-Host "==> PATH 已包含 $VenvScripts"
} else {
    $newPath = ($userPath.TrimEnd(';') + ";" + $VenvScripts)
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "==> 已把 $VenvScripts 加入用户 PATH（永久生效）" -ForegroundColor Green
}

# 当前会话即时可用（新开终端用持久化 PATH）
$env:Path = $VenvScripts + ";" + $env:Path

# ---- 5) 校验 ----
Write-Host "==> 版本: " -NoNewline
& $Exe --version
if ($LASTEXITCODE -ne 0) { Write-Host "（版本读取失败，请检查安装）" -ForegroundColor Yellow }

Write-Host ""
Write-Host "安装完成。请新开一个终端，然后直接运行: xg-cli" -ForegroundColor Green