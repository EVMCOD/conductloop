# ConductLoop — Changelog

User-facing changes to ConductLoop (the desktop app and its local server), newest first.

## 1.2.14 — 2026-11-05 — First run that a stranger can finish

Launch polish on top of 1.2.13: a stranger on a clean Mac can finish first
run without the app hiding the key, the tour matches the product that ships,
links and downloads work in the desktop window, the in-app browser is usable,
and Mac / connector smoke checks cover what does not need an account.

### Connectors that need no account: end-to-end smoke

- **`scripts/smoke-connectors.py`.** Drives a running server (dev or the
  packaged sidecar) through the Conectores API: MCP client over stdio and over
  Streamable HTTP (a fake server on a real loopback socket, JSON + SSE), and
  optionally RSS against a public feed. Checks read tools run with untrusted
  results, writes are refused while read-only and only run after
  confirmation, and cleans up its `smoke-*` servers.
- **`tests/test_connectors_no_account_smoke.py`.** The same flows in CI, no
  network: real-socket HTTP MCP with a bearer token, a wrong token is not
  reported as connected and never echoed, RSS sync through the routes with
  recorded feeds.
- **`docs/connectors/no-account-smoke.md`.** How to run it against the
  packaged sidecar with temporary data, and which account each remaining
  connector needs for manual verification.

### Mac checklist (#32): automate what needs no accounts

Issue #32 asked for Mac-only release checks. Most of the non-account work is
now a script and hermetic tests; Keychain, OAuth consoles and live L2 stay on
the issue as a manual list for Enrique.

- **`scripts/mac-checks.sh` / `make mac-checks`.** Port :7777 free check,
  pytest for the app-token guard / boot cookie / macOS minimum sync /
  migration 20–24 / sandbox-exec, optional migration run on a *copy* passed
  as `CONDUCTLOOP_DB_COPY` (refuses the live Application Support path), and a
  printed leftover checklist.
- **`tests/test_issue32_automation.py`.** Descriptions for migrations 20–24
  must match `migrations.py` after apply; migration 20 refuses an unexpected
  `agent_sessions` schema; a WAL-safe `sqlite3` backup copy keeps row counts
  and integrity; on Darwin, `detect_backend()` is `sandbox-exec`, a broken
  Seatbelt profile fails closed, and network / outside-write / `~/.ssh` probes
  are blocked.
- **Windows follow-up:** `test_seatbelt_profile_denies_network_and_sensitive_paths`
  skips the Seatbelt profile content assert off macOS (sandbox-exec only),
  so the non-required `windows-python` job is not tripped by quoted paths.
- Does **not** change `minimumSystemVersion` (already `13.0` on `main`, kept
  in sync by `tests/test_macos_minimum.py`). Confirming or revisiting that
  decision stays with Enrique on #32.

### In-app browser: focus, tab clicks and Save page

Three leftovers from the in-app browser on a packaged debug build:

- **Keyboard focus after «+».** A new blank tab's webview took the keyboard, so
  the next keystrokes went into an empty page instead of the URL bar. Blank
  user tabs now stay hidden until they have a real URL, and «+» focuses the
  URL bar (with `browser_focus_ui`).
- **First click on the tab strip was lost.** `setInterval(refresh)` rebuilt the
  tab DOM between mousedown and click, so WebKit dropped the gesture. The strip
  now acts on `pointerdown`, skips identical rebuilds, and takes focus when the
  pointer enters the chrome.
- **"Send to vault" renamed to "Save page".** Clips are stored in ConductLoop's
  index as untrusted `source=web` documents — they are not written into the
  notes folder. The button, tooltip and success toast say so.
- `tests/test_browser_page.py` guards the label, the tab-strip click path and
  the blank-tab focus behaviour.

### The quick tour describes the current product

- **`/tour` no longer promises retired features.** It talked about "13
  projects", revenue, GA4 traffic and Sentry errors. The five steps now cover
  what ConductLoop is today: bring your own AI key, a team of agents led by a
  CEO, local Markdown notes and opt-in connectors. No invented numbers. A
  small test guards the copy.

### First run without a keychain does not hide the key

Walked a packaged debug build as a stranger (temporary HOME, data dir, no
keychain, empty projects). The welcome page, the no-key answer and a few
error strings were lying to that person.

- **Welcome no longer saves the AI key in plain text.** With no system
  keychain and no passphrase yet, pasting a key on the welcome page wrote it
  into `config.json` and said "Key saved." It now asks for a passphrase on
  that page (or returns 423 so Credentials can open the vault) and stores the
  key only in the encrypted file.
- **The brain reads keys from the encrypted vault.** After the welcome page
  stored a key under a `*_api_key_ref` with no keychain, `get_api_key` only
  looked in the OS keychain, so the brain could not call the provider even
  with the vault unlocked. It now resolves keychain, then vault, then
  plaintext.
- **No-key answers no longer say "nothing matches" next to citations.** When
  retrieval found notes but no single line shared enough words with the
  question, the mock answer contradicted the [N] sources. It now lists the
  related notes and points at the welcome page / Credentials — not at a
  terminal environment variable.
- **Provider errors are in English and point at Credentials**, not at
  "Settings". Setup and Credentials say "this computer" / "system keychain"
  instead of "your Mac" only.
- `tests/test_first_run_friction.py` keeps the refusal of plaintext, the
  encrypted-vault path, the related-notes mock answer and the English
  provider errors in place.

### Links, downloads and drops work in the desktop window

