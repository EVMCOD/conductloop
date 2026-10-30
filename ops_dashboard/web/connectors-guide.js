// "Conectores" page — step-by-step BYOK guide: how to create your own OAuth
// app at Google, Microsoft, Slack and Notion (decision C1: in v1 every user
// brings the client ID of an OAuth app they created themselves).
//
// Static content only (no API calls). Same security rules as connectors.js:
// textContent / createTextNode only; links are fixed https URLs from this file
// and open with rel="noopener noreferrer".
//
// Every step was checked against the provider's official docs (see `sources`
// and docs/connectors/oauth-byok-guide.md). Anything that could not be verified
// there carries `unverified: true` and is shown with ⚠️.
//
// The redirect URIs below are the contract the native connectors implement:
//   Google    http://127.0.0.1:<free port>/callback   (Desktop client, nothing to register)
//   Microsoft http://localhost/callback               (port ignored by Entra for localhost)
//   Slack     http://localhost:47823/callback         (fixed port: Slack matches it exactly)
//   Notion    http://localhost:47824/callback         (fixed port)
// tests/test_connectors_guide.py pins these values.
(function () {
  'use strict';

  var ES = /^es\b/i.test((navigator && navigator.language) || '');
  var SLACK_PORT = 47823;
  var NOTION_PORT = 47824;

  var UI = {
    es: {
      title: 'Guía: crea tu propia app OAuth (BYOK)',
      intro: 'En esta versión cada conector usa el client ID de una app OAuth que creas tú en tu propia cuenta del proveedor. ConductLoop no tiene servidor ni app compartida: el token va del proveedor a este ordenador y se guarda en el llavero.',
      legend: '⚠️ = paso que no se ha podido confirmar en la documentación oficial del proveedor. Compruébalo en su consola antes de fiarte.',
      steps: 'Pasos', redirect: 'Redirect (URI de redirección)', scopes: 'Permisos mínimos (scopes)',
      read: 'lectura', write: 'escritura (más adelante, siempre con confirmación)', warnings: 'Advertencias',
      sources: 'Documentación oficial', console: 'Abrir la consola del proveedor', manifest: 'Manifiesto para pegar'
    },
    en: {
      title: 'Guide: create your own OAuth app (BYOK)',
      intro: 'In this version every connector uses the client ID of an OAuth app you create in your own provider account. ConductLoop has no server and no shared app: the token goes from the provider to this computer and is stored in the keychain.',
      legend: '⚠️ = step that could not be confirmed in the provider\u2019s official documentation. Check it in their console before relying on it.',
      steps: 'Steps', redirect: 'Redirect URI', scopes: 'Minimal permissions (scopes)',
      read: 'read', write: 'write (later, always with confirmation)', warnings: 'Warnings',
      sources: 'Official documentation', console: 'Open the provider console', manifest: 'Manifest to paste'
    }
  };

  // ── Guide content ─────────────────────────────────────────────────────────
  var GUIDE = [
    {
      id: 'google',
      name: 'Google (Calendar, Gmail, Drive)',
      console: 'https://console.cloud.google.com/auth/clients',
      redirect: {
        es: 'No se registra ninguno. Los clientes «Desktop app» aceptan el loopback http://127.0.0.1:<puerto>; ConductLoop abre un puerto libre en cada conexión y usa http://127.0.0.1:<puerto>/callback.',
        en: 'Nothing to register. "Desktop app" clients accept the loopback http://127.0.0.1:<port>; ConductLoop opens a free port on each connection and uses http://127.0.0.1:<port>/callback.'
      },
      steps: [
        { es: 'En Google Cloud Console crea un proyecto nuevo solo para ConductLoop (o elige uno tuyo).',
          en: 'In Google Cloud Console create a new project just for ConductLoop (or pick one of yours).' },
        { es: 'APIs y servicios › Biblioteca: habilita solo las API que vayas a usar: «Google Calendar API», «Gmail API» y/o «Google Drive API».',
          en: 'APIs & Services › Library: enable only the APIs you will use: "Google Calendar API", "Gmail API" and/or "Google Drive API".' },
        { es: 'Google Auth Platform › Branding › «Get started»: nombre de la app (p. ej. «ConductLoop personal»), email de soporte, Audience = External si usas una cuenta @gmail.com (Internal solo si tienes Google Workspace y la vas a usar dentro de tu organización), email de contacto, acepta la User Data Policy y pulsa Create.',
          en: 'Google Auth Platform › Branding › "Get started": app name (e.g. "ConductLoop personal"), support email, Audience = External for an @gmail.com account (Internal only with Google Workspace and use inside your organization), contact email, accept the User Data Policy and click Create.' },
        { es: 'Audience › Test users › Add users: añade tu propia cuenta de Google. Deja el estado de publicación en «Testing».',
          en: 'Audience › Test users › Add users: add your own Google account. Leave the publishing status on "Testing".' },
        { es: 'Data Access › Add or remove scopes: añade exactamente los scopes de la lista de abajo y nada más. Si el código pide un scope que no está aquí, Google muestra la pantalla de «app no verificada».',
          en: 'Data Access › Add or remove scopes: add exactly the scopes listed below and nothing else. If the code requests a scope that is not declared here, Google shows the "unverified app" screen.' },
        { es: 'Clients › Create client › Application type = «Desktop app» › nombre › Create. Copia el Client ID y el Client secret.',
          en: 'Clients › Create client › Application type = "Desktop app" › name › Create. Copy the Client ID and the Client secret.' },
        { es: 'ConductLoop usa la ruta /callback en el loopback (http://127.0.0.1:<puerto>/callback). Google documenta http://127.0.0.1:<puerto> sin decir nada de la ruta.',
          en: 'ConductLoop uses the /callback path on the loopback (http://127.0.0.1:<port>/callback). Google documents http://127.0.0.1:<port> and says nothing about the path.', unverified: true },
        { es: 'En esta página, en uno de los conectores de Google, pega el Client ID y el Client secret y pulsa Guardar. Después, «Conectar (solo lectura)». El secret se guarda solo en el llavero de macOS.',
          en: 'On this page, in one of the Google connectors, paste the Client ID and the Client secret and click Save. Then "Connect (read-only)". The secret is stored only in the macOS keychain.' },
        { es: 'Calendar, Gmail y Drive pueden usar el mismo cliente: en los otros dos conectores pulsa «Usar la misma app OAuth que …». Se copian solo el Client ID y el secret; cada conector pide su propio permiso y guarda su propio token.',
          en: 'Calendar, Gmail and Drive can use the same client: on the other two connectors click "Use the same OAuth app as …". Only the Client ID and secret are copied; each connector asks for its own consent and keeps its own token.' }
      ],
      scopes: [
        { scope: 'https://www.googleapis.com/auth/calendar.calendarlist.readonly', kind: 'read', es: 'Calendar: lista de tus calendarios.', en: 'Calendar: list of your calendars.' },
        { scope: 'https://www.googleapis.com/auth/calendar.events.readonly', kind: 'read', es: 'Calendar: ver eventos.', en: 'Calendar: view events.' },
        { scope: 'https://www.googleapis.com/auth/gmail.readonly', kind: 'read', es: 'Gmail: leer correos (scope RESTRINGIDO).', en: 'Gmail: read email (RESTRICTED scope).' },
        { scope: 'https://www.googleapis.com/auth/drive.readonly', kind: 'read', es: 'Drive: ver y descargar tus ficheros (scope RESTRINGIDO).', en: 'Drive: view and download your files (RESTRICTED scope).' },
        { scope: 'https://www.googleapis.com/auth/calendar.events.owned', kind: 'write', es: 'Calendar: crear y cambiar eventos en calendarios tuyos.', en: 'Calendar: create and change events on calendars you own.' },
        { scope: 'https://www.googleapis.com/auth/gmail.send', kind: 'write', es: 'Gmail: enviar correo en tu nombre (sensible).', en: 'Gmail: send email on your behalf (sensitive).' },
        { scope: 'https://www.googleapis.com/auth/drive.file', kind: 'write', es: 'Drive: solo los ficheros que cree ConductLoop (no sensible).', en: 'Drive: only files ConductLoop creates (non-sensitive).' }
      ],
      warnings: [
        { es: 'Gmail: gmail.readonly (igual que gmail.compose, gmail.modify y gmail.metadata) es un scope RESTRINGIDO. Para publicar una app con él Google exige la verificación de scopes restringidos y, si los datos se guardan o transmiten en servidores, una evaluación de seguridad. Con tu propia app en «Testing» no hace falta verificarla, pero verás la pantalla de «app no verificada».',
          en: 'Gmail: gmail.readonly (like gmail.compose, gmail.modify and gmail.metadata) is a RESTRICTED scope. Publishing an app with it requires restricted-scope verification and, if data is stored or transmitted on servers, a security assessment. Your own app in "Testing" needs no verification, but you will see the "unverified app" screen.' },
        { es: 'ConductLoop no envía tus correos a ningún servidor (todo se queda en este ordenador). Si eso basta para que Google no exija la evaluación de seguridad a una app 100 % local no está documentado.',
          en: 'ConductLoop does not send your email to any server (everything stays on this computer). Whether that exempts a 100 % local app from the security assessment is not documented.', unverified: true },
        { es: 'Tope de usuarios: en «Testing» caben como máximo 100 usuarios de prueba. Una app sin verificar que muestra la pantalla de «app no verificada» admite 100 usuarios nuevos en total durante toda la vida del proyecto, y ese tope no se puede reiniciar. Por eso cada usuario crea su propia app.',
          en: 'User cap: "Testing" allows at most 100 test users. An unverified app that shows the "unverified app" screen accepts 100 new users in total over the whole life of the project, and the cap cannot be reset. That is why every user creates their own app.' },
        { es: 'En «Testing», la autorización (y el refresh token) caduca a los 7 días del consentimiento: tendrás que volver a pulsar «Conectar» cada semana.',
          en: 'In "Testing", the authorization (and the refresh token) expires 7 days after consent: you will have to click "Connect" again every week.' },
        { es: 'Si pasas la app a «In production» sin verificarla, Google sigue mostrando el aviso de app no verificada y aplica el tope de 100 usuarios. La caducidad de 7 días solo está documentada para «Testing»; que Google deje usar scopes restringidos en producción sin verificar no está confirmado.',
          en: 'If you move the app to "In production" without verification, Google keeps showing the unverified-app warning and applies the 100-user cap. The 7-day expiry is only documented for "Testing"; whether Google lets an unverified production app use restricted scopes is not confirmed.', unverified: true },
        { es: 'Drive: drive.readonly es RESTRINGIDO. La alternativa no sensible, drive.file, solo da acceso a ficheros que cree la app o que elijas con el Google Picker, que ConductLoop no tiene. Para leer tus documentos existentes hace falta drive.readonly.',
          en: 'Drive: drive.readonly is RESTRICTED. The non-sensitive alternative, drive.file, only covers files the app creates or that you pick with the Google Picker, which ConductLoop does not have. Reading your existing documents needs drive.readonly.' },
        { es: 'Con scopes de Gmail, el refresh token deja de funcionar si cambias la contraseña de Google. Un administrador de Google Workspace puede bloquear la app (error admin_policy_enforced).',
          en: 'With Gmail scopes, the refresh token stops working if you change your Google password. A Google Workspace admin can block the app (error admin_policy_enforced).' },
        { es: 'El client secret de un cliente Desktop no es confidencial según Google (no protege nada por sí solo). Aun así ConductLoop lo guarda en el llavero y nunca en la base de datos.',
          en: 'Google says a Desktop client\u2019s secret is not confidential (it protects nothing on its own). ConductLoop still keeps it in the keychain, never in the database.' }
      ],
      sources: [
        'https://developers.google.com/identity/protocols/oauth2/native-app',
        'https://developers.google.com/workspace/guides/configure-oauth-consent',
        'https://support.google.com/cloud/answer/15549945',
        'https://support.google.com/cloud/answer/7454865',
        'https://developers.google.com/identity/protocols/oauth2#expiration',
        'https://developers.google.com/workspace/gmail/api/auth/scopes',
        'https://developers.google.com/workspace/calendar/api/auth',
        'https://developers.google.com/workspace/drive/api/guides/api-specific-auth'
      ]
    },
    {
      id: 'microsoft',
      name: 'Microsoft 365 (Outlook: correo y calendario)',
      console: 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
      redirect: {
        es: 'http://localhost/callback, en la plataforma «Mobile and desktop applications». Entra ignora el puerto en los redirects de localhost, así que ConductLoop puede usar un puerto libre (http://localhost:<puerto>/callback).',
        en: 'http://localhost/callback, on the "Mobile and desktop applications" platform. Entra ignores the port on localhost redirects, so ConductLoop can use a free port (http://localhost:<port>/callback).'
      },
      steps: [
        { es: 'Requisito de Microsoft: una cuenta de Azure (la gratuita sirve) con rol de al menos «Application Developer» y un tenant (vale el «Default Directory»).',
          en: 'Microsoft prerequisite: an Azure account (the free one works) with at least the "Application Developer" role and a tenant (the "Default Directory" is fine).' },
        { es: 'Microsoft Entra admin center › Entra ID › App registrations › New registration. Pon un nombre (p. ej. «ConductLoop personal»).',
          en: 'Microsoft Entra admin center › Entra ID › App registrations › New registration. Enter a name (e.g. "ConductLoop personal").' },
        { es: 'Supported account types = «Any Entra ID Tenant + Personal Microsoft accounts». ConductLoop usa el endpoint «common», que acepta cuentas personales (Outlook.com, Hotmail) y de trabajo.',
          en: 'Supported account types = "Any Entra ID Tenant + Personal Microsoft accounts". ConductLoop uses the "common" endpoint, which accepts personal (Outlook.com, Hotmail) and work accounts.' },
        { es: 'Si eliges «Single tenant», el endpoint «common» no te dejará iniciar sesión.',
          en: 'If you pick "Single tenant", the "common" endpoint will not let you sign in.', unverified: true },
        { es: 'Register. En Overview copia el «Application (client) ID».',
          en: 'Register. On Overview copy the "Application (client) ID".' },
        { es: 'Authentication › Add Redirect URI › «Mobile and desktop applications» › Custom redirect URI: http://localhost/callback › Configure.',
          en: 'Authentication › Add Redirect URI › "Mobile and desktop applications" › Custom redirect URI: http://localhost/callback › Configure.' },
        { es: 'No crees ningún secreto en «Certificates & secrets»: es un cliente público, y Microsoft dice que los clientes nativos no deben usar secretos.',
          en: 'Do not create a secret under "Certificates & secrets": this is a public client, and Microsoft says native clients must not use secrets.' },
        { es: 'No hace falta activar «Allow public client flows»: ConductLoop usa authorization code + PKCE, no device code ni contraseña.',
          en: 'You do not need "Allow public client flows": ConductLoop uses authorization code + PKCE, not device code or password.', unverified: true },
        { es: 'API permissions › Add a permission › Microsoft Graph › Delegated permissions: offline_access, User.Read, Mail.Read y Calendars.Read (los de escritura, más adelante).',
          en: 'API permissions › Add a permission › Microsoft Graph › Delegated permissions: offline_access, User.Read, Mail.Read and Calendars.Read (write ones later).' },
        { es: 'En esta página, en el conector de Microsoft 365, pega el client ID, Guardar y «Conectar (solo lectura)». No hay secret que pegar.',
          en: 'On this page, in the Microsoft 365 connector, paste the client ID, Save and "Connect (read-only)". There is no secret to paste.' }
      ],
      scopes: [
        { scope: 'offline_access', kind: 'read', es: 'Refresh token (sin él habría que reconectar cada hora).', en: 'Refresh token (without it you would reconnect every hour).' },
        { scope: 'User.Read', kind: 'read', es: 'Tu perfil (para mostrar la cuenta conectada).', en: 'Your profile (to show the connected account).' },
        { scope: 'Mail.Read', kind: 'read', es: 'Leer tu correo.', en: 'Read your mail.' },
        { scope: 'Calendars.Read', kind: 'read', es: 'Leer tus calendarios.', en: 'Read your calendars.' },
        { scope: 'Mail.Send', kind: 'write', es: 'Enviar correo en tu nombre.', en: 'Send mail as you.' },
        { scope: 'Calendars.ReadWrite', kind: 'write', es: 'Crear y cambiar eventos.', en: 'Create and change events.' }
      ],
      warnings: [
        { es: 'El portal no deja añadir un redirect http con 127.0.0.1 (solo editando replyUrlsWithType en el manifiesto). Por eso Microsoft usa localhost y no 127.0.0.1 como el resto.',
          en: 'The portal does not allow an http redirect with 127.0.0.1 (only by editing replyUrlsWithType in the manifest). That is why Microsoft uses localhost instead of 127.0.0.1 like the others.' },
        { es: 'No registres varios redirects de localhost que solo cambien de puerto: Entra elige uno al azar.',
          en: 'Do not register several localhost redirects that only differ by port: Entra picks one arbitrarily.' },
        { es: 'Microsoft recomienda 127.0.0.1 frente a localhost porque algunos cortafuegos rompen localhost. Si te pasa, avísanos.',
          en: 'Microsoft recommends 127.0.0.1 over localhost because some firewalls break localhost. If that happens to you, let us know.' },
        { es: 'Cuentas de trabajo o de centro educativo: algunos permisos necesitan consentimiento de un administrador, y la organización puede impedir que los usuarios den consentimiento a apps que no están verificadas.',
          en: 'Work or school accounts: some permissions need admin consent, and the organization may stop users from consenting to unverified apps.', unverified: true },
        { es: 'Microsoft rota el refresh token en cada renovación. ConductLoop guarda el nuevo en el llavero.',
          en: 'Microsoft rotates the refresh token on every renewal. ConductLoop saves the new one in the keychain.' }
      ],
      sources: [
        'https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app',
        'https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-redirect-uri',
        'https://learn.microsoft.com/en-us/entra/identity-platform/reply-url',
        'https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow'
      ]
    },
    {
      id: 'slack',
      name: 'Slack',
      console: 'https://api.slack.com/apps',
      redirect: {
        es: 'http://localhost:' + SLACK_PORT + '/callback, exacto. Slack compara el redirect entero, puerto incluido, así que este conector usa siempre el puerto ' + SLACK_PORT + '.',
        en: 'http://localhost:' + SLACK_PORT + '/callback, exactly. Slack compares the whole redirect, port included, so this connector always uses port ' + SLACK_PORT + '.'
      },
      manifest: [
        'display_information:',
        '  name: ConductLoop personal',
        'oauth_config:',
        '  redirect_urls:',
        '    - http://localhost:' + SLACK_PORT + '/callback',
        '  scopes:',
        '    user:',
        '      - channels:read',
        '      - channels:history',
        '      - users:read',
        '      - chat:write  # solo si vas a publicar / only if you will post',
        '  pkce_enabled: true',
        'settings:',
        '  token_rotation_enabled: true'
      ],
      steps: [
        { es: 'api.slack.com/apps › Create New App › «From a manifest» › elige tu workspace › pega el manifiesto de abajo (YAML) › Create.',
          en: 'api.slack.com/apps › Create New App › "From a manifest" › pick your workspace › paste the manifest below (YAML) › Create.' },
        { es: 'OAuth & Permissions: comprueba que el Redirect URL es exactamente http://localhost:' + SLACK_PORT + '/callback, que PKCE está activado y que solo hay «User Token Scopes» (ningún «Bot Token Scope»).',
          en: 'OAuth & Permissions: check that the Redirect URL is exactly http://localhost:' + SLACK_PORT + '/callback, that PKCE is enabled and that there are only "User Token Scopes" (no "Bot Token Scopes").' },
        { es: 'Basic Information › App Credentials: copia solo el Client ID. Con PKCE no hace falta el Client Secret; no lo pegues en ningún sitio.',
          en: 'Basic Information › App Credentials: copy only the Client ID. With PKCE the Client Secret is not needed; do not paste it anywhere.' },
        { es: 'No pulses «Install to Workspace» en la web de Slack: la conexión la hace ConductLoop con el flujo de usuario (oauth/v2_user/authorize + oauth.v2.user.access).',
          en: 'Do not click "Install to Workspace" on the Slack site: ConductLoop makes the connection with the user flow (oauth/v2_user/authorize + oauth.v2.user.access).' },
        { es: 'En esta página, en el conector de Slack, pega el Client ID, Guardar y «Conectar (solo lectura)». El puerto ' + SLACK_PORT + ' tiene que estar libre en ese momento.',
          en: 'On this page, in the Slack connector, paste the Client ID, Save and "Connect (read-only)". Port ' + SLACK_PORT + ' must be free at that moment.' }
      ],
      scopes: [
        { scope: 'channels:read', kind: 'read', es: 'Lista de canales públicos.', en: 'List of public channels.' },
        { scope: 'channels:history', kind: 'read', es: 'Mensajes de los canales públicos en los que estás.', en: 'Messages in the public channels you are in.' },
        { scope: 'users:read', kind: 'read', es: 'Nombres de las personas (para mostrar autores).', en: 'People\u2019s names (to show authors).' },
        { scope: 'chat:write', kind: 'write', es: 'Publicar mensajes como tú.', en: 'Post messages as you.' }
      ],
      warnings: [
        { es: 'Puerto fijo: Slack exige que el redirect coincida exactamente, puerto incluido. Si otro programa ocupa el ' + SLACK_PORT + ', la conexión falla: ciérralo y vuelve a intentarlo.',
          en: 'Fixed port: Slack requires an exact redirect match, port included. If another program is using ' + SLACK_PORT + ', the connection fails: close it and try again.' },
        { es: 'Solo permisos de usuario: con un redirect de escritorio (localhost con PKCE) Slack no deja pedir permisos de bot. El conector actúa como tú, y lo que publique aparecerá con tu nombre.',
          en: 'User permissions only: with a desktop redirect (localhost with PKCE) Slack does not allow bot scopes. The connector acts as you, and whatever it posts shows your name.' },
        { es: 'Activar PKCE marca la app como cliente público y no tiene vuelta atrás (solo el soporte de Slack lo deshace). Usa una app dedicada a ConductLoop.',
          en: 'Enabling PKCE marks the app as a public client and cannot be undone (only Slack support can revert it). Use an app dedicated to ConductLoop.' },
        { es: 'Con PKCE, los refresh tokens caducan a los 30 días. Si ConductLoop no se usa en 30 días, tendrás que volver a conectar.',
          en: 'With PKCE, refresh tokens expire after 30 days. If ConductLoop is not used for 30 days, you will have to reconnect.' },
        { es: 'Slack documenta localhost como redirect de escritorio, pero no 127.0.0.1. Por eso este conector usa localhost.',
          en: 'Slack documents localhost as a desktop redirect, but not 127.0.0.1. That is why this connector uses localhost.' },
        { es: 'El administrador del workspace puede exigir aprobar la app antes de que puedas conectarla.',
          en: 'The workspace admin may require approving the app before you can connect it.', unverified: true },
        { es: 'Con un redirect http no se puede activar la distribución pública de la app. No hace falta: la app es solo tuya.',
          en: 'Public distribution cannot be enabled with an http redirect. You do not need it: the app is just for you.', unverified: true }
      ],
      sources: [
        'https://docs.slack.dev/authentication/using-pkce',
        'https://docs.slack.dev/changelog/2026/03/30/pkce/',
        'https://docs.slack.dev/authentication/installing-with-oauth',
        'https://docs.slack.dev/reference/methods/oauth.v2.user.access',
        'https://docs.slack.dev/authentication/using-token-rotation'
      ]
    },
    {
      id: 'notion',
      name: 'Notion',
      console: 'https://app.notion.com/developers/connections',
      redirect: {
        es: 'No aplica: Notion se conecta con un token, sin redirect.',
        en: 'Not applicable: Notion connects with a token, no redirect.'
      },
      steps: [
        { es: 'El conector de Notion usa un token, no OAuth: reutiliza la conexión de la vista «Conexiones» › Notion, con un token personal (PAT) o de conexión interna, sin duplicarlo.',
          en: 'The Notion connector uses a token, not OAuth: it reuses the "Connections" view › Notion connection, with a personal access token (PAT) or an internal connection token, without duplicating it.' },
        { es: 'Para un token de conexión interna: Developer portal › Build › Internal connections › New connection, en tu workspace.',
          en: 'For an internal connection token: Developer portal › Build › Internal connections › New connection, in your workspace.' },
        { es: 'Capabilities: «Read content»; añade «Insert content» / «Update content» solo si quieres escrituras. Información de usuarios: sin email. Copia el token.',
          en: 'Capabilities: "Read content"; add "Insert content" / "Update content" only if you want writes. User information: without email. Copy the token.' },
        { es: 'En Notion, en cada página que quieras compartir: ··· › Conexiones › añade tu conexión. Solo verá esas páginas y sus hijas.',
          en: 'In Notion, on each page you want to share: ··· › Connections › add your connection. It only sees those pages and their children.' },
        { es: 'En ConductLoop: «Conexiones» › Notion › pega el token. Después, en esta página, el conector de Notion aparece como conectado.',
          en: 'In ConductLoop: "Connections" › Notion › paste the token. The Notion connector on this page then shows as connected.' },
        { es: 'OAuth con una conexión pública todavía no está disponible en ConductLoop (Notion exige un intercambio en JSON con HTTP Basic que el marco OAuth aún no hace): no crees una conexión pública para esto.',
          en: 'OAuth with a public connection is not available in ConductLoop yet (Notion requires a JSON exchange with HTTP Basic that the OAuth framework does not do yet): do not create a public connection for this.' }
      ],
      scopes: [
        { scope: 'Read content', kind: 'read', es: 'Notion no usa scopes: los permisos son las «capabilities» de la conexión y las páginas que compartas con ella.', en: 'Notion has no scopes: permissions are the connection\u2019s "capabilities" plus the pages you share with it.' },
        { scope: 'Insert content / Update content', kind: 'write', es: 'Añadir a una página y crear subpáginas (cada escritura espera tu confirmación).', en: 'Append to a page and create sub-pages (each write waits for your confirmation).' }
      ],
      warnings: [
        { es: 'El token da acceso a todo lo que compartas con la conexión: trátalo como una contraseña, no lo compartas ni lo subas a git.',
          en: 'The token gives access to everything you share with the connection: treat it like a password, do not share it or commit it.' },
        { es: 'Las escrituras siguen desactivadas hasta que las permitas, y cada una espera tu confirmación.',
          en: 'Writes stay off until you allow them, and each one waits for your confirmation.' },
        { es: 'Si usas un token personal (PAT): caduca como mucho al año, y en el plan Free solo pueden crearlo los propietarios del workspace.',
          en: 'With a personal access token (PAT): it expires after at most one year, and on the Free plan only workspace owners can create one.' }
      ],
      sources: [
        'https://developers.notion.com/guides/get-started/public-connections',
        'https://developers.notion.com/guides/get-started/authorization',
        'https://developers.notion.com/reference/create-a-token',
        'https://developers.notion.com/reference/refresh-a-token',
        'https://developers.notion.com/reference/revoke-token',
        'https://developers.notion.com/guides/get-started/personal-access-tokens'
      ]
    }
  ];

  // ── Rendering (textContent only) ─────────────────────────────────────────
  var T = ES ? UI.es : UI.en;
  var LANG = ES ? 'es' : 'en';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function extLink(url, label) {
    var a = el('a', 'link', label || url);
    if (!/^https:\/\//i.test(url)) return el('span', 'mono', url);
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    return a;
  }

  function mark(item) { return (item.unverified ? '⚠️ ' : '') + item[LANG]; }

  function renderProvider(p) {
    var d = el('details', 'guide');
    d.setAttribute('data-guide', p.id);
    d.appendChild(el('summary', null, p.name));

    d.appendChild(el('h4', null, T.redirect));
    var r = el('p', 'mono', p.redirect[LANG]);
    r.setAttribute('data-testid', 'guide-redirect');
    d.appendChild(r);

    d.appendChild(el('h4', null, T.steps));
    var ol = el('ol', 'steps');
    p.steps.forEach(function (s) {
      var li = el('li', s.unverified ? 'unverified' : null, mark(s));
      ol.appendChild(li);
    });
    d.appendChild(ol);

    if (p.manifest) {
      d.appendChild(el('h4', null, T.manifest));
      d.appendChild(el('pre', 'payload mono', p.manifest.join('\n')));
    }

    d.appendChild(el('h4', null, T.scopes));
    var ul = el('ul', 'list compact');
    p.scopes.forEach(function (s) {
      var li = el('li');
      li.appendChild(el('span', 'badge mono' + (s.kind === 'write' ? ' write' : ''), s.scope));
      li.appendChild(document.createTextNode(' ' + (s.kind === 'write' ? T.write : T.read) + ' — ' + s[LANG]));
      ul.appendChild(li);
    });
    d.appendChild(ul);

    d.appendChild(el('h4', null, T.warnings));
    var wl = el('ul', 'warnings');
    p.warnings.forEach(function (w) { wl.appendChild(el('li', w.unverified ? 'unverified' : null, mark(w))); });
    d.appendChild(wl);

    var links = el('p', 'hint');
    links.appendChild(extLink(p.console, T.console));
    d.appendChild(links);
    d.appendChild(el('h4', null, T.sources));
    var sl = el('ul', 'list compact');
    p.sources.forEach(function (u) { var li = el('li'); li.appendChild(extLink(u)); sl.appendChild(li); });
    d.appendChild(sl);
    return d;
  }

  function render() {
    var card = document.getElementById('guide-card');
    if (!card) return;
    while (card.firstChild) card.removeChild(card.firstChild);
    card.appendChild(el('h2', null, T.title));
    card.appendChild(el('p', 'hint', T.intro));
    card.appendChild(el('p', 'hint', T.legend));
    GUIDE.forEach(function (p) { card.appendChild(renderProvider(p)); });
  }

  // Exposed read-only for the connector cards and the browser test.
  window.ConductLoopGuide = { providers: GUIDE.map(function (p) { return p.id; }), slackPort: SLACK_PORT, notionPort: NOTION_PORT };
  document.addEventListener('DOMContentLoaded', render);
})();
