# Guía BYOK: crear tu propia app OAuth (Google, Microsoft, Slack, Notion)

Decisión C1: en v1, cada usuario usa el client ID de una app OAuth que ha creado él mismo (BYOK). La guía
que ve el usuario está en `/connectors` y sale de `ops_dashboard/web/connectors-guide.js` (ES/EN). Este
documento sirve para revisarla: dice de dónde sale cada paso y qué no se ha podido verificar.

Revisado contra la documentación oficial el 2026-10-21. **⚠️ = no se ha podido confirmar en la
documentación oficial**: hay que probarlo en el Mac con una app real (ver «Probar en el Mac»).

## Contrato de redirects (lo implementan los conectores nativos)

| Proveedor | Redirect que se registra | Redirect que usa ConductLoop | Por qué |
|---|---|---|---|
| Google | ninguno (cliente «Desktop app») | `http://127.0.0.1:<puerto libre>/callback` | Los clientes Desktop aceptan el loopback `http://127.0.0.1:port` en un puerto aleatorio. ⚠️ Google no documenta la ruta `/callback`. |
| Microsoft | `http://localhost/callback` («Mobile and desktop applications») | `http://localhost:<puerto libre>/callback` | Entra ignora el puerto en los redirects de `localhost`, pero la ruta sí cuenta. El portal no deja añadir `http://127.0.0.1` (solo editando el manifiesto). |
| Slack | `http://localhost:47823/callback` | el mismo, **puerto fijo** | Slack compara el redirect entero, puerto incluido. Solo `localhost` aparece documentado como redirect de escritorio con PKCE. |
| Notion | — (no aplica) | — | El conector de Notion usa un token de «Conexiones», no OAuth. El OAuth de abajo queda como referencia: **no está implementado** (ver la sección de Notion). |

`tests/test_connectors_guide.py` fija estos valores para que la guía y los conectores no se desincronicen.

## Google — scopes mínimos

| Conector | Lectura | Escritura (PR posterior, con confirmación) | Clasificación |
|---|---|---|---|
| Calendar | `calendar.calendarlist.readonly`, `calendar.events.readonly` | `calendar.events.owned` | Las páginas de Calendar no dan la clasificación de estos scopes. |
| Gmail | `gmail.readonly` | `gmail.send` | `gmail.readonly` es **restringido**; `gmail.send` es sensible. |
| Drive | `drive.readonly` | `drive.file` | `drive.readonly` es **restringido**; `drive.file` es no sensible. |