Checked in a packaged debug build: browser APIs that are fine in a browser
did nothing in the desktop window, because wry only acts on them when the
window has a handler, and the main window had none.

- **Links to other sites open in your browser.** `window.open` and
  `target="_blank"` links (chat answers, the status page, the sidebar URLs,
  connector guides) did nothing at all. A plain link to another site
  replaced the whole app with that site, with no way back. Other sites now
  open in the system browser; the app's own pages stay in the window;
  anything else (`file:`, other local ports) is refused.
- **"↓ save" saves the conversation.** It used `window.open`, so it did
  nothing in the app. It is now a download (`Content-Disposition:
  attachment`), saved to the Downloads folder, and a toast says where.
  Every `<a download>` and blob download works the same way.
- **Dragging in the agents org chart drops.** Tauri's file-drop handler took
  every drag, so the page never got its `drop`. It is off for the main
  window; a file dropped on it is refused instead of opening.
- **Copy in the note's AI panel** reports on its own button (it relabelled
  the action button) and shows an error instead of throwing.
- `tests/test_browser_apis_in_app.py` keeps the handlers, the download and
  the bans on `Notification` (always "denied" in the app) and
  `beforeunload` (never shown) in place.

## 1.2.13 — 2026-11-04 — The app asks before it deletes

The desktop window is served over http by the local server, and Tauri v2
overrides `window.confirm` with an **async** call to the dialog plugin. So in
the app `confirm()` returns a **Promise**, not a boolean — and a guard like
`if (!confirm(...)) return;` sees a truthy promise, never fires, and the
destructive action runs **without asking**.

Awaiting the global is not the fix either: the plugin's `message` command
needs the `dialog:allow-message` permission, which this repo grants to
**nobody** on purpose. Without it the awaited promise rejects with an ACL
error instead of resolving `false`.

There were **21 confirmations** in the shell — deleting a todo, an idea, a
document, a memory, a trigger, an agent, a decision, a cost, an approval, a
session task, a claim, a connector, a token, an MCP server, a skill
proposal, a backup restore, a device pairing, a media asset, a channel
binding. Every one of them was a "delete" that the app performed silently in
the desktop app, while the browser asked correctly.

- **Every question is now asked in the page**: a `<dialog>` opened with
  `showModal()`, the same shape `web/sessions.js` already used. No
  permission, no IPC, no capability — and the same behaviour in the browser
  and in the app.
- **`prompt()` is gone too**. The shell does not override it, but wry ships
  no text-input panel at all, so a prompt comes back as an empty string and
  the flow stops without the user ever being asked — a failure nobody
  reports. Text input is a dialog with a field.
- **`alert()` was already a toast**; the pages that had no feedback facility
  of their own now share `web/toast.js`, so a message there is visible too.
- Two tests guard it: no page may ask through a browser global again, and
  the browser tests now fail if a **native** dialog is ever raised — the
  defect is caught at the source instead of in the desktop app.

## 1.2.12 — 2026-11-02 — The app tells you what went wrong

The desktop window is served over http by the local server, and Tauri v2
overrides `window.alert` with an IPC call to the dialog plugin. Without the
permission for that call the message went nowhere: the action appeared to do
nothing and the app looked broken.

There were **47 `alert()`** calls in the shell, so this was not one bad
message but **every** message the app can show — the error of a connection
that cannot be added, a document that cannot be created, a delete that
failed. The desktop app swallowed all of them.

- **Every message the app reports is now visible**: a toast in the page, in
  the app and in a browser, with no dependency on Tauri. An error is marked
  as an error.
- Two tests guard it: the shell must not use `alert()` again, and it must
  stay valid JavaScript (a syntax error there is a blank app, and nothing
  says so).

## 1.2.11 — 2026-11-02 — The window keeps its session

The window gets its session cookie from the boot, and every request it makes
afterwards carries that cookie. The server checks it against the token it
accepts.

Those were **two different tokens**. The boot handed out the one from the
environment — a new one on every launch — while the server checked the
persisted one, which does not change. So from the second launch onwards the
window held a cookie that answered **403 to everything**: the page loaded
with no data, no buttons, nothing.

The session was kept, but the cookie carrying it was the wrong one. The same
failure as before the token was persisted, in another place.

- **The boot hands out the token the server accepts**, so the session
  survives a restart and a fresh launch gets a cookie that works.
- The cookie is still only handed out by the boot, which needs the nonce only
  the app has: no other browser on this computer can get one.

## 1.2.10 — 2026-11-01 — The app survives its own restart

The window keeps its session token in a cookie, and the app handed the
server a **new** token every time it launched. So whenever the server
restarted — an update, a crash, the scheduler — the cookie in the open
window stopped matching and **every request answered 403**.

The frontend turned that into an error, and the page stopped working: no
data, no buttons, nothing. The only way out was to quit the app and open it
again. Reloading was worse: the main page answered 403 too, so the app came
back as a **blank page**.

- **The token is kept between launches** (next to the data, readable only by
  you), so the window that is already open keeps working after the server
  restarts under it.
- **If the session is really lost** — the app was updated under a window
  that stayed open — the app says so instead of failing silently, and tells
  you to reopen it (which boots with a one-time nonce).

The token is what separates the desktop window from any other browser on the
machine, so it is never handed out: a browser that opens the local address
is refused, exactly as before. And the window no longer reloads itself on a
403, because a reload cannot repair it and would end in a blank page.

## 1.2.9 — 2026-11-01 — Your apps are back

The Conectores page showed an empty catalogue in the installed app. Not a bug
in the connectors: **the connectors were never in the binary**.

