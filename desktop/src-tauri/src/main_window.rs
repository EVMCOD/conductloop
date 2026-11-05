//! The main window (label `main`) and what it does with the browser APIs that
//! WKWebView/wry leave unhandled unless the app handles them.
//!
//! Without handlers, inside the packaged app:
//! * `window.open(...)` and `<a target="_blank">` did nothing at all: wry only
//!   creates a new window when `on_new_window` is set, and nothing was set for
//!   this window (only the browser tabs had one, browser.rs).
//! * `<a download>` was cancelled and a response WebKit cannot show was
//!   dropped: wry only downloads when `on_download` is set.
//! * A link without `target` to another site replaced the whole app with that
//!   site, with no way back (the window has no browser chrome).
//!
//! So the window is built here (tauri.conf.json has `"create": false` for it)
//! with the three handlers. Other sites open in the system browser; the app's
//! own pages open in this window; downloads go to the Downloads folder and the
//! page is told where.

use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{Manager, Url, WebviewWindow, WebviewWindowBuilder};

/// What the main window does with a URL it is asked to load or open.
#[derive(Debug, PartialEq, Eq)]
pub enum Route {
    /// The app itself (its loading page, or the bundled server).
    App,
    /// Another site: the system browser opens it.
    External,
    /// Anything else (file:, data:, javascript:...): nothing happens.
    Block,
}

/// `origin` is the bundled server, e.g. `http://localhost:7777`.
pub fn route(url: &Url, origin: &Url) -> Route {
    let host = url.host_str().unwrap_or("");
    match url.scheme() {
        // The bundled loading page (tauri://localhost on macOS/Linux,
        // http(s)://tauri.localhost on Windows) and blank documents.
        "tauri" | "about" => Route::App,
        "http" | "https" if host == "tauri.localhost" => Route::App,
        "http" | "https" => {
            let loopback = matches!(host, "localhost" | "127.0.0.1" | "[::1]" | "::1");
            if loopback && url.port_or_known_default() == origin.port_or_known_default() {
                Route::App
            } else if loopback {
                // Another local server: never hand a loopback URL to the
                // system browser from here, and never load it in the window.
                Route::Block
            } else {
                Route::External
            }
        }
        "mailto" => Route::External,
        _ => Route::Block,
    }
}

/// Only for URLs `route` sent to `External` (http, https, mailto).
#[allow(deprecated)] // tauri-plugin-shell's open: the opener plugin is not a dependency.
fn open_external<R: tauri::Runtime>(app: &tauri::AppHandle<R>, url: &Url) {
    use tauri_plugin_shell::ShellExt;
    let _ = app.shell().open(url.as_str(), None);
}

/// A string as a JavaScript literal (JSON is a subset of JS for strings).
fn js_str(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into())
}

/// The toast of web/toast.js, when the page has it.
fn toast<R: tauri::Runtime>(view: &tauri::Webview<R>, msg: &str, kind: &str) {
    let _ = view.eval(format!(
        "window.notify && window.notify({}, {})",
        js_str(msg),
        js_str(kind)
    ));
}

pub fn build<R: tauri::Runtime>(app: &tauri::App<R>, origin: &str) -> tauri::Result<WebviewWindow<R>> {
    let origin: Url = origin.parse().expect("BACKEND_URL is a valid URL");
    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|w| w.label == "main")
        .cloned()
        .expect("tauri.conf.json defines the main window");

    let (o_nav, h_nav) = (origin.clone(), app.handle().clone());
    let (o_new, h_new) = (origin.clone(), app.handle().clone());
    let o_dl = origin;

    WebviewWindowBuilder::from_config(app.handle(), &config)?
        .on_navigation(move |u| match route(u, &o_nav) {
            Route::App => true,
            Route::External => {
                open_external(&h_nav, u);
                false
            }
            Route::Block => false,
        })
        .on_new_window(move |u, _features| {
            match route(&u, &o_new) {
                Route::App => {
                    if let Some(win) = h_new.get_webview_window("main") {
                        let _ = win.navigate(u);
                    }
                }
                Route::External => open_external(&h_new, &u),
                Route::Block => {}
            }
            NewWindowResponse::Deny
        })
        .on_download(move |view, event| match event {
            // Only the app's own files (and blobs it made) are saved; wry has
            // already picked a free name in the Downloads folder.
            DownloadEvent::Requested { url, .. } => {
                matches!(url.scheme(), "blob" | "data") || route(&url, &o_dl) == Route::App
            }
            DownloadEvent::Finished { path, success, .. } => {
                match (success, path) {
                    (true, Some(p)) => toast(&view, &format!("Saved to {}", p.display()), "ok"),
                    (true, None) => toast(&view, "Saved to your Downloads folder", "ok"),
                    (false, _) => toast(&view, "The download failed", "error"),
                }
                true
            }
            _ => true,
        })
        .build()
}

#[cfg(test)]
mod tests {
    use super::{route, Route};
    use tauri::Url;

    fn r(u: &str) -> Route {
        route(&u.parse::<Url>().unwrap(), &"http://localhost:7777".parse::<Url>().unwrap())
    }

    #[test]
    fn the_app_stays_in_the_window() {
        assert_eq!(r("http://localhost:7777/"), Route::App);
        assert_eq!(r("http://127.0.0.1:7777/status.html"), Route::App);
        assert_eq!(r("http://localhost:7777/__cl/boot?n=x"), Route::App);
        assert_eq!(r("tauri://localhost/index.html"), Route::App);
        assert_eq!(r("http://tauri.localhost/index.html"), Route::App);
        assert_eq!(r("about:blank"), Route::App);
    }

    #[test]
    fn other_sites_go_to_the_system_browser() {
        assert_eq!(r("https://example.com/x"), Route::External);
        assert_eq!(r("http://example.com"), Route::External);
        assert_eq!(r("mailto:someone@example.com"), Route::External);
    }

    #[test]
    fn everything_else_goes_nowhere() {
        assert_eq!(r("file:///etc/passwd"), Route::Block);
        assert_eq!(r("http://localhost:8080/"), Route::Block);
        assert_eq!(r("http://127.0.0.1:11434/"), Route::Block);
        assert_eq!(r("data:text/html,hi"), Route::Block);
        assert_eq!(r("javascript:alert(1)"), Route::Block);
    }
}
