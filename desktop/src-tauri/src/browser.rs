//! In-app browser tabs (browser design PR 4, decisions B2/B3/B6/B13).
//!
//! Each tab is a child webview (`tab-<n>`) of the main window, created with
//! the `unstable` multiwebview API (`Window::add_child`). The tab strip, URL
//! bar and back/forward/reload live in the ConductLoop UI (`/browser`, webview
//! `main`), which drives these commands through the `browser-ui` capability.
//!
//! Isolation (design §1.2):
//! * `incognito(true)`: non-persistent data store, never the UI's cookies
//!   (`cl_app`, `ops_session`) — B3.
//! * No capability matches `tab-*` → no IPC for remote pages
//!   (tests/test_tauri_capabilities.py).
//! * `on_navigation` applies `conductloop_url_policy` (no loopback/LAN/
//!   link-local/Tailscale, http/https/about:blank only) — B6.
//! * Downloads refused, permission prompts (camera, mic, geolocation,
//!   notifications…) denied, new windows become a request the UI may open as a
//!   tab (re-checked), devtools only in debug builds.

use std::sync::Mutex;

use conductloop_url_policy::{check_navigation, Reason, Verdict};
use serde::Serialize;
use tauri::webview::{NewWindowResponse, PageLoadEvent, PermissionResponse, WebviewBuilder};
use tauri::{AppHandle, LogicalPosition, LogicalSize, Manager, Url, WebviewUrl};

pub const MAX_TABS: usize = 8;
const UI_WINDOW: &str = "main";
const TAB_PREFIX: &str = "tab-";
const MAX_PENDING_NEW_WINDOWS: usize = 4;

#[derive(Clone, Debug, Default, Serialize, PartialEq)]
pub struct TabInfo {
    pub id: String,
    pub url: String,
    pub title: String,
    pub loading: bool,
    /// Last navigation refused by the policy, e.g. "Loopback http://127.0.0.1:7777/".
    pub blocked: Option<String>,
    /// PR 8: agent session that owns this tab (None = the user's tab).
    pub agent: Option<i64>,
    /// PR 8, agent tabs only: who drives it — "agent", "paused" or "human".
    pub control: Option<String>,
    /// Agent tabs: registrable domains opened in the session (B10 new-site
    /// gate). Seeded from the user's task, grown by approvals and by what the
    /// user opens while in control.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub sites: Vec<String>,
    /// Agent tabs: last navigation stopped because its site is new. The
    /// backend asks the user; an approved `navigate` clears it.
    pub pending_nav: Option<String>,
}

pub const MAX_SITES: usize = 64;

/// Pure: the new-site gate for agent tabs. `Ok(Some(site))` → learn `site`
/// (the user is driving); `Ok(None)` → allow; `Err(site)` → stop, the site
/// is new and the agent (or the page, while the agent drives) is moving.
pub fn site_gate(t: &TabInfo, url: &str) -> Result<Option<String>, String> {
    if t.agent.is_none() {
        return Ok(None);
    }
    match conductloop_url_policy::sites::nav_needs_confirm(&t.sites, url) {
        None => Ok(None),
        Some(site) if t.control.as_deref() == Some("human") => Ok(Some(site)),
        Some(site) => Err(site),
    }
}

/// Apply the gate to the tab `label` (locks the state). Returns whether the
/// navigation may proceed.
fn gate_navigation(app: &AppHandle, label: &str, url: &str) -> bool {
    let state = app.state::<BrowserState>();
    let mut g = state.inner.lock().unwrap();
    let Some(t) = g.tabs.iter_mut().find(|t| t.id == label) else {
        return true;
    };
    match site_gate(t, url) {
        Ok(None) => true,
        Ok(Some(site)) => {
            if t.sites.len() < MAX_SITES {
                t.sites.push(site);
            }
            true
        }
        Err(site) => {
            t.pending_nav = Some(url.chars().take(2048).collect());
            t.blocked = Some(format!("new_site {site}"));
            false
        }
    }
}

