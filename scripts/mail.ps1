param(
    [string]$Message,
    [string]$ApiUrl = 'http://127.0.0.1:8002'
)

$ErrorActionPreference = 'Stop'
$base = $null
if (-not [Uri]::TryCreate($ApiUrl, [UriKind]::Absolute, [ref]$base) -or
    $base.Scheme -notin @('http', 'https') -or $base.Host -notin @('127.0.0.1', 'localhost') -or
    $base.UserInfo -or $base.Query -or $base.Fragment -or $base.AbsolutePath -ne '/') {
    throw 'Use una URL base local de correo, sin credenciales, ruta, consulta ni fragmento.'
}
$endpoint = $base.AbsoluteUri.TrimEnd('/') + '/api/chat'
if (-not $Message.Trim()) { $Message = Read-Host 'Converse o indique qué mensaje desea redactar y enviar y a quién' }
$context = $null
$displayedGmailPreview = $false

for ($turn = 0; $turn -lt 20; $turn++) {
    if (-not $Message.Trim() -or $Message.Trim().ToLowerInvariant() -in @('cancelar', 'salir', 'no')) {
        Write-Host 'Solicitud cancelada. No se intentó un nuevo envío.'
        return
    }
    $request = @{message = $Message}
    if ($context) { $request.context = $context }
    $json = $request | ConvertTo-Json -Depth 15 -Compress
    try {
        $reply = Invoke-RestMethod -Uri $endpoint -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 45 -MaximumRedirection 0 -ErrorAction Stop
    } catch {
        Write-Warning 'No se recibió un resultado seguro. No se repitió la petición. Si había un envío en curso, su entrega puede ser incierta.'
        return
    }
    if (-not $reply -or -not $reply.status) {
        Write-Warning 'La respuesta no permite determinar el resultado. No se reintentó.'
        return
    }
    if ($reply.status -eq 'sent') {
        if (($displayedGmailPreview -or ($reply.preview -and $reply.preview.transport -eq 'gmail' -and $reply.preview.sender -eq 'student@example.com' -and $reply.preview.recipient -and $reply.preview.subject -and $reply.preview.text)) -and $reply.delivery -eq 'provider_accepted' -and
            $reply.conversation_state -eq 'sent') {
            if ($reply.preview) {
                Write-Host ('Remitente: ' + $reply.preview.sender)
                Write-Host ('Destinatario: ' + $reply.preview.recipient)
                Write-Host ('Asunto: ' + $reply.preview.subject)
                Write-Host $reply.preview.text
            }
            Write-Host 'Correo aceptado por Gmail. La aceptación no garantiza llegada al buzón.'
        } else {
            Write-Warning 'No se confirmó la aceptación de Gmail de forma segura. No se reintentó.'
        }
        return
    }
    if ($reply.status -eq 'simulated') {
        Write-Host 'Simulación completada. No se envió correo real.'
        return
    }
    if ($reply.status -eq 'completed') {
        if ($reply.message) { Write-Host $reply.message }
        $context = $null
        $Message = Read-Host 'Escriba otro mensaje o cancelar para terminar'
        continue
    }
    if ($reply.status -notin @('preview', 'clarification')) {
        Write-Warning 'La solicitud terminó sin una aceptación confirmada. No se reintentó.'
        if ($reply.message) { Write-Host $reply.message }
        return
    }
    if ($reply.message) { Write-Host $reply.message }
    if ($reply.preview) {
        $preview = $reply.preview
        if (-not $preview.recipient -or -not $preview.subject -or -not $preview.text -or
            -not $preview.sender -or -not $preview.transport) {
            Write-Warning 'La vista previa está incompleta. No se intentó confirmar ni enviar.'
            return
        }
        Write-Host ('Destinatario: ' + $preview.recipient)
        Write-Host ('Asunto: ' + $preview.subject)
        Write-Host ('Remitente: ' + $preview.sender)
        Write-Host ('Transporte: ' + $preview.transport)
        Write-Host 'Texto exacto:'
        Write-Host $preview.text
        if ($preview.transport -eq 'gmail' -and $preview.sender -ne 'student@example.com') {
            Write-Warning 'La API usa otro remitente. Inicie el perfil personal correcto antes de continuar.'
            return
        }
        $displayedGmailPreview = $preview.transport -eq 'gmail' -and $preview.sender -eq 'student@example.com'
    }
    if (-not ($reply.context -is [string]) -or -not $reply.context) {
        Write-Warning 'Falta el contexto de continuación válido. No se hizo otra petición.'
        return
    }
    $context = $reply.context
    $Message = Read-Host 'Responda al agente; tras revisar la vista previa, «Sí, envíalo» confirma. Escriba cancelar para terminar'
}
Write-Warning 'Se alcanzó el límite de turnos. No se hicieron más peticiones.'
