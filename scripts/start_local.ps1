param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
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
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
if ($listeners | Where-Object { $_.LocalPort -eq 8002 }) {
    throw 'El puerto 8002 ya está ocupado. No se detuvo ningún proceso. Si correo ya está activo, use scripts/mail.ps1.'
}

function Assert-PrivateFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Falta un archivo privado de Gmail. Revise work/gmail-oauth-personal; no se generaron credenciales ni claves.'
    }
    $file = Get-Item -LiteralPath $Path
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $file.Length -lt 1 -or $file.Length -gt 16384) {
        throw 'El archivo privado de Gmail no es un archivo regular válido.'
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

$credentialsPath = Join-Path $workRoot 'gmail-oauth-personal/authorized-user.json'
$keyPath = Join-Path $workRoot 'gmail-oauth/approval-key.txt'
Assert-PrivateFile $credentialsPath
Assert-PrivateFile $keyPath
$approvalSecret = Get-Content -LiteralPath $keyPath -Raw -Encoding UTF8
if ([Text.Encoding]::UTF8.GetByteCount($approvalSecret) -lt 32 -or
    [Text.Encoding]::UTF8.GetByteCount($approvalSecret) -gt 4096) {
    throw 'La clave de aprobación existente no tiene una longitud válida. No se generó una nueva.'
}

$localProfile = @{
    MODEL_MODE = 'vertex'; VERTEX_PROJECT_ID = 'example-project'
    VERTEX_LOCATION = 'global'; MODEL_ID = 'gemini-3.1-flash-lite'; MODEL_CALL_LIMIT = '100'
    EMAIL_TRANSPORT = 'gmail'; EMAIL_SENDER = 'student@example.com'; ENABLE_REAL_EMAIL = 'true'
    GMAIL_CREDENTIALS_FILE = $credentialsPath; APPROVAL_SIGNING_KEY = $approvalSecret
    GMAIL_LEDGER = 'sqlite'; GMAIL_LEDGER_PATH = (Join-Path $workRoot 'gmail-dispatch.sqlite')
    HOST = '127.0.0.1'; PORT = '8002'; PUBLIC_AGENT_URL = 'http://127.0.0.1:8002'
    ENABLE_MCP = 'false'; PEER_USE_IAM = 'false'
    LANGSMITH_TRACING = 'false'; LANGCHAIN_TRACING_V2 = 'false'
}
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $PythonPath
$startInfo.Arguments = '-B -m email_agent'
$startInfo.WorkingDirectory = $projectRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $false
foreach ($name in $localProfile.Keys) { $startInfo.EnvironmentVariables[$name] = $localProfile[$name] }
foreach ($name in @('GOOGLE_API_KEY', 'GEMINI_API_KEY')) {
    $startInfo.EnvironmentVariables.Remove($name)
}

$child = $null
try {
    Write-Host 'Correo local en http://127.0.0.1:8002. En otra terminal ejecute scripts/mail.ps1. Ctrl+C detiene este proceso propio.'
    $child = [Diagnostics.Process]::Start($startInfo)
    $child.WaitForExit()
    if ($child.ExitCode -ne 0) { throw 'El proceso de correo terminó con error. Revise su diagnóstico de inicio.' }
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