/// Pure: is the tab showing a page whose site was never allowed (a redirect
/// that slipped past `on_navigation`)? The bridge then refuses to read or act.
pub fn off_site(t: &TabInfo) -> bool {
    t.agent.is_some() && conductloop_url_policy::sites::nav_needs_confirm(&t.sites, &t.url).is_some()
}

/// Add the backend's seeds (sites the user named) to the agent tab of `session`.
pub fn add_sites(state: &BrowserState, session: i64, sites: &[String]) {
    let mut g = state.inner.lock().unwrap();
    if let Some(t) = g.tabs.iter_mut().find(|t| t.agent == Some(session)) {
        for site in sites {
            if !t.sites.contains(site) && t.sites.len() < MAX_SITES {
                t.sites.push(site.clone());
            }
        }
    }
}

/// Add an approved site to the agent tab of `session`; clears pending_nav.
pub fn allow_site(state: &BrowserState, session: i64, site: String) {
    let mut g = state.inner.lock().unwrap();
    if let Some(t) = g.tabs.iter_mut().find(|t| t.agent == Some(session)) {
        if !t.sites.contains(&site) && t.sites.len() < MAX_SITES {
            t.sites.push(site);
        }
        t.pending_nav = None;
    }
}

pub const CONTROL_AGENT: &str = "agent";
pub const CONTROLS: [&str; 3] = ["agent", "paused", "human"];

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Bounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Default)]
struct Inner {
    tabs: Vec<TabInfo>,
    active: Option<String>,
    bounds: Option<Bounds>,
    visible: bool,
    next: u64,
    new_window_requests: Vec<String>,
    /// macOS title bar height (logical px), see measure_title_bar.
    title_bar: f64,
}

pub struct BrowserState {
    inner: Mutex<Inner>,
    /// Filtering proxy for every tab request (PR 5). None = browser disabled
    /// (fail closed: tabs are never created without it).
    proxy_url: Option<Url>,
    /// macOS < 14 can't set a per-webview proxy (B4): browser disabled.
    supported: bool,
}

impl BrowserState {
    pub fn new(proxy_url: Option<Url>) -> Self {
        Self { inner: Mutex::new(Inner::default()), proxy_url, supported: os_supported() }
    }
}

/// Start the filtering proxy (post-DNS check with `is_public_ip`).
pub fn start_proxy() -> Option<Url> {
    match conductloop_url_policy::proxy::spawn(conductloop_url_policy::is_public_ip) {
        Ok(handle) => {
            let url = Url::parse(&handle.url()).ok();
            // Lives for the whole process.
            std::mem::forget(handle);
            url
        }
        Err(_) => None,
    }
}

/// macOS major version from `sw_vers -productVersion` ("14.5" -> 14).
pub fn parse_macos_major(s: &str) -> Option<u32> {
    s.trim().split('.').next()?.parse().ok()
}

fn os_supported() -> bool {
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("/usr/bin/sw_vers")
            .arg("-productVersion")
            .output()
            .ok()
            .and_then(|o| parse_macos_major(&String::from_utf8_lossy(&o.stdout)))
            .is_some_and(|major| major >= 14)
    }
    #[cfg(not(target_os = "macos"))]
    {
        true
    }
}

#[derive(Serialize)]
pub struct BrowserList {
    tabs: Vec<TabInfo>,
    active: Option<String>,
    /// URLs pages tried to open with window.open / target=_blank (drained).
    new_window_requests: Vec<String>,
    /// Hosts the filtering proxy refused lately. A refused load shows nothing
    /// in the tab, so the UI uses this to say why.
    recent_blocks: Vec<String>,
}

/// URL bar input → URL, or an error code for the UI. Pure (unit-tested).
pub fn normalize_input(input: &str) -> Result<Url, String> {
    let s = input.trim();
    let candidate = if s.is_empty() {
        "about:blank".to_string()
    } else if s.contains("://") || has_scheme(s) {
        s.to_string()
    } else if s.contains(char::is_whitespace) {
        return Err("invalid_url".into());
    } else {
        format!("https://{s}")
    };
    let url = Url::parse(&candidate).map_err(|_| "invalid_url".to_string())?;
    policy(&url)?;
    Ok(url)
}

