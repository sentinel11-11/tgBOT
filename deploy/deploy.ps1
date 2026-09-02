<#
.SYNOPSIS
    Загрузка бота на Linux-сервер и установка его как systemd-сервиса.

.DESCRIPTION
    Скрипт упаковывает проект, копирует архив на сервер по SSH и запускает там
    deploy/install.sh. Секреты (.env, config.yaml, credentials.json) передаются
    по тому же зашифрованному каналу и остаются только на сервере.

    Повторный запуск = обновление: код заменяется, настройки и база сохраняются.

.EXAMPLE
    .\deploy\deploy.ps1 -Server root@45.131.186.67 -Port 48390

.EXAMPLE
    .\deploy\deploy.ps1 -NoSecrets      # обновить только код, настройки не трогать
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,

    [int]$Port = 22,

    [string]$AppName = "axiom-bot",

    [switch]$NoSecrets
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "=== Деплой $AppName на $Server (порт $Port) ===" -ForegroundColor Cyan

foreach ($tool in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Не найдена утилита '$tool'. Установите OpenSSH: Параметры -> Приложения -> " +
              "Дополнительные компоненты -> Клиент OpenSSH."
    }
}

Push-Location $projectRoot
try {
    if (-not (Test-Path "bot")) { throw "Папка bot/ не найдена — запускайте из корня проекта." }

    $items = @("bot", "tools", "deploy", "requirements.txt", "config.example.yaml")
    if (Test-Path "README.md") { $items += "README.md" }

    if ($NoSecrets) {
        Write-Host "Секреты не передаются (-NoSecrets): на сервере останутся текущие." -ForegroundColor Yellow
    }
    else {
        foreach ($secret in @(".env", "config.yaml", "credentials.json")) {
            if (Test-Path $secret) { $items += $secret }
            else { Write-Host "  нет локального $secret — пропускаю" -ForegroundColor Yellow }
        }
        if ($items -notcontains ".env") {
            Write-Host "Внимание: .env не найден. Сначала выполните: py tools\configure.py" -ForegroundColor Yellow
        }
    }

    $archive = Join-Path $env:TEMP "$AppName-deploy.tar.gz"
    if (Test-Path $archive) { Remove-Item $archive -Force }

    Write-Host "`n[1/3] Упаковываю: $($items -join ', ')"
    tar -czf $archive --exclude="__pycache__" --exclude="*.pyc" $items
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать архив" }

    $sizeKb = [math]::Round((Get-Item $archive).Length / 1KB)
    Write-Host "      архив готов: $sizeKb КБ"

    Write-Host "`n[2/3] Копирую на сервер (потребуется пароль или ключ)"
    scp -P $Port $archive "${Server}:/tmp/$AppName-deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "scp завершился с ошибкой" }

    Write-Host "`n[3/3] Устанавливаю на сервере"
    $remoteScript = @"
set -e
rm -rf /tmp/$AppName-src
mkdir -p /tmp/$AppName-src
tar -xzf /tmp/$AppName-deploy.tar.gz -C /tmp/$AppName-src
cd /tmp/$AppName-src
APP_NAME=$AppName bash deploy/install.sh
rm -rf /tmp/$AppName-src /tmp/$AppName-deploy.tar.gz
"@
    $remoteScript = $remoteScript -replace "`r`n", "`n"
    $remoteScript | ssh -p $Port $Server "bash -s"
    if ($LASTEXITCODE -ne 0) { throw "Установка на сервере завершилась с ошибкой" }

    Remove-Item $archive -Force

    Write-Host "`n=== Готово ===" -ForegroundColor Green
    Write-Host "Логи:      ssh -p $Port $Server 'journalctl -u $AppName -f'"
    Write-Host "Статус:    ssh -p $Port $Server 'systemctl status $AppName'"
    Write-Host "Проверка:  ssh -p $Port $Server 'cd /opt/$AppName && .venv/bin/python tools/preflight.py'"
}
finally {
    Pop-Location
}
