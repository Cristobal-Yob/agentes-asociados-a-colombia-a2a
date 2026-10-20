param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
$env:COLOMBIA_LIVE_OPT_IN = 'true'
$env:VERTEX_PROJECT_ID = 'example-project'
$env:GMAIL_EXPECTED_EMAIL = 'sender@example.com'
$env:GMAIL_OAUTH_PROJECT = 'example-project'
$env:EMAIL_AGENT_API_KEY = 'SYNTHETIC_TEST_EMAIL_API_KEY_32_BYTES'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$workRoot = [IO.Path]::GetFullPath((Join-Path $sourceRoot '../../work'))
if (-not $PythonPath) { $PythonPath = Join-Path $workRoot 'colombia-venv/Scripts/python.exe' }
$mailScript = Join-Path $sourceRoot 'scripts/mail.ps1'
$startScript = Join-Path $sourceRoot 'scripts/start_local.ps1'
$testRoot = Join-Path $workRoot ('personal-gmail-20261006/harness-' + [guid]::NewGuid())
[void](New-Item -ItemType Directory -Path $testRoot)
$script:failures = 0
$script:passed = 0

function Assert-True($Condition, $Label) {
    if (-not $Condition) { throw $Label }
}

function Run-Case($Name, $Body) {
    try {
        & $Body
        $script:passed++
        [Console]::WriteLine('PASS: ' + $Name)
    } catch {
        $script:failures++
        [Console]::WriteLine('FAIL: ' + $Name + ' — ' + $_.Exception.Message)
        [Console]::WriteLine($_.ScriptStackTrace)
    }
}

function Reset-Mail($Responses, $Answers) {
    $global:LocalTestRequests = [Collections.Generic.List[object]]::new()
    $global:LocalTestShown = [Collections.Generic.List[string]]::new()
    $global:LocalTestResponses = [Collections.Queue]::new()
    $global:LocalTestAnswers = [Collections.Queue]::new()
    foreach ($response in $Responses) { $global:LocalTestResponses.Enqueue($response) }
    foreach ($answer in $Answers) { $global:LocalTestAnswers.Enqueue($answer) }
}

function Reply($Status, $Context = $null, $Preview = $null, $Delivery = $null) {
    [pscustomobject]@{
        status = $Status; context = $Context; preview = $Preview; delivery = $Delivery
        conversation_state = if ($Status -eq 'sent') { 'sent' } else { 'awaiting_confirmation' }
        message = 'Respuesta de prueba'
    }
}

$preview = [pscustomobject]@{
    recipient = 'persona@example.com'; subject = 'Reunión mañana'
    text = "Primera línea exacta.`nSegunda línea: café."
    sender = 'sender@example.com'; transport = 'gmail'; expires_at = 4102444800
}