/// "javascript:…", "about:blank", "data:…" have a scheme; "localhost:7777"
/// or "example.com:8080/x" do not (digits after the colon = a port).
fn has_scheme(s: &str) -> bool {
    match s.split_once(':') {
        Some((scheme, rest)) => {
            !scheme.is_empty()
                && scheme.chars().next().is_some_and(|c| c.is_ascii_alphabetic())
                && scheme.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
                && !scheme.contains('.')
                && !rest.starts_with(|c: char| c.is_ascii_digit())
        }
        None => false,
    }
}

fn policy(url: &Url) -> Result<(), String> {
    match check_navigation(url.as_str()) {
        Verdict::Allow => Ok(()),
        Verdict::Block(r) => Err(format!("blocked:{}", reason_code(r))),
    }
}

pub fn reason_code(r: Reason) -> &'static str {
    match r {
        Reason::Scheme => "scheme",
        Reason::BadUrl => "bad_url",
        Reason::Userinfo => "userinfo",
        Reason::LocalName => "local_name",
        Reason::Loopback => "loopback",
        Reason::Private => "private",
        Reason::Reserved => "reserved",
    }
}

/// Bounds from the UI (CSS px == logical px). Pure (unit-tested).
pub fn sanitize_bounds(x: f64, y: f64, width: f64, height: f64) -> Result<Bounds, String> {
    let ok = |v: f64| v.is_finite() && (0.0..=100_000.0).contains(&v);
    if !(ok(x) && ok(y) && ok(width) && ok(height)) || width < 50.0 || height < 50.0 {
        return Err("bad_bounds".into());
    }
    Ok(Bounds { x, y, width, height })
}

fn valid_tab_id(id: &str) -> bool {
    id.len() <= 32
        && id
            .strip_prefix(TAB_PREFIX)
            .is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
}

fn with_tab(app: &AppHandle, label: &str, f: impl FnOnce(&mut TabInfo)) {
    let state = app.state::<BrowserState>();
    let mut g = state.inner.lock().unwrap();
    if let Some(t) = g.tabs.iter_mut().find(|t| t.id == label) {
        f(t);
    }
}

fn tab_builder(app: &AppHandle, label: &str, url: Url, proxy: Url) -> WebviewBuilder<tauri::Wry> {
    let app_nav = app.clone();
    let label_nav = label.to_string();
    let app_new = app.clone();
    let app_load = app.clone();
    let app_title = app.clone();
    WebviewBuilder::new(label, WebviewUrl::External(url))
        // Every request of the tab (documents, fetch, img, WebSocket) goes
        // through the filtering proxy (PR 5): DNS is resolved and checked there.
        .proxy_url(proxy)
        .incognito(true)
        // A click on a tab while the app's own page has the focus acts at
        // once instead of only moving the focus (and the other way round,
        // see acceptFirstMouse in tauri.conf.json).
        .accept_first_mouse(true)
        .devtools(cfg!(debug_assertions))
        .on_navigation(move |u| match check_navigation(u.as_str()) {
            // B10: agent tabs only go to sites opened in the session.
            Verdict::Allow => gate_navigation(&app_nav, &label_nav, u.as_str()),
            Verdict::Block(r) => {
                let note = format!("{} {}", reason_code(r), u.as_str());
                with_tab(&app_nav, &label_nav, |t| t.blocked = Some(note));
                false
            }
        })
        .on_new_window(move |u, _features| {
            if check_navigation(u.as_str()).allowed() {
                let state = app_new.state::<BrowserState>();
                let mut g = state.inner.lock().unwrap();
                if g.new_window_requests.len() < MAX_PENDING_NEW_WINDOWS {
                    g.new_window_requests.push(u.to_string());
                }
            }
            NewWindowResponse::Deny
        })
        .on_download(|_, _| false)
        .on_permission_request(|_, _| PermissionResponse::Deny)
        .on_page_load(move |wv, payload| {
            let url = payload.url().to_string();
            let loading = matches!(payload.event(), PageLoadEvent::Started);
            with_tab(&app_load, wv.label(), |t| {
                t.url = url;
                t.loading = loading;
                if loading && t.agent.is_some() && t.control.as_deref() == Some("human") {
                    // What the user opens while driving counts as opened.
                    if let Some(site) = conductloop_url_policy::sites::nav_needs_confirm(&t.sites, &t.url) {
                        if t.sites.len() < MAX_SITES {
                            t.sites.push(site);
                        }
                    }
                }
                if loading && !off_site(t) {
                    t.blocked = None;
                    t.pending_nav = None;
                } else if loading {
                    // Backstop (e.g. a server redirect not seen by
                    // on_navigation): the bridge will not read or act here.
                    t.pending_nav = Some(t.url.chars().take(2048).collect());
                    t.blocked = Some("new_site redirect".into());
                }
            });
        })
        .on_document_title_changed(move |wv, title| {
            let title: String = title.chars().take(300).collect();
            with_tab(&app_title, wv.label(), |t| t.title = title);
        })
}

