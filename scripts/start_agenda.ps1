param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
if ($env:COLOMBIA_LIVE_OPT_IN -ne 'true' -or -not $env:VERTEX_PROJECT_ID) {
    throw 'Inicio real deshabilitado. Use el perfil fixture/mock o configure su proyecto y COLOMBIA_LIVE_OPT_IN=true explicitamente.'
}
if ($env:OS -ne 'Windows_NT') { throw 'Este inicio local requiere Windows.' }
if ($env:K_SERVICE) { throw 'El perfil privado local no se carga en Cloud Run.' }
$projectRoot = Split-Path $PSScriptRoot -Parent
$workRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '../../work'))
if (-not $PythonPath) {
    foreach ($candidate in @((Join-Path $projectRoot '.venv/Scripts/python.exe'), (Join-Path $workRoot 'colombia-venv/Scripts/python.exe'))) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $PythonPath = $candidate; break }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or [IO.Path]::GetFileName($PythonPath) -ne 'python.exe') {
    throw 'No se encontró un intérprete Python existente. No se instaló ninguna dependencia.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (@(Get-NetTCPConnection -State Listen -ErrorAction Stop) | Where-Object { $_.LocalPort -eq 8003 }) {
    throw 'El puerto 8003 está ocupado. No se detuvo ningún proceso.'
}
function Assert-PrivateFile([string]$Path) {
    $file = Get-Item -LiteralPath $Path
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $file.Length -lt 1 -or $file.Length -gt 16384) { throw 'Archivo privado de Agenda no válido.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $identity) { throw 'Agenda exige archivos privados propios y sin herencia de permisos.' }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $identity) { throw 'El archivo permite acceso a otra identidad; no se modificaron permisos.' }
    }
}
$grantPath = Join-Path $workRoot 'calendar-oauth-clases/authorized-user.json'
$keyPath = Join-Path $workRoot 'calendar-oauth-clases/approval-key.txt'
Assert-PrivateFile $keyPath
$authPath = Join-Path $workRoot 'service-auth/agenda-api-key.txt'
$emailKeyPath = Join-Path $workRoot 'service-auth/email-api-key.txt'
Assert-PrivateFile $authPath
if (Test-Path -LiteralPath $emailKeyPath) { Assert-PrivateFile $emailKeyPath } else { $emailKeyPath = '' }
$grant = ''; $key = ''
if (Test-Path -LiteralPath $grantPath) { Assert-PrivateFile $grantPath; $grant = $grantPath }
if (Test-Path -LiteralPath $keyPath) {
    Assert-PrivateFile $keyPath
    $key = Get-Content -LiteralPath $keyPath -Raw -Encoding UTF8
    if ([Text.Encoding]::UTF8.GetByteCount($key) -lt 32 -or [Text.Encoding]::UTF8.GetByteCount($key) -gt 4096) { throw 'La clave propia de Agenda tiene longitud inválida.' }
}
$agendaProfile = @{
    AGENT_API_KEY_FILE = $authPath; EMAIL_AGENT_API_KEY_FILE = $emailKeyPath
    WORKFLOW_DB_PATH = (Join-Path $workRoot 'workflows/agenda.sqlite')
    MODEL_MODE = 'vertex'; VERTEX_PROJECT_ID = $env:VERTEX_PROJECT_ID; VERTEX_LOCATION = 'global'
    MODEL_ID = 'gemini-3.1-flash-lite'; MODEL_CALL_LIMIT = '100'
    CALENDAR_CREDENTIALS_FILE = $grant; AGENDA_APPROVAL_SIGNING_KEY = $key
    CALENDAR_LEDGER_PATH = (Join-Path $workRoot 'calendar-dispatch.sqlite')
    EMAIL_AGENT_URL = 'http://127.0.0.1:8002'; HOST = '127.0.0.1'; PORT = '8003'
    PUBLIC_AGENT_URL = 'http://127.0.0.1:8003'; PEER_USE_IAM = 'false'; ENABLE_MCP = 'false'
    LANGSMITH_TRACING = 'false'; LANGCHAIN_TRACING_V2 = 'false'
}
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $PythonPath; $startInfo.Arguments = '-B -m agenda_agent'
$startInfo.WorkingDirectory = $projectRoot; $startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
foreach ($name in @('AGENT_API_KEY','AGENT_API_KEY_FILE','EMAIL_AGENT_API_KEY','EMAIL_AGENT_API_KEY_FILE','AGENDA_AGENT_API_KEY','AGENDA_AGENT_API_KEY_FILE')) { $startInfo.EnvironmentVariables.Remove($name) }
foreach ($name in $agendaProfile.Keys) { $startInfo.EnvironmentVariables[$name] = $agendaProfile[$name] }
foreach ($name in @('GOOGLE_API_KEY', 'GEMINI_API_KEY', 'GMAIL_CREDENTIALS_FILE', 'APPROVAL_SIGNING_KEY', 'RESEND_API_KEY')) { $startInfo.EnvironmentVariables.Remove($name) }
$child = $null
try {
    Write-Host 'Agenda en http://127.0.0.1:8003/docs. Sin grant, conversa y solicita autorización antes de consultar Calendar. Ctrl+C detiene este hijo.'
    $child = [Diagnostics.Process]::Start($startInfo)
    $child.WaitForExit()
    if ($child.ExitCode -ne 0) { throw 'El proceso de Agenda terminó con error; revise el diagnóstico de inicio.' }
} finally {
    if ($child) { if (-not $child.HasExited) { $child.Kill(); $child.WaitForExit() }; $child.Dispose() }
    $key = $null; $agendaProfile.Clear(); $startInfo.EnvironmentVariables.Remove('AGENDA_APPROVAL_SIGNING_KEY')
}
