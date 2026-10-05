<#
.SYNOPSIS
    Установка Сундука на Windows (Docker Desktop) одной командой.

.DESCRIPTION
    Поднимает `docker-compose.portable.yml` и делает всё, что обычно приходится
    делать руками: проверяет Docker, создаёт папку для HOST_ROOT, загружает
    образ из sunduk-*.tar, если он лежит рядом с репозиторием, иначе собирает
    образ из server/ или берёт готовый из GHCR, запускает стек и печатает
    адреса панели и внешних ссылок.

    Файловая система сервера (или выбранная папка) подключается в /host, а
    каталоги для показа выбираются уже в панели — править файлы не нужно.

.EXAMPLE
    .\install.ps1 -HostRoot 'C:\Share'

.EXAMPLE
    .\install.ps1 -HostRoot 'D:\Media' -PublicAddress '192.168.1.50'

.EXAMPLE
    # Второй экземпляр рядом с основным, без конфликта имён и тома.
    .\install.ps1 -HostRoot 'C:\Share2' -AdminPort 9080 -PublicPort 9081 `
        -ContainerName sunduk2 -DataVolume sunduk_data2 -ProjectName sunduk2
#>
[CmdletBinding()]
param(
    # Каталог репозитория (там, где лежат server/ и config/).
    [string] $RepoPath      = (Split-Path -Parent $PSScriptRoot),
    # Что панель показывает как файловую систему: папка или диск.
    [string] $HostRoot      = 'C:\Share',
    [int]    $AdminPort     = 8080,
    [int]    $PublicPort    = 8081,
    [string] $PublicAddress = 'localhost',
    # Пусто — берётся локальный sunduk:latest, иначе образ из GHCR.
    [string] $Image         = '',
    [string] $ContainerName = 'sunduk',
    [string] $DataVolume    = 'sunduk_data',
    [string] $ProjectName   = 'sunduk',
    # Пусто — docker-compose.portable.yml рядом с этим скриптом.
    [string] $ComposeFile   = ''
)

$ErrorActionPreference = 'Stop'

# Русский текст в консоли и в перенаправленном выводе — в одной кодировке.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Step([string] $text) { Write-Host "==> $text" -ForegroundColor Cyan }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Не найден docker. Установите Docker Desktop и повторите.'
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker не запущен (docker info не отвечает). Запустите Docker Desktop.'
}

if (-not $ComposeFile) {
    $ComposeFile = Join-Path $PSScriptRoot 'docker-compose.portable.yml'
}
if (-not (Test-Path -LiteralPath $ComposeFile)) {
    throw "Не найден файл стека: $ComposeFile"
}

# Папка, которую показывает панель: создаём, если её ещё нет (диск C:\ не трогаем).
if ($HostRoot -match '^[A-Za-z]:\\' -and -not (Test-Path -LiteralPath $HostRoot)) {
    Write-Step "создаю папку $HostRoot"
    New-Item -ItemType Directory -Path $HostRoot | Out-Null
}

# 1. Образ: сначала готовый архив (в корне репозитория или рядом со скриптом),
#    потом локальный sunduk:latest, потом сборка из server/, потом GHCR.
$tar = @($RepoPath, $PSScriptRoot) |
    ForEach-Object { Get-ChildItem -Path $_ -Filter 'sunduk-*.tar' -ErrorAction SilentlyContinue } |
    Select-Object -First 1

if ($tar) {
    Write-Step "загружаю образ из $($tar.Name)"
    docker load -i $tar.FullName
    if ($LASTEXITCODE -ne 0) { throw 'docker load не смог загрузить образ.' }
    if (-not $Image) { $Image = 'sunduk:latest' }
}

if (-not $Image) {
    $local = (docker image inspect sunduk:latest 2>$null)
    if ($LASTEXITCODE -eq 0) {
        Write-Step 'использую локальный образ sunduk:latest'
        $Image = 'sunduk:latest'
    } elseif (Test-Path -LiteralPath (Join-Path $RepoPath 'server\Dockerfile')) {
        Write-Step 'собираю образ из исходников (см. docker build)'
        docker build -t sunduk:latest (Join-Path $RepoPath 'server')
        if ($LASTEXITCODE -ne 0) { throw 'Сборка образа не удалась.' }
        $Image = 'sunduk:latest'
    } else {
        Write-Step 'беру готовый образ из GHCR (нужен docker login ghcr.io, если пакет приватный)'
        $Image = 'ghcr.io/afanagr/sunduk:latest'
    }
}

# 2. Переменные стека: их читает и compose, и сам Сундук.
$env:SUNDUK_IMAGE    = $Image
$env:CONTAINER_NAME  = $ContainerName
$env:DATA_VOLUME     = $DataVolume
$env:ADMIN_PORT      = "$AdminPort"
$env:PUBLIC_PORT     = "$PublicPort"
$env:PUBLIC_ADDRESS  = $PublicAddress
$env:HOST_ROOT       = $HostRoot
$env:COMPOSE_PROJECT_NAME = $ProjectName

Write-Step "запускаю стек ($ComposeFile)"
docker compose -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { throw 'docker compose up завершился с ошибкой.' }

# 3. Ждём первый ответ панели — так видно, что порты и том на месте.
Write-Step 'жду ответа панели'
$ready = $false
foreach ($attempt in 1..30) {
    $code = (curl.exe -s -o NUL -w '%{http_code}' "http://localhost:$AdminPort/healthz")
    if ($code -eq '200') { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Warning 'Панель не ответила за 60 секунд. Логи: docker compose -f ... logs'
    exit 1
}

docker ps --filter "name=^$ContainerName$" --format '{{.Names}} — {{.Status}} — {{.Ports}}'

@"

Готово.

  Панель  : http://$PublicAddress`:$AdminPort  (первый вход: admin / admin)
  Ссылки  : http://$PublicAddress`:$PublicPort
  Каталог : $HostRoot  (панель показывает его как файловую систему сервера)

Что сделать дальше:
  1. Зайдите в панель и задайте новый пароль — она потребует этого сразу.
  2. Нажмите «Добавить каталог» и укажите путь внутри $HostRoot.
  3. Для ссылок из интернета укажите PUBLIC_ADDRESS с внешним адресом.

Логи:      docker compose -f "$ComposeFile" logs -f
Обновление: docker compose -f "$ComposeFile" pull; docker compose -f "$ComposeFile" up -d
Бэкап:     docker run --rm -v ${DataVolume}:/data -v "${PWD}:/backup" alpine tar czf /backup/sunduk-backup.tgz -C /data .
"@ | Write-Host