/// Show only the active tab (when the /browser page is visible), at the
/// current bounds; hide everything else.
/// On macOS a child webview's position counts from the top of the window
/// frame, title bar included, while the page measures #viewport from the top
/// of its content. Tauri's sizes include the title bar too, so AppKit is asked
/// directly (frame minus contentLayoutRect). Without it the tab covered the
/// browser toolbar. AppKit wants the main thread: lib.rs calls this from setup
/// and from window events (both run there) and the value is cached.
pub fn refresh_title_bar(window: &tauri::Window) {
    let dy = measure_title_bar(window);
    if let Some(state) = window.try_state::<BrowserState>() {
        state.inner.lock().unwrap().title_bar = dy;
    }
}

#[cfg(target_os = "macos")]
fn measure_title_bar(window: &tauri::Window) -> f64 {
    let Ok(ptr) = window.ns_window() else { return 0.0 };
    if ptr.is_null() {
        return 0.0;
    }
    // SAFETY: Tauri hands out this window's live NSWindow; called on the main
    // thread (setup / window events) and only reads two rectangles.
    let ns = unsafe { &*(ptr as *const objc2_app_kit::NSWindow) };
    let (frame, content) = (ns.frame(), ns.contentLayoutRect());
    (frame.size.height - content.size.height).clamp(0.0, 100.0)
}

#[cfg(not(target_os = "macos"))]
fn measure_title_bar(_window: &tauri::Window) -> f64 {
    0.0
}

fn title_bar_offset(app: &AppHandle) -> f64 {
    app.state::<BrowserState>().inner.lock().unwrap().title_bar
}

