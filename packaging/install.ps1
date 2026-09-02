<#
    CRWALLM 설치.

        powershell -ExecutionPolicy Bypass -File packaging\install.ps1
        powershell -ExecutionPolicy Bypass -File packaging\install.ps1 -Uninstall

    관리자 권한이 필요 없습니다. 프로그램은 사용자 폴더
    (%LOCALAPPDATA%\Programs\CRWALLM) 에 들어가고, 레지스트리도 HKCU만
    건드립니다. Windows의 "앱 및 기능"에 등록되므로 거기서 지울 수도
    있습니다.

    이 파일은 BOM이 있는 UTF-8이어야 합니다. Windows PowerShell 5.1은
    BOM이 없으면 .ps1을 시스템 코드페이지로 읽고, 그러면 아래 한글이 전부
    깨집니다 - .bat이 같은 이유로 ASCII 전용인 것과 같은 함정입니다.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = 'Stop'

$AppName   = 'CRWALLM'
$Source    = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\CRWALLM'
$Target    = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$Exe       = Join-Path $Target 'CRWALLM.exe'
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
$Desktop   = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
$RegKey    = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"

function Stop-Running {
    $running = Get-Process -Name 'CRWALLM' -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host '  실행 중인 창을 닫습니다...'
        $running | Stop-Process -Force
        Start-Sleep -Milliseconds 700
    }
}

function New-Shortcut([string]$Path, [string]$TargetExe) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $TargetExe
    $link.WorkingDirectory = Split-Path $TargetExe -Parent
    $link.IconLocation = "$TargetExe,0"
    $link.Description = '웹페이지에서 표를 모아 엑셀로 저장합니다'
    $link.Save()
}

if ($Uninstall) {
    Write-Host ''
    Write-Host "$AppName 을(를) 제거합니다."
    Stop-Running
    foreach ($path in @($StartMenu, $Desktop)) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "  바로 가기 삭제  $path" }
    }
    if (Test-Path $RegKey) { Remove-Item $RegKey -Recurse -Force; Write-Host '  등록 정보 삭제' }
    if (Test-Path $Target) { Remove-Item $Target -Recurse -Force; Write-Host "  파일 삭제  $Target" }
    Write-Host ''
    Write-Host '제거했습니다.'
    exit 0
}

if (-not (Test-Path (Join-Path $Source 'CRWALLM.exe'))) {
    Write-Host ''
    Write-Host "빌드된 프로그램이 없습니다: $Source"
    Write-Host '먼저 빌드하세요:'
    Write-Host '  uv run python packaging\build.py'
    Write-Host ''
    exit 1
}

Write-Host ''
Write-Host "$AppName 을(를) 설치합니다."
Stop-Running

# 통째로 지우고 다시 넣습니다. 남은 파일 위에 덮어쓰면 이전 버전에만 있던
# 모듈이 살아남아, 다음 실행에서 있지도 않은 코드를 부르게 됩니다.
if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
New-Item -ItemType Directory -Path $Target -Force | Out-Null
Copy-Item (Join-Path $Source '*') $Target -Recurse -Force
Write-Host "  복사  $Target"

# 제거 스크립트를 설치 폴더 안으로 복사합니다. 저장소를 가리키게 두면,
# 저장소를 지운 사람은 "앱 및 기능"에 지울 수 없는 항목을 남기게 됩니다.
$Uninstaller = Join-Path $Target 'uninstall.ps1'
Copy-Item $PSCommandPath $Uninstaller -Force

New-Shortcut -Path $StartMenu -TargetExe $Exe
Write-Host '  시작 메뉴에 추가'

if (-not $NoDesktopShortcut) {
    New-Shortcut -Path $Desktop -TargetExe $Exe
    Write-Host '  바탕화면에 추가'
}

$size = [math]::Round(((Get-ChildItem $Target -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 0)
New-Item -Path $RegKey -Force | Out-Null
Set-ItemProperty $RegKey 'DisplayName'     $AppName
Set-ItemProperty $RegKey 'DisplayIcon'     $Exe
Set-ItemProperty $RegKey 'InstallLocation' $Target
Set-ItemProperty $RegKey 'Publisher'       'CRWALLM'
Set-ItemProperty $RegKey 'NoModify'        1 -Type DWord
Set-ItemProperty $RegKey 'NoRepair'        1 -Type DWord
Set-ItemProperty $RegKey 'EstimatedSize'   ($size * 1024) -Type DWord
Set-ItemProperty $RegKey 'UninstallString' `
    "powershell -ExecutionPolicy Bypass -File `"$Uninstaller`" -Uninstall"
Write-Host '  앱 및 기능에 등록'

Write-Host ''
Write-Host "설치했습니다. 시작 메뉴나 바탕화면의 $AppName 아이콘을 누르세요."
Write-Host ''
