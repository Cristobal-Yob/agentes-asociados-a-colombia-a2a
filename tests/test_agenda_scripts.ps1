param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$env:COLOMBIA_LIVE_OPT_IN = 'true'
$env:VERTEX_PROJECT_ID = 'example-project'
$env:GMAIL_EXPECTED_EMAIL = 'sender@example.com'
$env:GMAIL_OAUTH_PROJECT = 'example-project'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$workRoot = [IO.Path]::GetFullPath((Join-Path $sourceRoot '../../work'))
if (-not $PythonPath) { $PythonPath = Join-Path $workRoot 'colombia-venv/Scripts/python.exe' }
$scriptPath = Join-Path $sourceRoot 'scripts/start_agenda.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw 'Falta el launcher independiente de Agenda.' }
$launcherBytes = [IO.File]::ReadAllBytes($scriptPath)
if ($launcherBytes.Length -lt 3 -or $launcherBytes[0] -ne 0xEF -or $launcherBytes[1] -ne 0xBB -or $launcherBytes[2] -ne 0xBF) { throw 'El launcher requiere UTF-8 con BOM para conservar el texto en Windows PowerShell 5.' }
$fixture = Join-Path $workRoot ('agenda-harness-' + [guid]::NewGuid())
$project = Join-Path $fixture 'outputs/colombia-a2a'
[void](New-Item -ItemType Directory -Path (Join-Path $project 'scripts') -Force)
[void](New-Item -ItemType Directory -Path (Join-Path $project 'agenda_agent') -Force)
Copy-Item -LiteralPath $scriptPath -Destination (Join-Path $project 'scripts/start_agenda.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$auth = Join-Path $fixture 'work/service-auth'
$calendarPrivate = Join-Path $fixture 'work/calendar-oauth-clases'
[void](New-Item -ItemType Directory -Path $auth -Force)
[void](New-Item -ItemType Directory -Path $calendarPrivate -Force)
foreach ($path in @((Join-Path $auth 'agenda-api-key.txt'), (Join-Path $auth 'email-api-key.txt'), (Join-Path $calendarPrivate 'approval-key.txt'))) {
    [IO.File]::WriteAllText($path, ('s' * 40))
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetOwner($identity); $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
    Set-Acl -LiteralPath $path -AclObject $acl
}
$resultPath = Join-Path $fixture 'child.json'
$probe = @'
import json, os
assert not os.getenv('GMAIL_CREDENTIALS_FILE')
assert not os.getenv('APPROVAL_SIGNING_KEY')
assert os.getenv('AGENDA_APPROVAL_SIGNING_KEY')
assert os.getenv('AGENT_API_KEY_FILE').endswith('agenda-api-key.txt')
assert os.getenv('EMAIL_AGENT_API_KEY_FILE').endswith('email-api-key.txt')
with open(os.environ['AGENDA_TEST_RESULT'], 'w', encoding='utf-8') as stream:
    json.dump({k: os.environ.get(k) for k in ['PORT','MODEL_MODE','CALENDAR_CREDENTIALS_FILE','AGENDA_APPROVAL_SIGNING_KEY']}, stream)
'@
[IO.File]::WriteAllText((Join-Path $project 'agenda_agent/__init__.py'), '')
[IO.File]::WriteAllText((Join-Path $project 'agenda_agent/__main__.py'), $probe)
function Get-NetTCPConnection { param($State, $ErrorAction)
    if ($global:AgendaTestOccupied) { [pscustomobject]@{LocalPort = 8003} }
}
$oldResult = $env:AGENDA_TEST_RESULT
$oldGrant = $env:GMAIL_CREDENTIALS_FILE
$oldKey = $env:APPROVAL_SIGNING_KEY
try {
    $env:AGENDA_TEST_RESULT = $resultPath
    $env:GMAIL_CREDENTIALS_FILE = 'ARCHIVO_AJENO_NO_LEER'
    $env:APPROVAL_SIGNING_KEY = 'CLAVE_AJENA_NO_LEER'
    & (Join-Path $project 'scripts/start_agenda.ps1') -PythonPath $PythonPath
    $data = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
    if ($data.PORT -ne '8003' -or $data.MODEL_MODE -ne 'vertex' -or $data.CALENDAR_CREDENTIALS_FILE) { throw 'Perfil independiente incorrecto.' }
    if ($env:APPROVAL_SIGNING_KEY -cne 'CLAVE_AJENA_NO_LEER') { throw 'Se alteró el entorno padre.' }
    Remove-Item -LiteralPath (Join-Path $auth 'email-api-key.txt')
    [IO.File]::WriteAllText((Join-Path $project 'agenda_agent/__main__.py'), "import os; assert not os.getenv('EMAIL_AGENT_API_KEY_FILE'); assert os.getenv('AGENT_API_KEY_FILE')")
    & (Join-Path $project 'scripts/start_agenda.ps1') -PythonPath $PythonPath
    $global:AgendaTestOccupied = $true
    $rejected = $false
    try { & (Join-Path $project 'scripts/start_agenda.ps1') -PythonPath $PythonPath } catch { $rejected = $true }
    if (-not $rejected) { throw 'Se permitió un puerto ocupado.' }
    $tokens = $null; $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw 'Sintaxis PowerShell inválida.' }
    [Console]::WriteLine('Agenda: inicio sin Gmail/Calendar, aislamiento de entorno, puerto ocupado y sintaxis verificados. Sin proveedores.')
} finally {
    $env:AGENDA_TEST_RESULT = $oldResult
    $env:GMAIL_CREDENTIALS_FILE = $oldGrant
    $env:APPROVAL_SIGNING_KEY = $oldKey
}