PyInstaller builds the bundled server with the spec's own directory at the
front of the import path, not the repository root. So the step that collects
the app's modules found **nothing — and returned an empty list without
raising**. The build succeeded, the app shipped, and every module that is
only reached by importing it by name was missing. The connectors register
themselves that way, so all eleven were missing: the app you run had no
connectors at all, and nothing said so.

- **The 11 apps are in the app now**, with their catalogue, their
  descriptions and their section.
- **The build fails instead of shipping a broken app.** If the package
  cannot be found, the build stops there.
- **The connectors no longer register by walking a folder.** A frozen build
  has no folder to walk: the modules live inside the archive.

Nothing else changes: the catalogue, the C-suite agents, the session agent
and the token sign-in work as they did in 1.2.8.

## 1.2.8 — 2026-11-01 — The agent that works in your repo can use your apps

The agent that works in a repository — the one that reads files, runs the test
suite and writes code — had its own tool protocol: the model replies with one
JSON object, `{"tool": ..., "args": {...}}`. That protocol knew nothing about
the connectors. An agent doing research could not look up an issue in GitHub,
even with the connector connected and working. It could only work from what
was in the worktree.

- **It can read from your apps now**, and write to them with your
  confirmation. The write is queued and shows up in the Conectores page.
- **The app's answer is data, not instructions.** An issue title is
  attacker-controlled text, so the result comes back fenced, like everywhere
  else.
- **A repository has to be marked as trusted.** The same mark that already
  guards the repo's own skills: a repository is not necessarily your code,
  and a README that says "read my inbox" is an instruction from the
  repository. The page that asks for the trust now says what trusting one
  hands over.

Nothing else changes: the catalogue, the C-suite agents, the playbooks and the
token sign-in all work as they did in 1.2.7.

## 1.2.7 — 2026-11-01 — Your apps, in a catalogue

The Connectors page used to be a list of connection settings: a client id, a
set of permissions, a credential line, an audit table. It answered "how do I
wire this" and never "what does this app do for me" — so choosing one was
guesswork.

Now it opens like a catalogue.

- **Every app says what it is for.** Under the name, one line: "Issues, pull
  requests and releases" for GitHub, "Lee y busca en tu correo, y envía
  mensajes" for Gmail. In your language.
- **The apps are in sections.** Productivity (mail, calendar, files, tasks),
  Work (issues, notes, messages), Reading (highlights, feeds).
- **A search box**, so you can find the app by what it does and not by the
  name you half-remember.
- **Each app has its own colour and a "+" to connect it.** The "+" opens the
  connection that app already uses — the token form for the ones that take a
  token, the consent screen for the ones that use OAuth, and the settings (with
  the reason why) for the ones that need your own client id first.
- **The settings are still there, folded below.** The client id, the scopes,
  the credential and the audit log did not go anywhere: they are one click
  away, and they remember whether you had them open, so a reload does not
  fold them under your hands.

Nothing else changes: the connectors, the playbooks, the token sign-in and the
write confirmations all work as they did in 1.2.6.

## 1.2.6 — 2026-11-01 — The agents can use the apps you connected

On the Mac it installs from the app ("Check for Updates…", or the quiet check
a little after start-up). One thing changes, and it is the thing that was
missing: the agents that answer you can now reach the connectors you have
connected.

- **The Boardroom's CEOs can use your connectors.** Until now the connector
  tools worked in the chat and in the task runner, but the twelve CEOs — the
  ones that have their role playbooks — answered with no tools at all. A CFO
  with a pricing playbook could not look at an issue in GitHub, because the
  CEO's reply never offered to look. It does now.
- **Talking to one agent works too.** The agent you are talking to in the team
  chat can check a connected app, and so can the Holding CEO when you ask it
  a quick question over Telegram or Discord.
- **The agent is told what it has.** A tool the model is offered but never
  told about is a tool it does not reach for. Every one of these answers now
  says which apps it can read, and it says the two things that keep it
  honest: what an app returns is data, not instructions (an issue can carry
  text that tries to steer the agent), and a write waits for you to confirm it.
- **The chat still streams when there is nothing to look up.** Reading from a
  connected app is a round trip, so when one is involved the answer arrives
  in one piece. With no app connected, nothing changes: the text still arrives
  as it is written.
- **A guard so this does not slip again.** The paths where you are waiting
  for an answer are listed in a test, and it fails if one of them cannot
  reach your apps, or if a new piece of the app answers you without saying
  whether somebody is waiting.

Nothing else changes: the playbooks, the connectors, the token sign-in and the
credential warnings all work as they did in 1.2.5.

## 1.2.5 — 2026-11-01 — Agents with their own playbooks, and connectors the agent can use

On the Mac it installs from the app ("Check for Updates…", or the quiet check
a little after start-up). What changes: the Boardroom's C-Suite now works to
real playbooks instead of a free-text list of skills, the agent can use the
connectors you have connected, and a connector can be connected once with a
personal token instead of a consent that expires.

- **The Boardroom's agents have real playbooks.** The C-Suite could list
  skills in a text field, but there was nothing to install: an agent's
  "skills" were words a human had pasted into a prompt. There is now a
  catalog of role playbooks (36 of them, one pack per role), and each agent
  can have any of them installed. A CFO's pricing playbook is not the CTO's
  code-review playbook: they are separate documents, installed separately,
  and an agent only ever reads its own.
