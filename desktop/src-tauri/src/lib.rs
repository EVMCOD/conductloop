// ConductLoop — desktop wrapper (Tauri v2)
//
// Starts the bundled ConductLoop server as a sidecar, waits until it's
// listening, then points the window at it. ConductLoop is free: there is no
// license gate. The sidecar is killed when the window closes.

use std::sync::Mutex;

mod agent_bridge;
mod browser;

use tauri::menu::{Menu, MenuItem, MenuItemKind, PredefinedMenuItem, Submenu};
use tauri::webview::PageLoadEvent;
use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

const BACKEND_URL: &str = "http://localhost:7777";

/// Back / Home controls for the app's own pages (nav.js): the window has no
/// browser chrome, so most pages had no way back to the dashboard.
const NAV_JS: &str = include_str!("nav.js");

/// The default macOS menu plus a Go menu (Back ⌘[, Forward ⌘], Home ⇧⌘H),
/// placed after View like in Safari and Finder.
///
/// ⌘W is "Close Tab": on the browser page it closes the open tab, elsewhere
/// it closes the window as before. It used to be the default "Close Window"
/// everywhere, so ⌘W in the browser quit the whole app and its server.
/// "Close Window" moves to ⇧⌘W, like in Safari.
fn app_menu<R: tauri::Runtime>(handle: &tauri::AppHandle<R>) -> tauri::Result<Menu<R>> {
    let menu = Menu::default(handle)?;
    for item in menu.items()? {
        let MenuItemKind::Submenu(sub) = item else { continue };
        for entry in sub.items()? {
            if let MenuItemKind::Predefined(p) = &entry {
                if p.text()? == "Close Window" {
                    sub.remove(&entry)?;
                }
            }
        }
        if sub.text()? == "File" {
            sub.append(&MenuItem::with_id(handle, "close-tab", "Close Tab", true, Some("CmdOrCtrl+W"))?)?;
            sub.append(&MenuItem::with_id(
                handle,
                "close-window",
                "Close Window",
                true,
                Some("CmdOrCtrl+Shift+W"),
            )?)?;
        }
    }
    let go = Submenu::with_items(
        handle,
        "Go",
        true,
        &[
            &MenuItem::with_id(handle, "go-back", "Back", true, Some("CmdOrCtrl+["))?,
            &MenuItem::with_id(handle, "go-forward", "Forward", true, Some("CmdOrCtrl+]"))?,
            &PredefinedMenuItem::separator(handle)?,
            &MenuItem::with_id(handle, "go-home", "Home", true, Some("CmdOrCtrl+Shift+H"))?,
        ],
    )?;
    // The default menu ends with Window and Help.
    let pos = menu.items()?.len().saturating_sub(2);
    menu.insert(&go, pos)?;
    Ok(menu)
}

struct ServerProcess(Mutex<Option<CommandChild>>);

/// Minimal HTTP GET over plain HTTP (no HTTP crate in the dependency tree).
/// Returns the response body.
fn http_get(url: &str, timeout: std::time::Duration) -> Option<String> {
    use std::io::{Read, Write};
    let rest = url.strip_prefix("http://")?;
    let (host_port, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let mut stream = std::net::TcpStream::connect(host_port).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: {host_port}\r\nConnection: close\r\n\
         User-Agent: conductloop-desktop\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = String::new();
    stream.read_to_string(&mut buf).ok()?;
    buf.split("\r\n\r\n").nth(1).map(|b| b.to_string())
}

/// 32 random bytes as hex from the OS CSPRNG, or None if unavailable.
/// (No extra crate: /dev/urandom on macOS/Linux. Elsewhere the app token is
/// simply not used and the server keeps its previous behaviour.)
fn random_hex32() -> Option<String> {
    #[cfg(unix)]
    {
        use std::io::Read;
        let mut buf = [0u8; 32];
        let mut f = std::fs::File::open("/dev/urandom").ok()?;
        f.read_exact(&mut buf).ok()?;
        Some(buf.iter().map(|b| format!("{b:02x}")).collect())
    }
    #[cfg(not(unix))]
    {
        None
    }
}

#[tauri::command]
fn backend_url() -> &'static str {
    BACKEND_URL
}

