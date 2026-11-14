// ConductLoop Demo — desktop wrapper for the trial/marketing build.
//
// v1.0: opens a window pointing at the local server (http://localhost:7777)
//       which the user is expected to have started with the demo launcher
//       (ConductLoop Demo Server.command). The launcher runs the server with
//       CONDUCTLOOP_DEMO=1 and a separate demo.db so the user never sees
//       personal data.
//
// v1.1+ (planned): bundle the Python server as a Tauri sidecar so the .app
//                  starts the demo server automatically and tears it down on close.

use std::time::{SystemTime, UNIX_EPOCH};
use tauri::Manager;

const BASE_URL: &str = "http://localhost:7778";
const WINDOW_TITLE: &str = "ConductLoop Demo";

#[tauri::command]
fn backend_url() -> String {
    // Return a fresh URL with a cache-busting query string every time it's queried.
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{}?v={}", BASE_URL, ts)
}

pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![backend_url])
        .setup(|app| {
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.set_title(WINDOW_TITLE);
                // Cache-bust on every launch so the webview never serves a stale
                // page from a previous server (real or demo) on localhost:7777.
                let ts = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0);
                let url = format!("{}?v={}", BASE_URL, ts);
                let _ = win.navigate(url.parse().expect("valid url"));
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running ConductLoop Demo desktop app");
}