- **A new agent arrives with its role's playbook already on it.** You do not
  have to install a pack per agent by hand. An agent created for a role that
  has a pack gets it. A role with no pack gets nothing: guessing would
  install the wrong procedure.
- **The agents you already had get theirs too.** They were created before the
  playbooks existed, so a one-off step at start-up gives each of them the
  pack of its role.
- **You can see what an agent will read.** The panel shows which playbooks
  reach it, which ones are only listed by name, and why: a playbook too long
  to read whole is listed rather than cut in half, because half a procedure
  is worse than none.
- **The agent can use the connectors you connected.** Until now a connector
  was something you set up and the agent could not touch: the actions existed
  and were audited, but the model was never offered them. Now a connected
  connector's actions are tools of the agent. Only the connectors you have
  connected are offered, and only the ones you connected with write
  permission can write.
- **A write is never a write on its own.** When the agent wants to post a
  comment or send a mail, the action waits for you: it appears in the
  Conectores page with what it is about to do, and runs only when you confirm
  it. The agent is told not to claim it was done.
- **What a connector returns is data, not instructions.** An issue can carry
  text that tries to steer the agent. Everything a connector returns is
  marked as data before the agent reads it.
- **Connect a connector once, with a personal token.** The OAuth consent has
  a shelf life the provider decides: 30 days of inactivity for Slack and
  Microsoft, 7 days for an app in testing, and a Gmail consent stops working
  when you change your Google password. A personal token lasts until you
  revoke it. Slack and Microsoft now offer "Use a personal token" next to the
  normal consent, and the token is checked with the provider before it is
  saved and kept in the system keychain, never in a database column.
- **The card says when a connection will lapse.** Instead of finding out the
  day the first sync fails, the connector card says how long you have, and a
  personal token says plainly that it does not expire.
- **More playbooks, from other projects.** 13 role playbooks from the
  TerminalSkills collection (Apache-2.0) join the ones written here, each
  with its origin and licence recorded in the file.

## 1.2.4 — 2026-10-30 — A dark theme that works, better answers without a key, Linux and Windows betas

On the Mac it installs from the app ("Check for Updates…", or the quiet check
a little after start-up, from 1.2.2 on). What changes there: the dark theme,
the answers without an AI key, the note actions in your language and wording
that is true on every system. Linux (AppImage, .deb) and Windows (setup.exe)
get their 1.2.4 builds as betas: pre-releases, unsigned on Windows, without
automatic updates; both now start and pass their checks in CI (Ubuntu 22.04
and 24.04; Windows install, start, close and uninstall).

- **The dark theme works on the dashboard.** The ◐ toggle added the `dark` class but the dashboard stayed white: its colour aliases were resolved on `:root` with the light palette. They are now declared on `body` too.
- **Better answers without an AI key.** Words of one family match ("decide" and "decision", "pricing" and "price"), a line under a matching heading counts it and shows it ("Decision (2026-10-21) — Keep both plans…"), a note's title adds a little, and the note with the best line comes first. Asked "What did we decide about pricing?", it now quotes the decision instead of a daily note that mentions pricing.
- **Note actions in your language.** Key points, Summarize, Action items and Expand answered in Spanish for everyone; they now follow the browser's language (Spanish for es-*, English otherwise), and the API defaults to English. The Translate buttons keep their own language.
- Search results on the notes home show the words around the match instead of a few words.
- **Copy that is true on every system.** The welcome page said the AI key "is stored in the Keychain of this Mac", also on Linux and Windows (seen in the first Linux and Windows smoke tests). It now says the system keychain (the Keychain on a Mac) or an encrypted file. The same for the other "this Mac" / "este Mac" in the interface (Connectors, Learning, the feedback buttons, Servers, Media): "this computer" / "este ordenador". Nothing changes on the Mac except the wording.
- CI only: the "Windows bundles" workflow now runs `scripts/windows-smoke.ps1` on the NSIS installer: it installs it silently, starts the app as a new user, and checks the server, that `/api/platform` says Windows with the in-app browser and code execution off, the first run and welcome page, the window (with a screenshot), that the app and its server are still running 20 s later, that closing the window stops the server, and that **uninstalling keeps the user's data** (it lives in `%LOCALAPPDATA%\ConductLoop\data`, inside the install folder). Still informational (a runner has no real desktop session), but each failed check is now an error annotation on the run page, and the screenshot and log are kept for 3 days.
- CI only: the Linux bundles workflow now **starts the AppImage and the .deb** it builds, on Ubuntu 22.04 (the build image) and on ubuntu-latest, on a virtual display with an empty HOME and no keyring. `scripts/linux-smoke.sh` checks that the local server answers, that `/api/platform` says Linux with the in-app browser off, that it is a first run with the welcome page, that a ConductLoop window is on screen, and that closing the app stops the server; screenshots and logs are kept for 3 days. Until now the Linux packages were only built, never run. The script also works by hand on a Linux machine.
- CI only: a "Windows (compile and tests)" workflow checks on every relevant pull request that the desktop wrapper still compiles for Windows and that the Windows-safe part of the Python tests passes there. It builds no installer, signs nothing, publishes nothing and is not a required check. Windows is still not supported.
- Windows: a CI workflow, "Windows bundles", builds the NSIS installer and the .msi on `windows-latest` and keeps them for 3 days as downloadable artifacts. It only runs when started by hand (it is not a check of pull requests), signs nothing and publishes nothing. CI only: the app does not change.
- **Windows: a script to build an installer (experimental, unsigned).** `scripts/build-windows.ps1` freezes the server with PyInstaller and runs `tauri build --bundles nsis` (an `.msi` with `-Bundles nsis,msi`); the Windows settings live in `desktop/src-tauri/tauri.windows.conf.json`, so the macOS build does not change. The installer is not signed, so Windows SmartScreen warns when it runs ("More info", then "Run anyway"), and it needs WebView2 (included in Windows 11 and current Windows 10; the installer downloads it if missing). No updates on Windows: the app does not look for updates at start-up there. On Windows the bundled server now runs in UTF-8 mode. Not tested on a real Windows machine yet; Windows is still not a supported platform. `docs/PORTABILITY.md` has the status.