Verificado:
- Cliente «Desktop app» con loopback `http://127.0.0.1:port` y PKCE. Google genera el client ID y el client secret, y dice que los clientes Desktop son públicos y no pueden guardar secretos. En la tabla del intercambio, `client_secret` aparece como «Optional». ConductLoop lo pide y lo guarda en el llavero porque Google lo entrega con todos los clientes Desktop. — [native-app](https://developers.google.com/identity/protocols/oauth2/native-app)
- Branding, Audience (External/Internal, test users) y Data Access. — [configure-oauth-consent](https://developers.google.com/workspace/guides/configure-oauth-consent)
- En «Testing» hay un máximo de 100 test users, y las autorizaciones caducan a los 7 días, refresh token incluido. — [Manage App Audience](https://support.google.com/cloud/answer/15549945), [expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- Las apps sin verificar admiten 100 usuarios nuevos en total durante toda la vida del proyecto, sin posibilidad de reiniciar el contador. La pantalla de «app no verificada» aparece si los scopes pedidos no coinciden con los declarados. — [Unverified apps](https://support.google.com/cloud/answer/7454865)
- Scopes restringidos de Gmail: verificación de scopes restringidos, más una evaluación de seguridad si los datos se guardan o transmiten en servidores. — [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- `drive.file` solo da acceso a ficheros creados por la app o elegidos con el Picker; `drive.readonly` es restringido. — [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- Con scopes de Gmail, el refresh token deja de valer si cambia la contraseña. Hay un tope de 100 refresh tokens por cuenta y client ID. — [expiration](https://developers.google.com/identity/protocols/oauth2#expiration)

⚠️ Sin verificar:
- Que la ruta `/callback` se acepte en el loopback de un cliente Desktop.
- Que una app 100 % local, sin servidor, quede fuera de la evaluación de seguridad de los scopes restringidos.
- Que una app «In production» sin verificar pueda pedir scopes restringidos (y así evitar la caducidad de 7 días).
- La clasificación (sensible o no) de `calendar.events.readonly`, `calendar.calendarlist.readonly` y `calendar.events.owned`.

### Una sola app de Google para los tres conectores

Basta un proyecto y un cliente «Desktop app». Pega el Client ID/secret en uno
de los conectores de Google y, en los otros, pulsa «Usar la misma app OAuth que
…» (`POST /api/connectors-client-group/google`). Solo se copian el Client ID
(SQLite) y el secret (llavero, un ítem por conector); los tokens siguen siendo
uno por conector, cada uno con su propio consentimiento y sus scopes. Al
desconectar un conector se borra solo su token. ⚠️ Revocar el acceso en
myaccount.google.com revoca los tres a la vez (es por proyecto).

## Microsoft (Entra) — scopes mínimos

Lectura: `offline_access User.Read Mail.Read Calendars.Read`. Escritura: `Mail.Send Calendars.ReadWrite`.

Verificado:
- Requisitos: cuenta de Azure con suscripción activa (vale la gratuita), rol Application Developer como mínimo y un tenant. Tipos de cuenta, incluido «Any Entra ID Tenant + Personal Microsoft accounts». — [quickstart-register-app](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
- Plataforma «Mobile and desktop applications» con redirect personalizado. — [how-to-add-redirect-uri](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri)
- En `localhost` se ignora el puerto y la ruta distingue los redirects; no deben registrarse varios que solo cambien de puerto. `[::1]` no está soportado. Microsoft recomienda 127.0.0.1, pero el redirect http con 127.0.0.1 solo se añade desde el manifiesto (`replyUrlsWithType`). — [reply-url](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)
- Los clientes públicos (nativos) no deben usar secretos. `offline_access` da el refresh token, que se rota. — [auth code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)

⚠️ Sin verificar:
- Que con «Single tenant» falle el endpoint `common` (en ese caso habría que permitir configurar el tenant).
- Que «Allow public client flows» no haga falta para authorization code con PKCE.
- Las restricciones de consentimiento de las organizaciones para apps multitenant sin verificar.

## Slack — scopes mínimos (solo de usuario)

Lectura: `channels:read channels:history users:read`. Escritura: `chat:write`.
Flujo: `https://slack.com/oauth/v2_user/authorize` (scopes separados por comas) y después `oauth.v2.user.access`, que devuelve el `access_token` de usuario en el primer nivel.

Verificado:
- PKCE: activarlo es irreversible (convierte la app en cliente público), los refresh tokens caducan a los 30 días y los redirects de escritorio (`localhost` con PKCE) no pueden pedir scopes de bot. Se puede configurar por manifiesto (`pkce_enabled`). — [using-pkce](https://docs.slack.dev/authentication/using-pkce), [changelog](https://docs.slack.dev/changelog/2026/03/30/pkce/)
- El redirect tiene que coincidir con el Redirect URL o ser un subdirectorio suyo. El ejemplo de Slack marca como inválido un puerto distinto. — [installing-with-oauth](https://docs.slack.dev/authentication/installing-with-oauth)
- `oauth.v2.user.access`: la respuesta es estándar y acepta `code_verifier` y `refresh_token`. — [método](https://docs.slack.dev/reference/methods/oauth.v2.user.access)

⚠️ Sin verificar:
- Que `http://127.0.0.1` cuente como redirect de escritorio (solo está documentado `localhost`).
- Que el administrador del workspace tenga que aprobar la app.
- Que la distribución pública quede bloqueada con un redirect http.
- Que la cadena de refresh tokens rotados dure más de 30 días si se usa.

## Notion — solo token (OAuth no implementado)

El conector de Notion usa la conexión con token que ya existe (vista «Conexiones», con un PAT o un token de
conexión interna). **No hay flujo OAuth para Notion**: el intercambio del código exige HTTP Basic con un cuerpo
JSON, y `connector_oauth.complete()` solo envía `application/x-www-form-urlencoded`. Lo que sigue es la
investigación por si se añade más adelante; la guía de la app ya no ofrece este camino.

Verificado:
- Conexión pública: el redirect, el installation scope (no se puede cambiar después) y las capabilities se fijan al crearla. El client ID y el client secret están en Configuration. — [public-connections](https://developers.notion.com/guides/get-started/public-connections)
- `authorize?owner=user&client_id&redirect_uri&response_type=code&state`. El intercambio va con HTTP Basic `client_id:client_secret` y un cuerpo **JSON**. `redirect_uri` es obligatorio si se envió en la URL. La respuesta trae `access_token`, `refresh_token`, `workspace_name`… Al renovar llega un par nuevo, y existe `/v1/oauth/revoke`. Hace falta la cabecera `Notion-Version`. — [authorization](https://developers.notion.com/guides/get-started/authorization), [create-a-token](https://developers.notion.com/reference/create-a-token), [refresh](https://developers.notion.com/reference/refresh-a-token), [revoke](https://developers.notion.com/reference/revoke-token)
- PAT: caduca en un año como máximo, y en el plan Free solo lo crean los owners. — [personal-access-tokens](https://developers.notion.com/guides/get-started/personal-access-tokens)

Guardado del secret: `connector_tokens.save_client_secret`, solo en el llavero (`connector-client-secret:notion`). Sin
llavero se rechaza (`TokenStoreUnavailable`). Nunca va a SQLite, config.json ni a la auditoría (redactado).

⚠️ Sin verificar:
- Que Notion acepte `http://localhost:<puerto>`.
- Que el flujo funcione antes de enviar la conexión a revisión: la guía dice que la «Authorization URL» del portal se rellena tras enviarla.
- Que no haya PKCE en la API REST pública: solo está documentado para Notion MCP.

## Probar en el Mac (nada de esto se ha probado todavía con apps reales)

1. Abrir `/connectors` en la app Tauri, en ES y en EN: la tarjeta de la guía se despliega por proveedor y los enlaces abren el navegador del sistema, no el webview.
2. Seguir la guía de Google con una cuenta de prueba y confirmar los pasos marcados con ⚠️, sobre todo la ruta `/callback`.
3. Comprobar en la consola de cada proveedor que los nombres de los menús siguen siendo los de la guía.
