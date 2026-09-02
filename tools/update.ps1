<#
.SYNOPSIS
    Обновление проекта до последней версии ветки на GitHub.

.DESCRIPTION
    Скачивает свежий код и кладёт его поверх текущей папки.
    ЛОКАЛЬНЫЕ НАСТРОЙКИ НЕ ТРОГАЮТСЯ: .env, config.yaml, credentials.json,
    база data\ и логи logs\ остаются на месте — их нет в репозитории.

    Если папка была склонирована через git, скрипт просто сделает git pull.
    Если это распакованный ZIP (папка вида tgBOT-arena-...), скачает
    свежий ZIP ветки и аккуратно заменит код.

.EXAMPLE
    .\tools\update.ps1

.EXAMPLE
    .\tools\update.ps1 -SkipDeps        # не переустанавливать зависимости
#>
[CmdletBinding()]
param(
    [string]$Repo = "sentinel11-11/tgBOT",
    [string]$Branch = "arena/01a0625e-tgbot",
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "=== Обновление проекта ===" -ForegroundColor Cyan
Write-Host "  репозиторий: $Repo"
Write-Host "  ветка:       $Branch"
Write-Host "  папка:       $projectRoot"

Push-Location $projectRoot
try {
    $updatedViaGit = $false

    # --- Вариант 1: обычный git-репозиторий --------------------------------
    if ((Test-Path (Join-Path $projectRoot ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "`n[1/2] Забираю изменения через git" -ForegroundColor Cyan
        git fetch origin $Branch
        if ($LASTEXITCODE -ne 0) { throw "git fetch завершился с ошибкой" }
        git checkout $Branch
        git pull --ff-only origin $Branch
        if ($LASTEXITCODE -ne 0) {
            throw "git pull не смог обновиться без конфликтов. Сохраните свои правки и повторите."
        }
        $updatedViaGit = $true
    }

    # --- Вариант 2: распакованный ZIP --------------------------------------
    if (-not $updatedViaGit) {
        Write-Host "`n[1/2] Скачиваю свежий ZIP ветки" -ForegroundColor Cyan

        $zipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
        $tempDir = Join-Path $env:TEMP ("tgbot-update-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tempDir | Out-Null
        $zipPath = Join-Path $tempDir "source.zip"

        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
        }
        catch {
            throw "Не удалось скачать $zipUrl`n$($_.Exception.Message)"
        }

        Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force
        $srcDir = Get-ChildItem -Path $tempDir -Directory | Select-Object -First 1
        if (-not $srcDir) { throw "В архиве не оказалось папки с кодом" }

        # Старый код удаляем целиком, чтобы не оставались файлы,
        # удалённые в новых версиях. Секреты и данные лежат вне этих папок.
        foreach ($dir in @("bot", "deploy", "tests")) {
            $path = Join-Path $projectRoot $dir
            if (Test-Path $path) { Remove-Item $path -Recurse -Force }
        }

        Copy-Item -Path (Join-Path $srcDir.FullName "*") -Destination $projectRoot -Recurse -Force

        # В tools\ подчищаем файлы, которых больше нет в репозитории
        $srcTools = Join-Path $srcDir.FullName "tools"
        if (Test-Path $srcTools) {
            Get-ChildItem (Join-Path $projectRoot "tools") -File | ForEach-Object {
                if (-not (Test-Path (Join-Path $srcTools $_.Name))) {
                    Remove-Item $_.FullName -Force
                }
            }
        }

        Remove-Item $tempDir -Recurse -Force
        Write-Host "      код обновлён"
    }

    # --- Зависимости --------------------------------------------------------
    if ($SkipDeps) {
        Write-Host "`n[2/2] Зависимости пропущены (-SkipDeps)" -ForegroundColor Yellow
    }
    else {
        Write-Host "`n[2/2] Обновляю зависимости" -ForegroundColor Cyan
        # Именно "py -m pip": pip.exe из .venv нередко блокируется
        # политикой безопасности Windows (Smart App Control / AppLocker).
        py -m pip install --user --quiet --upgrade -r (Join-Path $projectRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            Write-Host "      не удалось поставить зависимости — попробуйте вручную:" -ForegroundColor Yellow
            Write-Host "      py -m pip install --user -r requirements.txt" -ForegroundColor Yellow
        }
    }

    # --- Что осталось нетронутым --------------------------------------------
    Write-Host "`nЛокальные файлы сохранены:" -ForegroundColor Green
    foreach ($file in @(".env", "config.yaml", "credentials.json", "data", "logs")) {
        $path = Join-Path $projectRoot $file
        if (Test-Path $path) { Write-Host "  есть   $file" }
        else { Write-Host "  нет    $file" -ForegroundColor DarkGray }
    }

    # --- какая версия теперь стоит -----------------------------------------
    try {
        $ProgressPreference = "SilentlyContinue"
        $commit = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/$Branch" `
            -Headers @{ "User-Agent" = "tgbot-updater" } -UseBasicParsing
        $shortSha = $commit.sha.Substring(0, 7)
        $when = ([datetime]$commit.commit.author.date).ToLocalTime().ToString("dd.MM.yyyy HH:mm")
        $subject = ($commit.commit.message -split "`n")[0]
        Write-Host "`nВерсия: $shortSha от $when" -ForegroundColor Cyan
        Write-Host "        $subject"
    }
    catch {
        Write-Host "`n(не удалось узнать номер версии — не критично)" -ForegroundColor DarkGray
    }

    Write-Host "`n=== Готово ===" -ForegroundColor Green
    Write-Host "Дальше:"
    Write-Host "  py tools\preflight.py     # проверить настройки"
    Write-Host "  py -m bot                 # запустить бота"
}
finally {
    Pop-Location
}
