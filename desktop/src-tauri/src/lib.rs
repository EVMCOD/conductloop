// ConductLoop — desktop wrapper (Tauri v2)
//
// Starts the bundled ConductLoop server as a sidecar, waits until it's
// listening, then points the window at it. ConductLoop is free: there is no
// license gate. The sidecar is killed when the window closes.

use std::sync::Mutex;

mod agent_bridge;
mod browser;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
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
fn app_menu<R: tauri::Runtime>(handle: &tauri::AppHandle<R>) -> tauri::Result<Menu<R>> {
    let menu = Menu::default(handle)?;
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
        .manage(ServerProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            backend_url,
            check_for_update,
            install_update,
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
            let js = match event.id().as_ref() {
                "go-back" => "history.back()",
                "go-forward" => "history.forward()",
                "go-home" => "location.assign('/')",
                _ => return,
            };
            // The main webview only: browser tabs keep their own back/forward.
            if let Some(view) = app.get_webview("main") {
                let _ = view.eval(js);
            }
        })
        .on_page_load(|webview, payload| {
            if webview.label() == "main" && payload.event() == PageLoadEvent::Finished {
                let _ = webview.eval(NAV_JS.replace("__CL_ORIGIN__", BACKEND_URL));
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
            let mut sidecar = app
                .shell()
                .sidecar("conductloop-server")?
                // So /api/health reports the app version, not "dev".
                .env("CONDUCTLOOP_APP_VERSION", app.package_info().version.to_string())
                // The server exits when this process is gone (Cmd+Q, crash):
                // killing the PyInstaller launcher alone left it running.
                .env("CONDUCTLOOP_PARENT_PID", std::process::id().to_string());
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
                for _ in 0..120 {
                    if std::net::TcpStream::connect("127.0.0.1:7777").is_ok() {
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(500));
                }
                let target = match &boot_nonce {
                    Some(n) if app_token.is_some() => format!("{BACKEND_URL}/__cl/boot?n={n}"),
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