#[tauri::command]
async fn check_for_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    // Returns None if up-to-date, Some(version) if update is available.
    // The frontend (settings page) can call this and surface a
    // "Update available — restart to apply" banner. Auto-install is gated on
    // explicit user action; we never yank a running session out from under
    // the user without consent.
    match app.updater() {
        Ok(updater) => match updater.check().await {
            Ok(Some(update)) => Ok(Some(update.version)),
            Ok(None) => Ok(None),
            Err(e) => Err(format!("updater check failed: {e}")),
        },
        Err(e) => Err(format!("updater init failed: {e}")),
    }
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<(), String> {
    match app.updater() {
        Ok(updater) => match updater.check().await {
            Ok(Some(update)) => update
                .download_and_install(|_, _| {}, || {})
                .await
                .map_err(|e| format!("install failed: {e}")),
            Ok(None) => Err("no update available".into()),
            Err(e) => Err(format!("updater check failed: {e}")),
        },
        Err(e) => Err(format!("updater init failed: {e}")),
    }
}

/// The native folder picker behind the "Choose…" buttons of the folder fields
/// (welcome page). Returns the chosen folder, or None when the user cancels.
/// The UI only ever gets a path the user picked by hand in a system dialog.
#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let mut dialog = app.dialog().file();
    if let Ok(home) = app.path().home_dir() {
        dialog = dialog.set_directory(home);
    }
    if let Some(win) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&win);
    }
    // blocking_pick_folder must not run on the main thread, and it would block
    // an async worker: give it a thread of its own.
    let picked = tauri::async_runtime::spawn_blocking(move || dialog.blocking_pick_folder())
        .await
        .map_err(|e| format!("folder picker failed: {e}"))?;
    match picked {
        None => Ok(None),
        Some(path) => path
            .into_path()
            .map(|p| Some(p.to_string_lossy().into_owned()))
            .map_err(|e| format!("folder picker failed: {e}")),
    }
}