fn apply_visibility(app: &AppHandle) -> Result<(), String> {
    let (tabs, active, bounds, visible) = {
        let state = app.state::<BrowserState>();
        let g = state.inner.lock().unwrap();
        let ids: Vec<String> = g.tabs.iter().map(|t| t.id.clone()).collect();
        (ids, g.active.clone(), g.bounds, g.visible)
    };
    let dy = title_bar_offset(app);
    for id in tabs {
        let Some(wv) = app.get_webview(&id) else { continue };
        if visible && active.as_deref() == Some(id.as_str()) {
            if let Some(b) = bounds {
                wv.set_position(LogicalPosition::new(b.x, b.y + dy)).map_err(|e| e.to_string())?;
                wv.set_size(LogicalSize::new(b.width, b.height)).map_err(|e| e.to_string())?;
            }
            wv.show().map_err(|e| e.to_string())?;
        } else {
            wv.hide().map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Show or hide the tabs. lib.rs hides them whenever the main webview starts
/// loading a page: the browser page's own `pagehide` call does not always get
/// through before the page is gone, and native tabs left visible are drawn
/// over the rest of the app.
pub fn set_visible(app: &AppHandle, visible: bool) -> Result<(), String> {
    app.state::<BrowserState>().inner.lock().unwrap().visible = visible;
    apply_visibility(app)
}

/// The tab shown on the browser page, if the page is showing tabs.
pub fn visible_active_tab(app: &AppHandle) -> Option<String> {
    let state = app.state::<BrowserState>();
    let g = state.inner.lock().unwrap();
    if g.visible {
        g.active.clone()
    } else {
        None
    }
}

/// Close tab `id` and activate the last remaining one.
pub fn close_tab(app: &AppHandle, id: &str) -> Result<(), String> {
    let wv = tab_webview(app, id)?;
    wv.close().map_err(|e| e.to_string())?;
    {
        let state = app.state::<BrowserState>();
        let mut g = state.inner.lock().unwrap();
        g.tabs.retain(|t| t.id != id);
        if g.active.as_deref() == Some(id) {
            g.active = g.tabs.last().map(|t| t.id.clone());
        }
    }
    apply_visibility(app)
}

fn tab_webview(app: &AppHandle, id: &str) -> Result<tauri::Webview, String> {
    if !valid_tab_id(id) {
        return Err("bad_tab".into());
    }
    app.get_webview(id).ok_or_else(|| "no_such_tab".to_string())
}

#[tauri::command]
pub async fn browser_open(
    app: AppHandle,
    state: tauri::State<'_, BrowserState>,
    url: Option<String>,
) -> Result<TabInfo, String> {
    let target = normalize_input(url.as_deref().unwrap_or(""))?;
    open_tab(&app, &state, target, None, Vec::new())
}

/// Create a tab. `agent = Some(session)` → an agent tab (PR 8): marked in the
/// strip, starts under agent control, and does not steal the active tab.
pub fn open_tab(
    app: &AppHandle,
    state: &BrowserState,
    target: Url,
    agent: Option<i64>,
    sites: Vec<String>,
) -> Result<TabInfo, String> {
    if !state.supported {
        return Err("macos_14_required".into());
    }
    let proxy = state.proxy_url.clone().ok_or("proxy_unavailable")?;
    let window = app.get_window(UI_WINDOW).ok_or("no_main_window")?;
    let (label, bounds, info) = {
        let mut g = state.inner.lock().unwrap();
        if g.tabs.len() >= MAX_TABS {
            return Err("too_many_tabs".into());
        }
        g.next += 1;
        let label = format!("{TAB_PREFIX}{}", g.next);
        let info = TabInfo {
            id: label.clone(),
            url: target.to_string(),
            // about:blank never reports a finished load: it would stay "…" forever.
            loading: target.as_str() != "about:blank",
            agent,
            control: agent.map(|_| CONTROL_AGENT.to_string()),
            sites: if agent.is_some() { sites } else { Vec::new() },
            ..Default::default()
        };
        // Registered under the same lock as the MAX_TABS check, so concurrent
        // opens cannot both pass it; and BEFORE the webview exists, so the
        // first on_navigation of an agent tab already sees its sites.
        g.tabs.push(info.clone());
        (label, g.bounds, info)
    };
    let b = bounds.unwrap_or(Bounds { x: 0.0, y: 0.0, width: 800.0, height: 600.0 });
    let builder = tab_builder(app, &label, target.clone(), proxy);
    let dy = title_bar_offset(app);
    if let Err(e) = window.add_child(builder, LogicalPosition::new(b.x, b.y + dy), LogicalSize::new(b.width, b.height)) {
        state.inner.lock().unwrap().tabs.retain(|t| t.id != label);
        return Err(e.to_string());
    }
    {
        let mut g = state.inner.lock().unwrap();
        if agent.is_none() || g.active.is_none() {
            g.active = Some(label);
        }
    }
    apply_visibility(app)?;
    // A new webview takes the keyboard once it has loaded: for an empty tab,
    // give it back to the URL bar afterwards, or what the user types next goes
    // into the blank page. (Done right away, the tab took it back.)
    if agent.is_none() && target.as_str() == "about:blank" {
        let app = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(300));
            if let Some(main) = app.get_webview(UI_WINDOW) {
                let _ = main.set_focus();
            }
        });
    }
    Ok(info)
}