## 1.2.3 — 2026-10-29 — A home for your notes and a Brain that helps without a key

The first version that installs from the app: in 1.2.2, "Check for Updates…"
(or the quiet check a little after start-up) offers it. On the Mac, what
changes is the first part of the list below (the notes home, the Brain without
a key, Setup); the Linux and Windows items are groundwork for those systems and
leave the Mac app as it was.

- **A home for your notes.** Without projects (a fresh install has none), the
  dashboard opened on an empty project canvas and search hid behind ⌘P. It now
  opens on "Your notes": how many are indexed, a box to ask a question (it goes
  to the Brain in Chat, filled in, not sent), a search box and the recent notes,
  each opening in the note view. With projects the canvas is unchanged.
- **The Brain is useful without an AI key.** With no provider it answered with a
  placeholder, even when it had found the right note. It now quotes the lines of
  the notes it found that share the most words with the question, with their
  [N] sources, and says how to get written answers. Same in Chat, team mode
  included.
- **Setup marks what is optional.** Semantic search, projects and the memory
  export to Obsidian are extras: shown with "–" and "optional", and no longer
  counted against "ready". Their hints no longer ask for a terminal (`pip
  install`, `OPS_PROJECTS_FILE`, `ops obsidian-sync`), which the desktop app does
  not have, and the checklist shows folder names as text.
- The dashboard's "Aprendizaje" link and "Yo creo — segundo cerebro" node are in
  English, like the rest of the dashboard.
- **Windows groundwork: four fixes that cannot be tested on a real Windows machine yet.** The check that the desktop app is still running no longer ends the app on Windows (it used a signal that Windows treats as "terminate"); the local server refuses to start when its port is already taken, instead of sharing it with another program; the app token and server nonce are generated on Windows as well; and setting private file permissions is a documented no-op there instead of an error. macOS and Linux behave exactly as before. Windows itself is still not supported: there is no installer and no secure key storage yet.
- Linux: `scripts/linux_update_manifest.py` builds and checks the update manifest for the AppImage (the same format as the macOS one, `linux-x86_64-appimage` only). It signs nothing and publishes nothing, and the Linux app still does not check for updates; this is only the groundwork for it. macOS is unchanged.
- **Linux: secrets go to the system keychain (Secret Service).** On Linux, API keys and the other stored secrets now use the desktop keyring (GNOME Keyring, KWallet or KeePassXC), under the same names as on macOS. If there is no keyring (no D-Bus session, a headless machine, a locked keyring nobody unlocks), ConductLoop says so on the platform status, keeps provider keys in the encrypted vault and refuses connector tokens; it never stores them in plain text without telling you. If the keyring exists but cannot be read, ConductLoop stops with an error instead of creating a new key that would make saved connection credentials unreadable. macOS is unchanged. Experimental: not tested on a real Linux desktop yet.
- Linux: a CI workflow, "Linux bundles", builds the AppImage and the .deb on every pull request that touches the desktop app and keeps them for 3 days as downloadable artifacts. It builds on `ubuntu-22.04` instead of `ubuntu-latest` so the AppImage works with an older glibc; it is the only job pinned to a runner image. CI only: the app does not change.
- **Linux: a script to build an AppImage and a .deb.** `scripts/build-linux.sh` freezes the server with PyInstaller and runs `tauri build --bundles appimage,deb`; the Linux settings live in `desktop/src-tauri/tauri.linux.conf.json`, so the macOS build does not change. No signing and no updates on Linux (the app does not look for updates at start-up there). Experimental: the packages have not been installed or run on a Linux desktop yet.
- **Linux: a File menu with Close Tab (Ctrl+W) and Close Window (Ctrl+Shift+W).** The default Linux menu has no File menu, so the two shortcuts did nothing there. The macOS menu does not change. Not tested on a real Linux desktop yet.
- **Linux: the command sandbox no longer lets commands reach local sockets.** Tests and linters run by agent sessions on Linux could still connect to the desktop's D-Bus session bus, the X11 server, ssh-agent/gpg-agent and the Docker socket from inside bubblewrap; those are now hidden. Without bubblewrap, commands stay off. macOS is unchanged. Sockets nested deeper than three levels under /tmp or /run, or in other directories, can still be reached (a seccomp filter would close that).
- **Honest about what each system can do.** The in-app browser is now off on Linux and Windows (it only exists on macOS 14+), and the Browser page says why instead of offering to enable it. On Windows, code sessions are turned off with an explanation: there is no OS sandbox there yet and ConductLoop never runs commands without one. Where the sandbox is missing on Linux, the Sessions page says that commands are off. macOS is unchanged.
- Docs only: `docs/PORTABILITY.md` audits everything that is macOS-only (keychain, sandbox, in-app browser, paths, sidecar, menu, updater, CI) and plans the Linux (experimental) and Windows work. Nothing changes in the app.

## 1.2.2 — 2026-10-29 — Updates that work, a signed DMG and the folder picker in Setup