function Invoke-RestMethod {
    param($Uri, $Method, $ContentType, $Body, $TimeoutSec, $MaximumRedirection, $ErrorAction, $Headers)
    Assert-True ($Headers['X-API-Key'] -eq 'SYNTHETIC_TEST_EMAIL_API_KEY_32_BYTES') 'Falta autenticación de Correo'
    Assert-True ($Uri -eq 'http://127.0.0.1:8002/api/chat') 'URL inesperada'
    Assert-True ($Body -is [byte[]]) 'El JSON debe enviarse como bytes UTF-8'
    Assert-True ($MaximumRedirection -eq 0) 'No se permiten redirecciones'
    $request = [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
    $global:LocalTestRequests.Add($request)
    Assert-True ($global:LocalTestResponses.Count -gt 0) 'Se repitió una llamada sin respuesta de prueba'
    $response = $global:LocalTestResponses.Dequeue()
    if ($response -is [Exception]) { throw $response }
    return $response
}

function Read-Host {
    param($Prompt)
    Assert-True ($global:LocalTestAnswers.Count -gt 0) 'Pregunta inesperada'
    $answer = $global:LocalTestAnswers.Dequeue()
    if ($answer -eq 'Sí, envíalo') {
        foreach ($exact in @($preview.recipient, $preview.subject, $preview.text,
                            $preview.sender, $preview.transport)) {
            Assert-True (($global:LocalTestShown -join "`n").Contains($exact)) 'Falta vista previa exacta'
        }
    }
    return $answer
}

function Write-Host { param($Object) $global:LocalTestShown.Add([string]$Object) }
function Write-Warning { param($Message) $global:LocalTestShown.Add([string]$Message) }

Run-Case 'Mensaje directo, vista previa y única confirmación con contexto exacto' {
    Reset-Mail @((Reply preview 'TOKEN_OPACO' $preview), (Reply sent $null $null provider_accepted)) @('Sí, envíalo')
    & $mailScript -Message 'Redacta una reunión para persona@example.com con café.'
    Assert-True ($global:LocalTestRequests.Count -eq 2) 'Cantidad de POST inesperada'
    Assert-True ($global:LocalTestRequests[0].PSObject.Properties.Name.Count -eq 1) 'Primera petición debe contener solo message'
    Assert-True ($global:LocalTestRequests[1].context -ceq 'TOKEN_OPACO') 'Contexto alterado'
    Assert-True (($global:LocalTestShown -join "`n").Contains('Correo aceptado por Gmail')) 'Falta aceptación verdadera'
}

Run-Case 'Datos faltantes de uno en uno, sin flags ni aprobación automática' {
    Reset-Mail @((Reply clarification 'C1'), (Reply clarification 'C2'),
        (Reply preview 'C3' $preview), (Reply sent $null $null provider_accepted)) @(
        'persona@example.com', 'Recordatorio de reunión', 'Sí, envíalo')
    & $mailScript -Message 'Hola'
    Assert-True ($global:LocalTestRequests.Count -eq 4) 'Secuencia incompleta'
    Assert-True ($global:LocalTestRequests[1].context -ceq 'C1') 'Primer contexto alterado'
    Assert-True ($global:LocalTestRequests[2].context -ceq 'C2') 'Segundo contexto alterado'
    Assert-True ($global:LocalTestRequests[3].context -ceq 'C3') 'Contexto de aprobación alterado'
}

Run-Case 'Cancelación después de vista previa no hace otro POST' {
    Reset-Mail @((Reply preview 'TOKEN' $preview)) @('cancelar')
    & $mailScript -Message 'Recordatorio para persona@example.com'
    Assert-True ($global:LocalTestRequests.Count -eq 1) 'Cancelación hizo POST'
}

Run-Case 'Sin parámetros solicita el mensaje; cancelar inicialmente no hace POST' {
    Reset-Mail @((Reply preview 'TOKEN' $preview)) @('Reunión para persona@example.com', 'cancelar')
    & $mailScript
    Assert-True ($global:LocalTestRequests.Count -eq 1) 'Falta mensaje inicial interactivo'
    Assert-True ($global:LocalTestRequests[0].message -eq 'Reunión para persona@example.com') 'Mensaje inicial alterado'
    Reset-Mail @() @('cancelar')
    & $mailScript
    Assert-True ($global:LocalTestRequests.Count -eq 0) 'Cancelación inicial hizo POST'
}

Run-Case 'Corrección natural conserva contexto y exige nueva vista previa' {
    $changed = $preview.PSObject.Copy()
    $changed.recipient = 'otra@example.com'
    Reset-Mail @((Reply preview 'ORIGINAL' $preview), (Reply preview 'NUEVO' $changed)) @(
        'Sí, pero cambia el destinatario a otra@example.com', 'cancelar')
    & $mailScript -Message 'Recordatorio para persona@example.com'
    Assert-True ($global:LocalTestRequests.Count -eq 2) 'Se intentó confirmar una corrección'
    Assert-True ($global:LocalTestRequests[1].context -ceq 'ORIGINAL') 'Corrección perdió contexto'
}

Run-Case 'Resultado ambiguo y error de red no repiten POST ni anuncian aceptación' {
    $missingState = Reply sent $null $null provider_accepted
    $missingState.conversation_state = $null
    foreach ($result in @((Reply delivery_unknown), [Exception]::new('Fallo simulado'),
        (Reply sent $null $null unknown), $missingState, (Reply simulated $null $null simulated_only))) {
        Reset-Mail @((Reply preview 'TOKEN' $preview), $result) @('Sí, envíalo')
        & $mailScript -Message 'Recordatorio para persona@example.com'
        Assert-True ($global:LocalTestRequests.Count -eq 2) 'Se repitió un POST'
        Assert-True (-not (($global:LocalTestShown -join "`n").Contains('Correo aceptado por Gmail'))) 'Aceptación inventada'
    }
}

Run-Case 'Envío directo aceptado muestra contenido final sin exigir vista previa anterior' {
    Reset-Mail @((Reply sent $null $preview provider_accepted)) @()
    & $mailScript -Message 'Redacta un recordatorio y envíalo a persona@example.com'
    Assert-True ($global:LocalTestRequests.Count -eq 1) 'Se repitió una orden directa'
    Assert-True (($global:LocalTestShown -join "`n").Contains('Correo aceptado por Gmail')) 'Falta aceptación real del contrato'
}

Run-Case 'Conversación normal permite otro mensaje sin exigir campos de correo' {
    Reset-Mail @((Reply completed)) @('cancelar')
    & $mailScript -Message 'Hola'
    Assert-True ($global:LocalTestRequests.Count -eq 1) 'Se repitió conversación'
    Assert-True (($global:LocalTestShown -join "`n").Contains('Respuesta de prueba')) 'Falta conversación'
}

Run-Case 'URL remota se rechaza antes de enviar mensaje o contexto' {
    Reset-Mail @() @()
    $rejected = $false
    try { & $mailScript -Message 'Contenido privado' -ApiUrl 'https://example.com' } catch { $rejected = $true }
    Assert-True $rejected 'Se permitió URL remota'
    Assert-True ($global:LocalTestRequests.Count -eq 0) 'URL remota recibió POST'
}

function Get-NetTCPConnection { param($State, $ErrorAction)
    if ($global:LocalTestOccupied) { [pscustomobject]@{LocalPort = 8002} }
}

function New-StartupFixture {
    $fixture = Join-Path $testRoot ([guid]::NewGuid().ToString())
    $project = Join-Path $fixture 'outputs/colombia-a2a'
    $private = Join-Path $fixture 'work/gmail-oauth'
    [void](New-Item -ItemType Directory -Path (Join-Path $project 'scripts') -Force)
    [void](New-Item -ItemType Directory -Path (Join-Path $project 'email_agent') -Force)
    [void](New-Item -ItemType Directory -Path $private -Force)
    Copy-Item -LiteralPath $startScript -Destination (Join-Path $project 'scripts/start_local.ps1')
    $personal = Join-Path $fixture 'work/gmail-oauth-clases'
    [void](New-Item -ItemType Directory -Path $personal -Force)
    [IO.File]::WriteAllText((Join-Path $personal 'authorized-user.json'), '{"type":"fixture"}')
    [IO.File]::WriteAllText((Join-Path $private 'approval-key.txt'), ('p' * 32))
    $auth = Join-Path $fixture 'work/service-auth'
    [void](New-Item -ItemType Directory -Path $auth -Force)
    foreach ($role in @('email','tourism','agenda')) { [IO.File]::WriteAllText((Join-Path $auth ($role + '-api-key.txt')), ('s' * 40)) }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($path in @((Join-Path $personal 'authorized-user.json'), (Join-Path $private 'approval-key.txt'), (Join-Path $auth 'email-api-key.txt'), (Join-Path $auth 'tourism-api-key.txt'), (Join-Path $auth 'agenda-api-key.txt'))) {
        $acl = [Security.AccessControl.FileSecurity]::new()
        $acl.SetOwner($identity)
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
        Set-Acl -LiteralPath $path -AclObject $acl
    }
    $probe = @'
import hashlib, json, os, sys
keys = ["MODEL_MODE", "VERTEX_PROJECT_ID", "VERTEX_LOCATION", "MODEL_ID", "EMAIL_TRANSPORT", "EMAIL_SENDER", "ENABLE_REAL_EMAIL", "HOST", "PORT", "GMAIL_CREDENTIALS_FILE", "GMAIL_LEDGER_PATH"]
data = {key: os.environ.get(key) for key in keys}
data["key_hash"] = hashlib.sha256(os.environ["APPROVAL_SIGNING_KEY"].encode()).hexdigest()
data["argv"] = sys.argv
with open(os.environ["LOCAL_TEST_RESULT"], "w", encoding="utf-8") as result:
    json.dump(data, result)
'@
    [IO.File]::WriteAllText((Join-Path $project 'email_agent/__main__.py'), $probe)
    [IO.File]::WriteAllText((Join-Path $project 'email_agent/__init__.py'), '')
    return [pscustomobject]@{Root = $project; Private = $personal; Script = (Join-Path $project 'scripts/start_local.ps1'); Result = (Join-Path $fixture 'child.json')}
}

Run-Case 'Inicio nativo hermético usa perfil local solo en hijo y conserva entorno' {
    $fixture = New-StartupFixture
    $global:LocalTestOccupied = $false
    $oldResult = $env:LOCAL_TEST_RESULT
    $oldKey = $env:APPROVAL_SIGNING_KEY
    try {
        $env:LOCAL_TEST_RESULT = $fixture.Result
        $env:APPROVAL_SIGNING_KEY = 'CLAVE_ANTERIOR_FICTICIA'
        & $fixture.Script -PythonPath $PythonPath
        Assert-True ($env:APPROVAL_SIGNING_KEY -ceq 'CLAVE_ANTERIOR_FICTICIA') 'Se alteró entorno del llamador'
        $data = Get-Content -LiteralPath $fixture.Result -Raw | ConvertFrom-Json
        Assert-True ($data.EMAIL_TRANSPORT -eq 'gmail' -and $data.PORT -eq '8002') 'Perfil incorrecto'
        Assert-True ($data.MODEL_MODE -eq 'vertex' -and $data.EMAIL_SENDER -eq 'sender@example.com') 'Valores incorrectos'
        Assert-True ($data.GMAIL_CREDENTIALS_FILE -eq (Join-Path $fixture.Private 'authorized-user.json')) 'Path incorrecto'
        Assert-True ($data.GMAIL_LEDGER_PATH -eq [IO.Path]::GetFullPath((Join-Path $fixture.Root '../../work/gmail-dispatch.sqlite'))) 'Ledger incorrecto'
    } finally {
        $env:LOCAL_TEST_RESULT = $oldResult
        $env:APPROVAL_SIGNING_KEY = $oldKey
    }
}

Run-Case 'Turismo inicia sin grant ni clave Gmail con claves de sus pares' {
    $fixture = New-StartupFixture
    $global:LocalTestOccupied = $false
    $module = Join-Path $fixture.Root 'tourism_agent'
    [void](New-Item -ItemType Directory -Path $module -Force)
    [IO.File]::WriteAllText((Join-Path $module '__init__.py'), '')
    $probe = @'
import os
assert not os.getenv('GMAIL_CREDENTIALS_FILE')
assert not os.getenv('APPROVAL_SIGNING_KEY')
assert os.environ['PORT'] == '8001'
assert os.environ['AGENT_API_KEY_FILE'].endswith('tourism-api-key.txt')
assert os.environ['EMAIL_AGENT_API_KEY_FILE'].endswith('email-api-key.txt')
assert os.environ['AGENDA_AGENT_API_KEY_FILE'].endswith('agenda-api-key.txt')
assert os.environ['WORKFLOW_DB_PATH'].endswith('tourism.sqlite')
'@
    [IO.File]::WriteAllText((Join-Path $module '__main__.py'), $probe)
    Remove-Item -LiteralPath (Join-Path $fixture.Private 'authorized-user.json')
    & $fixture.Script -PythonPath $PythonPath -Agent tourism
}

Run-Case 'Puerto ocupado no mata ni lanza procesos' {
    $fixture = New-StartupFixture
    $global:LocalTestOccupied = $true
    $rejected = $false
    try { & $fixture.Script -PythonPath $PythonPath } catch { $rejected = $true }
    Assert-True $rejected 'Se permitió puerto ocupado'
    Assert-True (-not (Test-Path -LiteralPath $fixture.Result)) 'Se lanzó un hijo'
}

Run-Case 'Credenciales faltantes y Cloud Run impiden perfil local' {
    foreach ($mode in @('missing', 'cloud')) {
        $fixture = New-StartupFixture
        $global:LocalTestOccupied = $false
        $oldService = $env:K_SERVICE
        $rejected = $false
        try {
            if ($mode -eq 'missing') { Remove-Item -LiteralPath (Join-Path $fixture.Private 'authorized-user.json') }
            else { $env:K_SERVICE = 'SERVICIO_FICTICIO' }
            try { & $fixture.Script -PythonPath $PythonPath } catch { $rejected = $true }
        } finally { $env:K_SERVICE = $oldService }
        Assert-True $rejected 'Se permitió configuración inválida'
        Assert-True (-not (Test-Path -LiteralPath $fixture.Result)) 'Se lanzó un hijo'
    }
}

Run-Case 'Parser PowerShell acepta ambos scripts' {
    foreach ($path in @($mailScript, $startScript)) {
        $tokens = $null; $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
        Assert-True ($errors.Count -eq 0) 'Error de sintaxis PowerShell'
    }
}

[Console]::WriteLine("Resultado: $script:passed pruebas correctas; $script:failures fallos. Sin red ni proveedores reales.")
if ($script:failures) { exit 1 }