/// The agent tab of `session`, if open.
pub fn agent_tab(state: &BrowserState, session: i64) -> Option<TabInfo> {
    state.inner.lock().unwrap().tabs.iter().find(|t| t.agent == Some(session)).cloned()
}

/// Pure: is `control` a valid value, and may the agent act under it?
pub fn agent_may_act(control: Option<&str>) -> bool {
    control == Some(CONTROL_AGENT)
}

/// PR 8: the user pauses the agent, takes control, or gives it back.
#[tauri::command]
pub async fn browser_agent_control(
    app: AppHandle,
    state: tauri::State<'_, BrowserState>,
    id: String,
    control: String,
) -> Result<(), String> {
    tab_webview(&app, &id)?;
    if !CONTROLS.contains(&control.as_str()) {
        return Err("bad_control".into());
    }
    let mut g = state.inner.lock().unwrap();
    let t = g.tabs.iter_mut().find(|t| t.id == id).ok_or("no_such_tab")?;
    if t.agent.is_none() {
        return Err("not_an_agent_tab".into());
    }
    t.control = Some(control);
    Ok(())
}

/// Run a script in a tab and wait (blocking, bounded) for its JSON result.
pub fn eval_json_blocking(wv: &tauri::Webview, script: &str, timeout: std::time::Duration) -> Result<serde_json::Value, String> {
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let tx = Mutex::new(Some(tx));
    wv.eval_with_callback(script, move |res| {
        if let Some(t) = tx.lock().unwrap().take() {
            let _ = t.send(res);
        }
    })
    .map_err(|e| e.to_string())?;
    let res = rx.recv_timeout(timeout).map_err(|_| "eval_timeout".to_string())?;
    if res.len() > MAX_EXTRACT_BYTES {
        return Err("result_too_large".into());
    }
    serde_json::from_str(&res).map_err(|_| "bad_result".to_string())
}

pub fn webview_for(app: &AppHandle, id: &str) -> Result<tauri::Webview, String> {
    tab_webview(app, id)
}