**Install this one from the DMG** (1.2.1 cannot update itself, see below). From
1.2.2 on, new versions install from the app.

- **The app can update itself.** "Check for Updates…" is in the ConductLoop menu,
  and the app also looks once, quietly, a little after it starts. When there is a
  new version it asks; "Install and restart" downloads it, checks its signature,
  replaces the app and restarts it. Nothing is installed without that click. Up to
  1.2.1 nothing ever started a check, so no install could update itself: from
  1.2.1, install this version from the DMG once more.

- **The folder picker in Setup too.** Setup → Library now has "Choose…" next to
  each folder field and "Add folder…" for the folders to index, like the
  welcome page. It only fills the field: "Save folders" saves.

## 1.2.1 — 2026-10-29 — Backups, a folder picker, a browser that stays in its page, and an app that starts

The first release since 1.2.0 (2026-07-23), so it also carries every dated
section below down to the updater key fix. **If you are on 1.2.0 or earlier,
install this one from the DMG**: those builds embed an updater key that does
not match the releases and can never update themselves (see "Updater signing
key fixed" below). From 1.2.1 on, updates install from the app.

- **Fixed: the in-app browser got in the way of the rest of the app.** Leaving
  the browser page left the open web page drawn over the dashboard and every
  other page; coming back with Go › Back showed no page at all. ⌘W quit the
  whole app instead of closing the tab, and ⌘[ / ⌘] left the browser instead
  of going back in the tab. Now the tabs are hidden as soon as you leave the
  browser page and shown when you come back, ⌘W closes the tab (Close Window
  is ⇧⌘W) and Back/Forward act on the tab. Also: the URL bar shows the page you
  are on, new tabs are called "New tab" instead of "… about:blank", messages
  clear themselves, an address that points to your own machine or your LAN
  says it was blocked instead of doing nothing, the first click on the app's
  bar after using a page is no longer lost, the agent panel starts from your
  configured AI provider, and "Ask" without a key points to Credentials.

- **Fixed: the desktop app never got past "Connecting to local server…".** The
  window waits for its own server by looking for a per-launch id in
  `/api/health`, but the server only echoed the first 16 characters of it. The
  window never recognised its server, gave up after about two and a half
  minutes and loaded the dashboard without the start-up nonce, so every page
  answered 403 `app_token_required`. The id is now echoed in full.

- **Fixed: the Setup and Boardroom pages did nothing.** A script error stopped
  every script on both pages. Setup (the checklist, the library folders and
  "Index now") stayed on "Checking…". Boardroom never listed the C-suite: a
  truncated function and a missing `escapeHtml` broke it, and it linked a
  stylesheet that does not exist. Both work again. The "Ideas that keep coming
  back" list in Setup, which was drawn from the wrong data, now shows the themes.
  A new test runs every page script through `node --check`, so this cannot ship
  unnoticed again.

- **Backups you can trust, and restore from the app.** Setup → Backups takes a
  backup with one click, lists the last ones and restores any of them. The app
  also takes one on its own once a day, and one right before an update changes
  the database (migration 26 deletes tables). Backups used to be a copy of the
  database file: one taken while the app was writing could miss the latest
  changes or come out damaged, and inside the installed app it looked for the
  database in the wrong folder and failed. They now go through SQLite's backup
  API, are checked before they are kept, and live in `backups/` next to the
  database (the last 30). A restore first backs up the current state, refuses a
  damaged file or one made by a newer version, and brings an older backup up to
  date. From the terminal: `python -m ops_dashboard backup`, `backups` and
  `restore <name>`.

- **Content Security Policy for the desktop app.** The Tauri window loads the UI
  from the local server, so the server's header is the policy that applies to
  it; it now restricts everything to the app itself (`default-src 'self'`, no
  remote origins, no `eval`, no frames) plus Tauri's IPC. `'unsafe-inline'`
  stays only for scripts and styles because the older pages still contain inline
  code (a test now stops it from growing). The loading page gets a strict policy
  in `tauri.conf.json` (it was `null`). Images from other sites inside notes no
  longer load. See `docs/CSP.md`.

- **Pick your folders instead of typing them.** In the desktop app, the
  folder fields of the welcome page have a "Choose…" button that opens the
  macOS folder picker. It only fills the field; nothing is saved until you
  click Save. In a browser there is no button and the path is typed as before.

- **A first-run welcome page, and a notes folder you can choose.** On a fresh
  install the app now opens a welcome page that asks for two things, both
  optional: the folder with your notes (it reads it, never writes into it) and
  the folder where ConductLoop keeps its own notes, and an AI key. The key goes
  to the Keychain (or the encrypted file when there is none) and is never shown
  again, logged or sent back. The choice of folder is saved in the app, so it no
  longer needs the `OPS_VAULT_ROOT` environment variable, which the installed
  app had no way to set; it can also be changed later in Setup → Library. The
  page is shown once, and never to anyone who already has notes indexed, a key
  or any setting. The old "quick tour" is still at `/tour`.

- Contributor tooling: `scripts/verify.sh` (also `make verify`) runs the same checks as CI locally, with `--fast`, `--offline` and `--strict` options and an optional pre-push hook (`scripts/install-hooks.sh`). The mypy typed-module list now lives in `scripts/mypy-ratchet.txt`, shared by CI and the script.

- Contributor tooling: added a pull request template (with the local-verification, migration, changelog, no-personal-data and docs checklist) and GitHub issue forms for bug reports and feature requests; blank issues are off and vulnerabilities point to private reporting.

- Release tooling: `scripts/release_check.py --version X.Y.Z` is a dry-run pre-flight for desktop releases (versions in sync, dated CHANGELOG section and notes, updater key id, minimum macOS, updater endpoint, clean git tree). It publishes nothing. See `docs/RELEASING.md`.

## 2026-10-28 — Leftover billing tables and Stripe integrations removed

> **⚠️ This deletes data from your local database.** Migration 26 drops the
> tables `entitlements`, `founders` and `payment_events` and every row in them
> (old plan grants, founder badges, payment audit rows). Nothing in the app reads
> them any more. If you want to keep a copy, back up `ops.db` before updating
> (the data folder is `~/Library/Application Support/com.conductloop.desktop/data`
> on the desktop app). It does not touch any other table, and it is safe to run
> more than once.

**What changed**
  - New migration 26: `DROP TABLE IF EXISTS` for those three tables (their
    indexes go with them). New databases never create them.
  - Removed the entitlement helpers (`get_entitlement`, `grant_entitlement`,
    `list_entitlements`, `revoke_entitlement`) from `db_auth`/`db`. Signup no
    longer grants a "free" plan, and `/api/auth/me` no longer returns an
    `entitlement` field.
  - Removed the generic Stripe connector (`connectors.py`: tester, tools and
    catalog entry) and the Stripe source of the revenue indexer. The Apple App
    Store and Google Play sources and the mock source are unchanged.
    Connections or revenue sources of type `stripe` that you saved earlier stay
    in your database but can no longer be tested or polled.
  - Removed the `cloudflare-worker/` directory (an unused status-page Worker);
    the local `/status` page is unaffected.
  - Removed the founders mock data from the demo build and the leftover
    references in docs, i18n and tests.

## 2026-10-28 — Billing and license code removed from the repo

ConductLoop is free, so the dead billing/license code is gone.

**What changed**
  - Removed `ops_dashboard/licensing.py`, `ops_dashboard/db_billing.py`,
    `ops_dashboard/web/billing.html`, `docs/BILLING.md` and their tests.
  - Removed the `/license`, `/billing`, `/billing/success`, `/api/license/status`
    and `/api/billing/*` routes (they now 404 like any unknown path). The
    login/signup flow no longer writes a `payment_events` row, and
    `/api/auth/me` no longer returns the founder badge fields.
  - The README no longer lists Stripe as a connector.
  - Database tables are untouched (no destructive migration).

## 2026-10-28 — Evals live con tu clave (aprendizaje L2)

`python -m ops_dashboard.evals run --live` ejecuta los casos de evals contra un
proveedor real con la clave BYOK del llavero (`--key-ref`), solo a mano y nunca
en CI. Presupuesto en EUR y límite de llamadas obligatorios, precio del modelo
obligatorio, informe local comparable con la baseline offline, resultados en
`eval_runs`/`eval_results` (migración 25), `--record` y `--judge` opcionales.
Ver `docs/evals.md`.

## 2026-10-25 — The vault no longer hangs the app

**What changed** (PRs #30, #31)
  - A note folder that stops answering (an unmounted drive, a stalled sync
    folder) can no longer freeze the app: vault reads have a deadline, and the
    API answers `503 vault_unresponsive` instead of hanging (deadline 240 s,
    `CONDUCTLOOP_VAULT_TIMEOUT`).
  - A new list of "things you linked and never wrote": `[[links]]` in your
    notes that point to a note that does not exist.

## 2026-10-24 — Offline search, credentials screen and the vault of pages

**What changed** (PRs #15–#29)
  - **Offline semantic search** (#21): the desktop app embeds notes with an
    ONNX engine (fastembed), with no network, key or credit. The app grows from
    41 MB to 88 MB; the model downloads once, on first use, into the data
    folder. Changing the model re-embeds everything; the configured provider wins
    over whatever happens to be installed.
  - **Credentials** (#25): every key of the app lives in the macOS keychain, or
    in a passphrase-encrypted file if there is none — never in plain text, and
    values are never shown again. `/credentials` lists them all; keys already in
    plain text are moved with one button. The setup checklist now points there.
  - **Servers panel** (#24): `/servers` shows, per capability (chat, image,
    video, speech, search), which server will answer and which others could.
  - **The map of your work** (#15–#20, #22, #23): the library draws a map of
    your projects from your notes and writes it into the vault as pages
    (`pages/Index.md`, `pages/<project>.md`, `pages/areas/<area>.md`). It only
    changes when something changes, detects lines missing from your hand-written
    index and proposes them, inserts them without touching your text, and
    keeps an "unclassified" section and a list of forgotten notes. A **daily
    note** gets a marked block with what happened in the vault, without
    overwriting what you wrote. The agent's own instructions point at the map.
  - **Where each project stands** (#26–#29): each project page shows "Where this
    stands" (from your handoff notes) and "What remains open", plus a
    `pages/Waiting.md` page of what is waiting for you, and a `WAITING.md` the
    agent reads at the start.

## 2026-10-23 — Any model, any media vendor, read-only note folders

**What changed** (PRs #6–#14)
  - **Providers** (#6, #7, #8, #12, #13): MiniMax defaults to `MiniMax-M3` and the
    MiniMax Coding Plan subscription is supported; the Brain can talk to any
    OpenAI-compatible server (`OPENAI_BASE_URL`), and one endpoint can serve both
    chat and images. Provider errors are readable (retired model, no credit,
    bad key) and a failing plan call no longer wastes one call per agent. No
    `temperature` is sent to the Anthropic SDK 2.x.
  - **Media** (#11): the media pipeline is not tied to one vendor
    (`media.pick_provider`).
  - **Read-only note folders** (#10): extra folders of notes
    (`index_folders` / `memory_workspace` in `config.json`) are indexed read-only
    and re-indexed hourly.
  - The notes of the detected project now reach the Brain's context (#9).

## 2026-10-22 — Chat with your AI team

**What changed** (PRs #2–#5)
  - New `/chat` page (#3): talk to the Brain, the lead CEO or any executive. In
    team mode the CEO picks one to three agents, they answer in parallel in live
    cards, and the CEO writes the final answer (N+2 model calls per turn).
  - The desktop app has Back / Home controls on every page and a **Go** menu (#5).
  - Fix: every Teams load added the built-in AI squad again (#4).
  - The test suite no longer touches your real data folder (#2).

## 2026-10-21 — License, billing and Stripe removed from the app

ConductLoop is free; the leftovers of the old license/billing flow are gone.

**What changed** (PRs #69, #72, #73, #74, #75 + this docs PR)
  - No license gate, no `/license` screen (302 → `/`), no
    `/api/license/{trial,activate,checkout}` proxies to the license worker;
    `OPS_LICENSE_ENFORCE` no longer set by the desktop sidecar.
  - No Stripe in the app: checkout/cancel/webhook endpoints return 410,
    purchase emails removed, `/api/pricing-tier` removed, `/billing` is a static
    free-edition page (founders keep their badge, no price).
  - All price strings removed from i18n; the "edition" onboarding step removed
    (checklist is 9 steps).
  - Live license-worker tests and `scripts/test-fleet` removed (they created
    trials in the production D1); bare `pytest` never runs live tests.
  - `conductloop.egg-info/` untracked.

**User impact**: none for ≥1.1.0. Existing license/trial keys stay on disk and
are ignored. Desktop 1.0.x keeps validating against the worker, which now
accepts any existing key (EVMCOD/conductloop-license #1).

## 2026-10-21 — Updater signing key fixed (reinstall needed once)

**Problem**: v1.1.x/v1.2.0 embed updater pubkey `D68774E13CB63DED`, but
releases are signed with `770EA4C292DC6BD2` (the only updater key available),
so those installs can never auto-update.

**What changed**
  - `desktop/src-tauri/tauri.conf.json`: updater pubkey is now
    `770EA4C292DC6BD2` (verified against the published v1.2.0 signature).
  - `desktop/UPDATER_KEY_ID` + `scripts/check_updater_pubkey.py`: CI and
    `scripts/release-desktop.sh` fail on any key id mismatch (before building,
    and again on the produced `.sig`).
  - `scripts/release-desktop.sh`: removed the unused hard-coded pubkey and the
    stale "7-day free trial, then $10/month" release-note copy.

**User impact**: anyone on v1.2.0 or earlier must reinstall the DMG **once**
(unless the `D68774E13CB63DED` private key turns up; v1.1.0 embeds yet another
key, `CADDC7E4879ACD78`). Later releases update
automatically. See `desktop/docs/DISTRIBUTION.md`.

## 2026-07-16 — ConductLoop goes FREE (no subscription, no payment)

**Strategic pivot**: ConductLoop is now 100% free for everyone. The $10/mo
subscription and the Lifetime tier were retired on 2026-07-16. All features
unlocked for every install.

**What changed**
  - `ops_dashboard/licensing.py`: `enforce()` now always returns False.
    `status()` returns `{licensed: true, plan: "free", edition: "free"}`.
    Functionality kept for future cloud-only features (cross-device sync,
    AI hosting, team workspaces) — those will reintroduce paid tiers behind
    per-feature gates, NOT a global subscription.
  - `ops_dashboard/server.py`:
    - `_billing_checkout_subscription` returns a friendly "free edition" JSON
      instead of creating a Stripe session (kept for old client compatibility).
    - `_billing_checkout_lifetime` ditto.
    - The license gate on `/` (which used to redirect to `/license` when not
      licensed) is now always open because `enforce()` is False.
  - `ops_dashboard/web/license.html`: rewritten as a Free Edition landing card.
  - `ops_dashboard/web/billing.html`: shows a single "FREE EDITION" card.
    The legacy pricing/auth JS is preserved below the cut, ready to resurrect
    when cloud features ship.
  - `ops_dashboard/web/billing-success.html`: stops polling for a Stripe key;
    now displays a generic "Welcome, free edition" message and an optional
    best-effort fetch of any legacy session-bound key.
  - `README.md`: updated License section to reflect free-for-everyone.

**What did NOT change (intentional)**
  - Stripe / Apple IAP code in `conductloop-license` Cloudflare Worker is
    kept intact but dormant. It will be re-pointed at per-feature checkout
    when cloud-only features land.
  - License keys that were issued before today still validate against the
    worker (so anyone who paid during beta keeps their install recognized).
    New installs don't need a key.
  - The `/api/license/trial` and `/api/license/activate` endpoints still
    work — useful if you want a key for tracking, but not required.

**Migration impact**
  - The daily license-expiry cron that used to email the chairman 7 days
    before a license expired is now a no-op (no new trials expire).
  - The Boardroom no longer auto-reminds the chairman about an upcoming
    license renewal.

This is the biggest pricing change ConductLoop has had. From here on, growth
comes from usage, virality, and (later) optional cloud upgrades — not from
a paywall.

---
