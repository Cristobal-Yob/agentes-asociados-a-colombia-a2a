param([string]$PythonPath, [ValidateSet("email", "tourism")][string]$Agent = "email")

$ErrorActionPreference = 'Stop'
if ($env:COLOMBIA_LIVE_OPT_IN -ne 'true' -or -not $env:VERTEX_PROJECT_ID) {
    throw 'Inicio real deshabilitado. Use el perfil fixture/mock o configure su proyecto y COLOMBIA_LIVE_OPT_IN=true explicitamente.'
}
if ($env:OS -ne 'Windows_NT') { throw 'Este inicio local requiere Windows.' }
if ($env:K_SERVICE) { throw 'El perfil privado local no se carga en Cloud Run.' }

$projectRoot = Split-Path $PSScriptRoot -Parent
$workRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '../../work'))
if (-not $PythonPath) {
    foreach ($candidate in @((Join-Path $projectRoot '.venv/Scripts/python.exe'),
                            (Join-Path $workRoot 'colombia-venv/Scripts/python.exe'))) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $PythonPath = $candidate
            break
        }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or
    [IO.Path]::GetFileName($PythonPath) -ne 'python.exe') {
    throw 'No se encontró un intérprete Python existente. No se instaló ninguna dependencia.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$port = if ($Agent -eq "email") { 8002 } else { 8001 }
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
if ($listeners | Where-Object { $_.LocalPort -eq $port }) {
    throw ('El puerto ' + $port + ' ya está ocupado. No se detuvo ningún proceso.')
}

function Assert-PrivateFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Falta un archivo privado requerido. Ejecute setup_local para las claves de servicio y revise el perfil OAuth; no se generaron credenciales.'
    }
    $file = Get-Item -LiteralPath $Path
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $file.Length -lt 1 -or $file.Length -gt 16384) {
        throw 'El archivo privado no es un archivo regular válido.'
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl = Get-Acl -LiteralPath $Path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -ne $currentUser -or -not $acl.AreAccessRulesProtected) {
        throw 'El archivo privado debe pertenecer al usuario actual y tener permisos exclusivos sin herencia.'
    }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -eq 'Allow' -and
            $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $currentUser) {
            throw 'El archivo privado permite acceso a otra identidad. No se cambiaron sus permisos.'
        }
    }
}

$authPath = Join-Path $workRoot ('service-auth/' + $Agent + '-api-key.txt')
Assert-PrivateFile $authPath
if ($Agent -eq "email") {
    $credentialsPath = Join-Path $workRoot 'gmail-oauth-clases/authorized-user.json'
    $keyPath = Join-Path $workRoot 'gmail-oauth/approval-key.txt'
    Assert-PrivateFile $credentialsPath
    Assert-PrivateFile $keyPath
    $approvalSecret = Get-Content -LiteralPath $keyPath -Raw -Encoding UTF8
    if ([Text.Encoding]::UTF8.GetByteCount($approvalSecret) -lt 32 -or
        [Text.Encoding]::UTF8.GetByteCount($approvalSecret) -gt 4096) {
        throw 'La clave de aprobación existente no tiene una longitud válida. No se generó una nueva.'
    }

    $localProfile = @{
        MODEL_MODE = 'vertex'; VERTEX_PROJECT_ID = $env:VERTEX_PROJECT_ID
        VERTEX_LOCATION = 'global'; MODEL_ID = 'gemini-3.1-flash-lite'; MODEL_CALL_LIMIT = '100'
        EMAIL_TRANSPORT = 'gmail'; EMAIL_SENDER = $env:GMAIL_EXPECTED_EMAIL; GMAIL_EXPECTED_EMAIL = $env:GMAIL_EXPECTED_EMAIL; GMAIL_OAUTH_PROJECT = $env:GMAIL_OAUTH_PROJECT; ENABLE_REAL_EMAIL = 'true'
        GMAIL_CREDENTIALS_FILE = $credentialsPath; APPROVAL_SIGNING_KEY = $approvalSecret
        GMAIL_LEDGER = 'sqlite'; GMAIL_LEDGER_PATH = (Join-Path $workRoot 'gmail-dispatch.sqlite')
        HOST = '127.0.0.1'; PORT = '8002'; PUBLIC_AGENT_URL = 'http://127.0.0.1:8002'
        ENABLE_MCP = 'false'; PEER_USE_IAM = 'false'
        LANGSMITH_TRACING = 'false'; LANGCHAIN_TRACING_V2 = 'false'
    }
} else {
    $emailKeyPath = Join-Path $workRoot 'service-auth/email-api-key.txt'
    $agendaKeyPath = Join-Path $workRoot 'service-auth/agenda-api-key.txt'
    Assert-PrivateFile $emailKeyPath
    Assert-PrivateFile $agendaKeyPath
    $localProfile = @{
        MODEL_MODE='vertex'; VERTEX_PROJECT_ID=$env:VERTEX_PROJECT_ID; VERTEX_LOCATION='global'
        MODEL_ID='gemini-3.1-flash-lite'; MODEL_CALL_LIMIT='100'; ENABLE_MCP='false'; PEER_USE_IAM='false'
        EMAIL_AGENT_URL='http://127.0.0.1:8002'; AGENDA_AGENT_URL='http://127.0.0.1:8003'
        EMAIL_AGENT_API_KEY_FILE=$emailKeyPath; AGENDA_AGENT_API_KEY_FILE=$agendaKeyPath
        HOST='127.0.0.1'; PORT='8001'; PUBLIC_AGENT_URL='http://127.0.0.1:8001'
        LANGSMITH_TRACING='false'; LANGCHAIN_TRACING_V2='false'
    }
}
$localProfile.AGENT_API_KEY_FILE = $authPath
$localProfile.WORKFLOW_DB_PATH = Join-Path $workRoot ('workflows/' + $Agent + '.sqlite')
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $PythonPath
$startInfo.Arguments = '-B -m ' + $Agent + '_agent'
$startInfo.WorkingDirectory = $projectRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
foreach ($name in @('AGENT_API_KEY','AGENT_API_KEY_FILE','EMAIL_AGENT_API_KEY','EMAIL_AGENT_API_KEY_FILE','AGENDA_AGENT_API_KEY','AGENDA_AGENT_API_KEY_FILE','GMAIL_CREDENTIALS_FILE','APPROVAL_SIGNING_KEY','RESEND_API_KEY')) { $startInfo.EnvironmentVariables.Remove($name) }
foreach ($name in $localProfile.Keys) { $startInfo.EnvironmentVariables[$name] = $localProfile[$name] }
foreach ($name in @('GOOGLE_API_KEY', 'GEMINI_API_KEY')) {
    $startInfo.EnvironmentVariables.Remove($name)
}

$child = $null
try {
    Write-Host ('API ' + $Agent + ' en http://127.0.0.1:' + $port + '/docs. Use Authorize con su clave propia. Ctrl+C detiene este hijo.')
    $child = [Diagnostics.Process]::Start($startInfo)
    $child.WaitForExit()
    if ($child.ExitCode -ne 0) { throw 'La API terminó con error. Revise su diagnóstico de inicio.' }
} finally {
    if ($child) {
        if (-not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
        $child.Dispose()
    }
    $approvalSecret = $null
    $localProfile.Clear()
    $startInfo.EnvironmentVariables.Remove('APPROVAL_SIGNING_KEY')
}
# Solo se modificó el entorno del hijo; el entorno y directorio del llamador quedan intactos.