#[tauri::command]
pub async fn browser_navigate(app: AppHandle, id: String, url: String) -> Result<(), String> {
    let target = normalize_input(&url)?;
    let wv = tab_webview(&app, &id)?;
    // The user typing in the URL bar of an agent tab takes control of it.
    with_tab(&app, &id, |t| {
        if t.agent.is_some() {
            t.control = Some("human".into());
        }
    });
    wv.navigate(target).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn browser_back(app: AppHandle, id: String) -> Result<(), String> {
    // Tauri has no go_back(); history.back() is the documented-equivalent JS.
    tab_webview(&app, &id)?.eval("history.back()").map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn browser_forward(app: AppHandle, id: String) -> Result<(), String> {
    tab_webview(&app, &id)?.eval("history.forward()").map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn browser_reload(app: AppHandle, id: String) -> Result<(), String> {
    tab_webview(&app, &id)?.reload().map_err(|e| e.to_string())
}

/// Drop a closed tab from the state (bridge `close`).
pub fn forget_tab(app: &AppHandle, id: &str) {
    {
        let state = app.state::<BrowserState>();
        let mut g = state.inner.lock().unwrap();
        g.tabs.retain(|t| t.id != id);
        if g.active.as_deref() == Some(id) {
            g.active = g.tabs.last().map(|t| t.id.clone());
        }
    }
    let _ = apply_visibility(app);
}

#[tauri::command]
pub async fn browser_close(app: AppHandle, id: String) -> Result<(), String> {
    close_tab(&app, &id)
}

#[tauri::command]
pub async fn browser_activate(
    app: AppHandle,
    state: tauri::State<'_, BrowserState>,
    id: String,
) -> Result<(), String> {
    tab_webview(&app, &id)?;
    state.inner.lock().unwrap().active = Some(id);
    apply_visibility(&app)
}

#[tauri::command]
pub async fn browser_set_bounds(
    app: AppHandle,
    state: tauri::State<'_, BrowserState>,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let b = sanitize_bounds(x, y, width, height)?;
    state.inner.lock().unwrap().bounds = Some(b);
    apply_visibility(&app)
}

#[tauri::command]
pub async fn browser_set_visible(app: AppHandle, visible: bool) -> Result<(), String> {
    set_visible(&app, visible)
}

#[tauri::command]
pub async fn browser_list(state: tauri::State<'_, BrowserState>) -> Result<BrowserList, String> {
    let mut g = state.inner.lock().unwrap();
    Ok(BrowserList {
        tabs: g.tabs.clone(),
        active: g.active.clone(),
        new_window_requests: std::mem::take(&mut g.new_window_requests),
        recent_blocks: conductloop_url_policy::proxy::recent_blocks(std::time::Duration::from_secs(15)),
    })
}

// ── "Send to vault" (browser design PR 6) ───────────────────────────────

const READABILITY_JS: &str = include_str!("vendor/Readability.js");
const EXTRACT_JS: &str = include_str!("browser_extract.js");
/// Shared "is this visible to the user?" helper (agent snapshot + extraction).
pub const VISIBILITY_JS: &str = include_str!("visibility.js");
const MAX_EXTRACT_BYTES: usize = 3_000_000;

/// Extraction script with Readability inlined (built once).
pub fn extract_script() -> &'static str {
    static SCRIPT: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    SCRIPT.get_or_init(|| {
        EXTRACT_JS
            .replacen("/*__READABILITY__*/", READABILITY_JS, 1)
            .replacen("/*__CL_VISIBILITY__*/", VISIBILITY_JS, 1)
    })
}

/// Readable content of the page open in tab `id`: {url, title, byline,
/// site_name, lang, html, text, truncated, readability}. The UI posts it to
/// /api/web/clip, which re-validates and sanitises it.
#[tauri::command]
pub async fn browser_extract(app: AppHandle, id: String) -> Result<serde_json::Value, String> {
    let wv = tab_webview(&app, &id)?;
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let tx = Mutex::new(Some(tx));
    wv.eval_with_callback(extract_script(), move |res| {
        if let Some(t) = tx.lock().unwrap().take() {
            let _ = t.send(res);
        }
    })
    .map_err(|e| e.to_string())?;
    let res = tauri::async_runtime::spawn_blocking(move || rx.recv_timeout(std::time::Duration::from_secs(15)))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|_| "extract_timeout".to_string())?;
    if res.len() > MAX_EXTRACT_BYTES {
        return Err("extract_too_large".into());
    }
    let v: serde_json::Value = serde_json::from_str(&res).map_err(|_| "extract_bad_result".to_string())?;
    if !v.is_object() {
        return Err("extract_bad_result".into());
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_bar_input() {
        assert_eq!(normalize_input("example.com").unwrap().as_str(), "https://example.com/");
        assert_eq!(normalize_input("  https://example.com/a?b ").unwrap().as_str(), "https://example.com/a?b");
        assert_eq!(normalize_input("").unwrap().as_str(), "about:blank");
        assert_eq!(normalize_input("not a url").unwrap_err(), "invalid_url");
        assert_eq!(normalize_input("example.com:8080/x").unwrap().as_str(), "https://example.com:8080/x");
        assert!(has_scheme("javascript:alert(1)") && has_scheme("about:blank") && has_scheme("data:text/html,x"));
        assert!(!has_scheme("localhost:7777") && !has_scheme("example.com:8080") && !has_scheme("example.com"));
    }

    #[test]
    fn url_bar_refuses_local_and_odd_schemes() {
        for (input, code) in [
            ("localhost:7777", "blocked:local_name"),
            ("http://127.0.0.1:7777/", "blocked:loopback"),
            ("http://2130706433/", "blocked:loopback"),
            ("192.168.1.1", "blocked:private"),
            ("http://[::1]:7777/", "blocked:loopback"),
            ("file:///etc/passwd", "blocked:scheme"),
            ("tauri://localhost/", "blocked:scheme"),
            ("javascript:alert(1)", "blocked:scheme"),
            ("https://user:pw@example.com/", "blocked:userinfo"),
            ("nas.local", "blocked:local_name"),
        ] {
            assert_eq!(normalize_input(input).unwrap_err(), code, "{input}");
        }
    }

    #[test]
    fn bounds() {
        assert!(sanitize_bounds(0.0, 40.0, 800.0, 600.0).is_ok());
        assert!(sanitize_bounds(f64::NAN, 0.0, 800.0, 600.0).is_err());
        assert!(sanitize_bounds(0.0, 0.0, 10.0, 600.0).is_err());
        assert!(sanitize_bounds(-1.0, 0.0, 800.0, 600.0).is_err());
        assert!(sanitize_bounds(0.0, 0.0, f64::INFINITY, 600.0).is_err());
    }

    #[test]
    fn macos_version_parse() {
        assert_eq!(parse_macos_major("14.5\n"), Some(14));
        assert_eq!(parse_macos_major("26.0.1"), Some(26));
        assert_eq!(parse_macos_major("10.15.7"), Some(10));
        assert_eq!(parse_macos_major("garbage"), None);
    }

    #[test]
    fn proxy_starts_on_loopback() {
        let u = start_proxy().expect("proxy");
        assert_eq!(u.scheme(), "http");
        assert_eq!(u.host_str(), Some("127.0.0.1"));
    }

    #[test]
    fn extract_script_inlines_readability() {
        let s = extract_script();
        assert!(!s.contains("/*__READABILITY__*/"));
        assert!(s.contains("function Readability("));
        assert!(s.trim_end().ends_with("})()"));
        assert!(s.contains("[data-cl-hidden],script,style"));
        assert!(!s.contains("/*__CL_VISIBILITY__*/") && s.contains("function clVisibility()"));
    }

    #[test]
    fn agent_control_values() {
        assert!(agent_may_act(Some("agent")));
        for c in [None, Some("paused"), Some("human"), Some("Agent"), Some("")] {
            assert!(!agent_may_act(c), "{c:?}");
        }
        assert!(CONTROLS.contains(&CONTROL_AGENT));
    }

    #[test]
    fn tab_ids() {
        assert!(valid_tab_id("tab-1"));
        assert!(valid_tab_id("tab-42"));
        for bad in ["main", "tab-", "tab-x", "tab-1/../main", "TAB-1", "tab-1 "] {
            assert!(!valid_tab_id(bad), "{bad}");
        }
    }

    #[test]
    fn site_gate_for_agent_tabs() {
        let mut t = TabInfo {
            agent: Some(1),
            control: Some("agent".into()),
            sites: vec!["shop.example".into()],
            url: "https://shop.example/".into(),
            ..Default::default()
        };
        assert_eq!(site_gate(&t, "https://www.shop.example/x"), Ok(None));
        assert_eq!(site_gate(&t, "https://attacker.example/c"), Err("attacker.example".into()));
        assert_eq!(site_gate(&t, "about:blank"), Ok(None));
        t.control = Some("paused".into());
        assert!(site_gate(&t, "https://attacker.example/c").is_err());
        t.control = Some("human".into());
        assert_eq!(site_gate(&t, "https://other.example/"), Ok(Some("other.example".into())));
        assert!(!off_site(&t));
        t.url = "https://attacker.example/landed".into();
        assert!(off_site(&t));
        let user_tab = TabInfo { url: "https://any.example/".into(), ..Default::default() };
        assert_eq!(site_gate(&user_tab, "https://attacker.example/"), Ok(None));
        assert!(!off_site(&user_tab));
    }
}