/// 32 random bytes as hex from /dev/urandom (no extra crate).
#[cfg(unix)]
fn bridge_token() -> Option<String> {
    use std::io::Read;
    let mut buf = [0u8; 32];
    std::fs::File::open("/dev/urandom").ok()?.read_exact(&mut buf).ok()?;
    Some(buf.iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(unix)]
fn start_agent_bridge(app: &tauri::App) {
    let Ok(dir) = app.path().app_data_dir() else { return };
    let Some(token) = bridge_token() else { return };
    if let Some(sock) = agent_bridge::start(app.handle().clone(), dir.join("agent-bridge"), token.clone()) {
        // Inherited by the sidecar only (spawned right after); its Python side
        // pops both from os.environ on first use.
        std::env::set_var("CONDUCTLOOP_BROWSER_SOCK", sock);
        std::env::set_var("CONDUCTLOOP_BROWSER_TOKEN", token);
    }
}

#[cfg(not(unix))]
fn start_agent_bridge(_app: &tauri::App) {}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .manage(ServerProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            backend_url,
            check_for_update,
            install_update,
            pick_folder,
            browser::browser_open,
            browser::browser_navigate,
            browser::browser_back,
            browser::browser_forward,
            browser::browser_reload,
            browser::browser_close,
            browser::browser_activate,
            browser::browser_set_bounds,
            browser::browser_set_visible,
            browser::browser_list,
            browser::browser_extract,
            browser::browser_agent_control
        ])
        .menu(app_menu)
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            // On the browser page, ⌘W and Back/Forward act on the open tab.
            let tab = browser::visible_active_tab(app);
            match (id, tab) {
                ("close-tab", Some(tab)) => {
                    let _ = browser::close_tab(app, &tab);
                    return;
                }
                ("close-tab" | "close-window", _) => {
                    if let Some(win) = app.get_window("main") {
                        let _ = win.close();
                    }
                    return;
                }
                ("go-back" | "go-forward", Some(tab)) => {
                    if let Some(view) = app.get_webview(&tab) {
                        let _ = view.eval(if id == "go-back" { "history.back()" } else { "history.forward()" });
                    }
                    return;
                }
                _ => {}
            }
            let js = match id {
                "go-back" => "history.back()",
                "go-forward" => "history.forward()",
                "go-home" => "location.assign('/')",
                _ => return,
            };
            if let Some(view) = app.get_webview("main") {
                let _ = view.eval(js);
            }
        })
        .on_page_load(|webview, payload| {
            if webview.label() != "main" {
                return;
            }
            // Tabs are shown on the browser page only. Leaving it (or
            // reloading) hides them, so they are not drawn over the next page;
            // landing on it shows them, also when it comes back from the
            // history cache, where its script does not run again.
            let on_browser = matches!(payload.url().path(), "/browser" | "/browser.html");
            match payload.event() {
                PageLoadEvent::Started => {
                    let _ = browser::set_visible(webview.app_handle(), false);
                }
                PageLoadEvent::Finished => {
                    if on_browser {
                        let _ = browser::set_visible(webview.app_handle(), true);
                    }
                    let _ = webview.eval(NAV_JS.replace("__CL_ORIGIN__", BACKEND_URL));
                }
            }
        })
        .setup(|app| {
            // 0. In-app browser state + filtering proxy (browser design PR 4/5).
            app.manage(browser::BrowserState::new(browser::start_proxy()));
            if let Some(win) = app.get_window("main") {
                browser::refresh_title_bar(&win);
            }
            // 0b. Agent ⇄ browser bridge (PR 8): Unix socket + token, handed to the
            // sidecar through the environment it inherits (set before it is spawned).
            start_agent_bridge(app);
            // 1. Spawn the bundled server sidecar.
            // Per-launch app token (browser design PR 2, ops_dashboard/app_token_guard.py):
            // the secret ends up in an HttpOnly cookie of this window only; the boot
            // nonce is single-use and only lets the first load set that cookie.
            let app_token = random_hex32();
            let boot_nonce = random_hex32();
            // Per-launch instance id: the server echoes it in /api/health, so
            // the window can tell "my sidecar" from "something else that
            // grabbed :7777 first" before handing over the boot nonce.
            let instance_id = random_hex32();
            let mut sidecar = app
                .shell()
                .sidecar("conductloop-server")?
                // So /api/health reports the app version, not "dev".
                .env("CONDUCTLOOP_APP_VERSION", app.package_info().version.to_string())
                // The server exits when this process is gone (Cmd+Q, crash):
                // killing the PyInstaller launcher alone left it running.
                .env("CONDUCTLOOP_PARENT_PID", std::process::id().to_string());
            if let Some(i) = &instance_id {
                sidecar = sidecar.env("CONDUCTLOOP_INSTANCE_ID", i);
            }
            if let (Some(t), Some(n)) = (&app_token, &boot_nonce) {
                sidecar = sidecar
                    .env("CONDUCTLOOP_APP_TOKEN", t)
                    .env("CONDUCTLOOP_APP_BOOT", n);
            }
            let (mut rx, child) = sidecar.spawn()?;
            app.state::<ServerProcess>()
                .0
                .lock()
                .unwrap()
                .replace(child);

            // Drain sidecar output so its pipe never blocks.
            tauri::async_runtime::spawn(async move { while rx.recv().await.is_some() {} });

            // 2. Once the server is listening on :7777, navigate the window to it.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // Wait for OUR server: the instance id proves the process on
                // the port is the sidecar we just spawned. If somebody else
                // owns :7777, that process must not receive the boot nonce
                // (it would become the app's privileged origin, able to hand
                // out the app token). 2026-10-23.
                let mut ours = false;
                for _ in 0..120 {
                    let health = http_get(
                        &format!("{BACKEND_URL}/api/health"),
                        std::time::Duration::from_millis(800),
                    );
                    if let Some(body) = &health {
                        match &instance_id {
                            Some(id) => {
                                if body.contains(id.as_str()) {
                                    ours = true;
                                    break;
                                }
                            }
                            None => {
                                ours = true;
                                break;
                            }
                        }
                    }
                    std::thread::sleep(std::time::Duration::from_millis(500));
                }
                let target = match (&boot_nonce, ours) {
                    (Some(n), true) if app_token.is_some() => {
                        format!("{BACKEND_URL}/__cl/boot?n={n}")
                    }
                    _ => BACKEND_URL.to_string(),
                };
                if let Some(win) = handle.get_webview_window("main") {
                    if let Ok(url) = target.parse() {
                        let _ = win.navigate(url);
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Resized(_) = event {
                // Fullscreen hides the title bar: browser tabs must follow.
                browser::refresh_title_bar(window);
            }
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(child) = window
                    .app_handle()
                    .state::<ServerProcess>()
                    .0
                    .lock()
                    .unwrap()
                    .take()
                {
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running ConductLoop desktop app");
}
