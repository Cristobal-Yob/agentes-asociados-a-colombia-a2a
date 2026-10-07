# Autorizar el Gmail personal

Remitente fijo: **student@example.com**. Proyecto OAuth: **example-project**. Cliente existente: **Colombia Two Agents Classroom**, de tipo escritorio. La autorización anterior de `learner@example.com` queda preservada y no sirve para esta identidad. El código no inicia sesión por sí solo ni sustituye el consentimiento humano.

## Consentimiento de la cuenta personal

El administrador del proyecto debe permitir `student@example.com` como usuario de prueba si la aplicación permanece en External / Testing. No se cambia la configuración de Cloud automáticamente. Mantenga los permisos existentes: `openid`, `email` (Google puede devolver su alias userinfo.email) y `gmail.send`; no se piden lectura, modificación, composición, perfil ni permisos de Cloud.

Prepare un directorio privado existente `work/gmail-oauth-personal` con acceso exclusivo del usuario actual. Desde `outputs/colombia-a2a`, ejecute con el intérprete existente:

```powershell
& '../../work/colombia-venv/Scripts/python.exe' -B -m scripts.authorize_gmail --client '../../work/gmail-oauth/desktop-client.json' --output '../../work/gmail-oauth-personal/authorized-user.json'
```

Seleccione **student@example.com** y acepte usted mismo los permisos de envío e identidad. El asistente abre Google, recibe la respuesta en un puerto loopback efímero y verifica PKCE, estado, nonce, firma/expiración/audiencia/emisor del ID token y coincidencia de email verificado y sujeto con UserInfo. El proceso dura como máximo cinco minutos y no envía, redacta ni lee correos. Nunca pegue los códigos o el JSON en chat.

El archivo de salida debe ser nuevo; el asistente nunca sobrescribe archivos existentes. Guarda solo credenciales de renovación, identidad y permisos; no conserva tokens de acceso/ID o códigos. En Windows restringe el archivo al usuario actual antes de escribir secretos. La clave persistente existente `work/gmail-oauth/approval-key.txt` y el ledger existente se reutilizan sin copiar, regenerar ni borrar.

Cuando termine, inicie `scripts/start_local.ps1`; si 8002 ya está ocupado, revise y detenga únicamente el proceso propio anterior antes de reiniciar. El inicio rechaza permisos incorrectos o archivos ausentes y no vuelve a la cuenta anterior ni a un transporte simulado. `scripts/mail.ps1` conversa con la API sin leer secretos.

## Envío y renovación

Una orden humana suficiente de redactar y enviar puede ejecutar en el mismo turno. Pedir solo un borrador requiere la confirmación posterior de su preview exacto. Cada envío renueva el token y verifica que la identidad real sea **student@example.com**, no solo el encabezado From. El destinatario puede ser cualquier dirección válida suministrada explícitamente; no se agregan Cc/Bcc.

Gmail no ofrece aquí una clave de idempotencia. El registro durable admite a lo sumo un intento automático por borrador y por continuación de envío consumida. Los resultados inciertos nunca se reintentan automáticamente. La aceptación de Gmail no demuestra llegada al buzón. Las pruebas usan grants y respuestas ficticios, sin envío real.

Los tokens de una aplicación External / Testing con gmail.send pueden vencer a los siete días, además de revocaciones o políticas. Para renovar, use un archivo nuevo protegido y cambie explícitamente la ruta local, sin sobrescribir el grant anterior. Cloud Run, IAM, GCS y secretos requieren autorización separada y no se configuran con este asistente.

Referencias oficiales: [OAuth de escritorio](https://developers.google.com/identity/protocols/oauth2/native-app), [OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect), [permisos Gmail](https://developers.google.com/workspace/gmail/api/auth/scopes), [envío de mensajes](https://developers.google.com/workspace/gmail/api/guides/sending).
