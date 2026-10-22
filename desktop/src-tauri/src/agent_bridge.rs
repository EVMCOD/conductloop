//! Agent ⇄ browser bridge (browser design PR 8, decision B7).
//!
//! The session runner lives in the Python sidecar; the webviews live here.
//! They talk over a **Unix socket in a 0700 directory** of the app data dir,
//! with a per-launch **token** — no TCP, so nothing new on 127.0.0.1 and pages
//! in tabs (which cannot reach loopback anyway) have no way in. The path and
//! token reach the sidecar through its environment; the sandboxed code tools
//! get a fresh environment (#21 `sandbox.build_env`) and never see them.
//!
//! Protocol: one JSON request line → one JSON response line per connection.
//!   {"token", "op": "open"|"navigate"|"read"|"status"|"close", "session": n, "url"?}
//! Rust re-checks everything: URL policy (same as the URL bar), the tab must
//! be that session's agent tab, and the agent only acts while it has control
//! (the user can pause / take over at any time from the tab strip).
//! PR 9 adds `click` / `type` on a `ref` from the last read. Before acting,
//! Rust describes the element actually found in the page and re-applies the
//! effect classifier (`conductloop_url_policy::effects`): passwords are always
//! refused, and an effectful action without `confirmed` (the user approved it
//! in the backend) is refused with `confirmation_required:<reason>`.
//! There is still no eval op and no free selectors.
//!
//! New-site gate (B10): an agent tab only goes to registrable domains opened
//! in the session (`TabInfo.sites`, seeded by the backend from the user's
//! task). `open`/`navigate` to a new site without `confirmed` →
//! `confirmation_required:new_site`; navigations started by the page (links,
//! JS, forms, redirects) are stopped in `on_navigation` and reported in
//! `pending_nav`; a page that still lands off-site is neither read nor acted on.

use serde_json::{json, Value};

use crate::browser::{self, BrowserState};

pub const MAX_REQUEST: usize = 64 * 1024;
pub const OPS: [&str; 7] = [
    "open", "navigate", "read", "status", "close", "click", "type",
];
pub const MAX_TYPE_CHARS: usize = 2000;
const SNAPSHOT_JS: &str = include_str!("agent_snapshot.js");

/// Snapshot script with the visibility helper inlined (built once).
pub fn snapshot_script() -> &'static str {
    static SCRIPT: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    SCRIPT.get_or_init(|| SNAPSHOT_JS.replacen("/*__CL_VISIBILITY__*/", browser::VISIBILITY_JS, 1))
}
const ACT_JS: &str = include_str!("agent_act.js");

/// Constant-time comparison (token check).
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Validated request. Pure (unit-tested).
#[derive(Debug, PartialEq)]
pub struct Request {
    pub op: String,
    pub session: i64,
    pub url: Option<String>,
    pub elem: Option<i64>,
    pub text: Option<String>,
    pub submit: bool,
    pub confirmed: bool,
    pub sites: Vec<String>,
}

pub const MAX_SEED_SITES: usize = 32;

pub fn parse_request(line: &str, token: &str) -> Result<Request, String> {
    if line.len() > MAX_REQUEST {
        return Err("too_large".into());
    }
    let v: Value = serde_json::from_str(line).map_err(|_| "bad_json".to_string())?;
    let t = v.get("token").and_then(Value::as_str).unwrap_or("");
    if token.is_empty() || !ct_eq(t.as_bytes(), token.as_bytes()) {
        return Err("unauthorized".into());
    }
    let op = v.get("op").and_then(Value::as_str).unwrap_or("");
    if !OPS.contains(&op) {
        return Err("unknown_op".into());
    }
    let session = v
        .get("session")
        .and_then(Value::as_i64)
        .filter(|s| *s > 0)
        .ok_or("bad_session")?;
    let url = v.get("url").and_then(Value::as_str).map(str::to_string);
    if matches!(op, "open" | "navigate") && url.is_none() {
        return Err("url_required".into());
    }
    let elem = v
        .get("ref")
        .and_then(Value::as_i64)
        .filter(|r| *r > 0 && *r < 100_000);
    if matches!(op, "click" | "type") && elem.is_none() {
        return Err("ref_required".into());
    }
    let text = v.get("text").and_then(Value::as_str).map(str::to_string);
    if op == "type"
        && text
            .as_ref()
            .is_none_or(|t| t.chars().count() > MAX_TYPE_CHARS)
    {
        return Err("bad_text".into());
    }
    Ok(Request {
        op: op.to_string(),
        session,
        url,
        elem,
        text,
        submit: v.get("submit").and_then(Value::as_bool).unwrap_or(false),
        confirmed: v.get("confirmed").and_then(Value::as_bool).unwrap_or(false),
        sites: v
            .get("sites")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .filter(|h| !h.is_empty() && h.len() <= 253)
                    .map(conductloop_url_policy::sites::registrable_domain)
                    .take(MAX_SEED_SITES)
                    .collect()
            })
            .unwrap_or_default(),
    })
}

/// Script for one element op; args are serialised JSON (never spliced text).
pub fn act_script(op: &str, elem: i64, text: Option<&str>, submit: bool) -> String {
    let args = json!({"op": op, "ref": elem, "text": text, "submit": submit});
    ACT_JS.replacen("__ARGS__", &args.to_string(), 1)
}

/// Effect of `op` on the element described by the page (pure, unit-tested).
pub fn effect_of(op: &str, submit: bool, d: &Value) -> conductloop_url_policy::effects::Effect {
    use conductloop_url_policy::effects::{classify, Action, Element, Page};
    let s = |k: &str| d.get(k).and_then(Value::as_str).unwrap_or("");
    let b = |k: &str| d.get(k).and_then(Value::as_bool).unwrap_or(false);
    let el = Element {
        tag: s("tag"),
        typ: s("type"),
        role: s("role"),
        name: s("name"),
        autocomplete: s("autocomplete"),
        in_form: b("in_form"),
        href: d.get("href").and_then(Value::as_str),
        payment: b("payment"),
    };
    let page = Page {
        host: s("page_host"),
        has_password: b("has_password"),
        has_payment: b("has_payment"),
    };
    let action = if op == "type" {
        Action::Type { submit }
    } else {
        Action::Click
    };
    classify(action, &el, &page)
}

/// Navigate the agent tab `t`; an approved new site is added first, so the
/// tab's own `on_navigation` lets it through.
fn navigate_tab(
    app: &tauri::AppHandle,
    state: &BrowserState,
    req: &Request,
    t: &browser::TabInfo,
    target: tauri::Url,
    new_site: Option<String>,
) -> Value {
    let res = browser::webview_for(app, &t.id).and_then(|wv| {
        if let Some(site) = new_site {
            browser::allow_site(state, req.session, site);
        }
        wv.navigate(target).map_err(|e| e.to_string())
    });
    match res {
        Ok(()) => json!({"ok": true, "tab": tab_json(t)}),
        Err(e) => json!({"ok": false, "error": e}),
    }
}

fn tab_json(t: &browser::TabInfo) -> Value {
    json!({"open": true, "id": t.id, "url": t.url, "title": t.title, "loading": t.loading,
           "blocked": t.blocked, "control": t.control, "sites": t.sites,
           "pending_nav": t.pending_nav, "off_site": browser::off_site(t)})
}

pub fn handle(app: &tauri::AppHandle, req: Request) -> Value {
    use tauri::Manager;
    let state = app.state::<BrowserState>();
    let tab = browser::agent_tab(&state, req.session);
    let err = |e: &str| json!({"ok": false, "error": e});
    match req.op.as_str() {
        "status" => match tab {
            Some(t) => json!({"ok": true, "tab": tab_json(&t)}),
            None => json!({"ok": true, "tab": {"open": false}}),
        },
        "open" | "navigate" => {
            let target = match browser::normalize_input(req.url.as_deref().unwrap_or("")) {
                Ok(u) => u,
                Err(e) => return err(&e),
            };
            let target_site = conductloop_url_policy::sites::http_host(target.as_str())
                .map(|h| conductloop_url_policy::sites::registrable_domain(&h));
            let new_site = |known: &[String]| target_site.clone().filter(|s| !known.contains(s));
            match tab {
                None if req.op == "open" => {
                    let mut sites = req.sites.clone();
                    if let Some(site) = new_site(&sites) {
                        if !req.confirmed {
                            return err("confirmation_required:new_site");
                        }
                        sites.push(site);
                    }
                    match browser::open_tab(app, &state, target, Some(req.session), sites) {
                        Ok(t) => json!({"ok": true, "tab": tab_json(&t)}),
                        Err(e) => err(&e),
                    }
                }
                None => err("tab_closed"),
                Some(t) if !browser::agent_may_act(t.control.as_deref()) => err("not_in_control"),
                Some(mut t) => {
                    // Sites the user named in later messages (backend seeds).
                    browser::add_sites(&state, req.session, &req.sites);
                    t.sites.extend(req.sites.iter().cloned());
                    if new_site(&t.sites).is_some() && !req.confirmed {
                        return err("confirmation_required:new_site");
                    }
                    navigate_tab(app, &state, &req, &t, target, new_site(&t.sites))
                }
            }
        }

        "read" => match tab {
            None => err("tab_closed"),
            Some(t) if !browser::agent_may_act(t.control.as_deref()) => err("not_in_control"),
            Some(t) if browser::off_site(&t) => err("off_site"),
            Some(t) if t.loading => err("loading"),
            Some(t) => match browser::webview_for(app, &t.id).and_then(|wv| {
                browser::eval_json_blocking(
                    &wv,
                    snapshot_script(),
                    std::time::Duration::from_secs(15),
                )
            }) {
                Ok(v) if v.is_object() => json!({"ok": true, "snapshot": v, "tab": tab_json(&t)}),
                Ok(_) => err("bad_result"),
                Err(e) => err(&e),
            },
        },
        "click" | "type" => match tab {
            None => err("tab_closed"),
            Some(t) if !browser::agent_may_act(t.control.as_deref()) => err("not_in_control"),
            Some(t) if browser::off_site(&t) => err("off_site"),
            Some(t) if t.loading => err("loading"),
            Some(t) => {
                use conductloop_url_policy::effects::{code, Effect};
                let wv = match browser::webview_for(app, &t.id) {
                    Ok(wv) => wv,
                    Err(e) => return err(&e),
                };
                let elem = req.elem.unwrap_or(0);
                let timeout = std::time::Duration::from_secs(15);
                let desc = match browser::eval_json_blocking(
                    &wv,
                    &act_script("describe", elem, None, false),
                    timeout,
                ) {
                    Ok(d) if d.get("ok").and_then(Value::as_bool) == Some(true) => d,
                    Ok(d) => {
                        return err(d
                            .get("error")
                            .and_then(Value::as_str)
                            .unwrap_or("bad_result"))
                    }
                    Err(e) => return err(&e),
                };
                let effect = effect_of(&req.op, req.submit, &desc);
                match effect {
                    Effect::Deny(r) => return err(&format!("denied:{r}")),
                    Effect::Confirm(r) if !req.confirmed => {
                        return err(&format!("confirmation_required:{r}"))
                    }
                    // The user approved following THIS cross-domain link:
                    // its site (from the live element) is now opened.
                    Effect::Confirm("cross_domain") => {
                        if let Some(site) = desc
                            .get("href")
                            .and_then(Value::as_str)
                            .and_then(conductloop_url_policy::sites::http_host)
                        {
                            let site = conductloop_url_policy::sites::registrable_domain(&site);
                            browser::allow_site(&state, req.session, site);
                        }
                    }
                    _ => {}
                }
                match browser::eval_json_blocking(
                    &wv,
                    &act_script(&req.op, elem, req.text.as_deref(), req.submit),
                    timeout,
                ) {
                    Ok(r) if r.get("ok").and_then(Value::as_bool) == Some(true) => {
                        json!({"ok": true, "effect": code(effect), "tab": tab_json(&t)})
                    }
                    Ok(r) => err(r
                        .get("error")
                        .and_then(Value::as_str)
                        .unwrap_or("bad_result")),
                    Err(e) => err(&e),
                }
            }
        },
        "close" => match tab {
            None => json!({"ok": true}),
            Some(t) => match browser::webview_for(app, &t.id)
                .and_then(|wv| wv.close().map_err(|e| e.to_string()))
            {
                Ok(()) => {
                    browser::forget_tab(app, &t.id);
                    json!({"ok": true})
                }
                Err(e) => err(&e),
            },
        },
        _ => err("unknown_op"),
    }
}

/// Bind the socket and serve it on a background thread. Returns (path, token)
/// for the sidecar's environment, or None (agent browsing then unavailable).
#[cfg(unix)]
pub fn start(
    app: tauri::AppHandle,
    dir: std::path::PathBuf,
    token: String,
) -> Option<std::path::PathBuf> {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixListener;

    std::fs::create_dir_all(&dir).ok()?;
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).ok()?;
    let path = dir.join("b.sock");
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).ok()?;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).ok()?;
    std::thread::Builder::new()
        .name("cl-agent-bridge".into())
        .spawn(move || {
            for stream in listener.incoming().flatten() {
                let app = app.clone();
                let token = token.clone();
                let _ = std::thread::Builder::new()
                    .name("cl-agent-conn".into())
                    .spawn(move || {
                        let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(10)));
                        let mut line = String::new();
                        let mut reader = BufReader::new((&stream).take(MAX_REQUEST as u64 + 1));
                        let out = match reader.read_line(&mut line) {
                            Ok(_) => match parse_request(line.trim_end(), &token) {
                                Ok(req) => handle(&app, req),
                                Err(e) => json!({"ok": false, "error": e}),
                            },
                            Err(_) => json!({"ok": false, "error": "read_error"}),
                        };
                        let mut w = &stream;
                        let _ = w.write_all(format!("{out}\n").as_bytes());
                    });
            }
        })
        .ok()?;
    Some(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    const T: &str = "0123456789abcdef0123456789abcdef";

    #[test]
    fn requests_need_the_token() {
        assert_eq!(
            parse_request(r#"{"op":"status","session":1}"#, T).unwrap_err(),
            "unauthorized"
        );
        assert_eq!(
            parse_request(r#"{"token":"nope","op":"status","session":1}"#, T).unwrap_err(),
            "unauthorized"
        );
        assert_eq!(
            parse_request(
                &format!(r#"{{"token":"{T}","op":"status","session":1}}"#),
                ""
            )
            .unwrap_err(),
            "unauthorized"
        );
        let ok = parse_request(
            &format!(r#"{{"token":"{T}","op":"status","session":3}}"#),
            T,
        )
        .unwrap();
        assert_eq!(
            ok,
            Request {
                op: "status".into(),
                session: 3,
                url: None,
                elem: None,
                text: None,
                submit: false,
                confirmed: false,
                sites: vec![]
            }
        );
    }

    #[test]
    fn requests_are_validated() {
        let r = |body: &str| parse_request(&format!(r#"{{"token":"{T}",{body}}}"#), T);
        assert_eq!(r(r#""op":"eval","session":1"#).unwrap_err(), "unknown_op");
        assert_eq!(
            r(r#""op":"click","session":1"#).unwrap_err(),
            "ref_required"
        );
        assert_eq!(
            r(r#""op":"type","session":1,"ref":3"#).unwrap_err(),
            "bad_text"
        );
        let long = "x".repeat(MAX_TYPE_CHARS + 1);
        assert_eq!(
            r(&format!(
                r#""op":"type","session":1,"ref":3,"text":"{long}""#
            ))
            .unwrap_err(),
            "bad_text"
        );
        let ok = r(r#""op":"type","session":1,"ref":3,"text":"hi","submit":true,"confirmed":true"#)
            .unwrap();
        assert!(
            ok.submit && ok.confirmed && ok.elem == Some(3) && ok.text.as_deref() == Some("hi")
        );
        assert!(!r(r#""op":"click","session":1,"ref":3"#).unwrap().confirmed);
        assert_eq!(r(r#""op":"read","session":0"#).unwrap_err(), "bad_session");
        assert_eq!(r(r#""op":"read""#).unwrap_err(), "bad_session");
        assert_eq!(r(r#""op":"open","session":1"#).unwrap_err(), "url_required");
        assert_eq!(
            r(r##""op":"open","session":1,"url":"https://example.com""##)
                .unwrap()
                .url
                .as_deref(),
            Some("https://example.com")
        );
        assert_eq!(parse_request("not json", T).unwrap_err(), "bad_json");
        assert_eq!(
            parse_request(&"x".repeat(MAX_REQUEST + 1), T).unwrap_err(),
            "too_large"
        );
    }

    #[test]
    fn seed_sites_are_normalised_and_capped() {
        let many: Vec<String> = (0..40).map(|i| format!("\"s{i}.example\"")).collect();
        let line = format!(
            r#"{{"token":"{T}","op":"open","session":1,"url":"https://www.a.example/","sites":["WWW.Shop.Example","news.bbc.co.uk",1,""]}}"#
        );
        let r = parse_request(&line, T).unwrap();
        assert_eq!(
            r.sites,
            vec!["shop.example".to_string(), "bbc.co.uk".to_string()]
        );
        let line = format!(
            r#"{{"token":"{T}","op":"open","session":1,"url":"https://a.example/","sites":[{}]}}"#,
            many.join(",")
        );
        assert_eq!(parse_request(&line, T).unwrap().sites.len(), MAX_SEED_SITES);
    }

    #[test]
    fn act_script_embeds_json_args() {
        let s = act_script("type", 4, Some("a\"); alert(1); (\""), true);
        assert!(!s.contains("__ARGS__"));
        assert!(s.contains(r#""text":"a\"); alert(1); (\"""#));
        assert!(s.trim_end().ends_with("})"));
    }

    #[test]
    fn rust_reclassifies_the_live_element() {
        use conductloop_url_policy::effects::Effect;
        let d = json!({"tag": "button", "type": "", "name": "Search", "in_form": true, "page_host": "shop.example"});
        assert_eq!(effect_of("click", false, &d), Effect::Confirm("submit"));
        let pw = json!({"tag": "input", "type": "password", "page_host": "x.example"});
        assert_eq!(
            effect_of("type", false, &pw),
            Effect::Deny("password_field")
        );
        let link = json!({"tag": "a", "href": "https://shop.example/p", "name": "Details", "page_host": "www.shop.example"});
        assert_eq!(effect_of("click", false, &link), Effect::Allow);
        let js = json!({"tag": "a", "href": "javascript:void(0)", "name": "x", "page_host": "shop.example"});
        assert_eq!(
            effect_of("click", false, &js),
            Effect::Confirm("non_http_link")
        );
    }

    #[test]
    fn ct_eq_works() {
        assert!(ct_eq(b"abc", b"abc"));
        assert!(!ct_eq(b"abc", b"abd"));
        assert!(!ct_eq(b"abc", b"ab"));
    }

    #[test]
    fn snapshot_script_is_read_only() {
        assert!(snapshot_script().trim_end().ends_with("})()"));
        assert!(snapshot_script().contains("data-cl-ref"));
        assert!(!snapshot_script().contains("/*__CL_VISIBILITY__*/"));
        assert!(snapshot_script().contains("function clVisibility()"));
        // never reads field values into the result, only whether they are filled
        assert!(!snapshot_script().contains("value: el.value"));
        for forbidden in [
            ".click(",
            "dispatchEvent",
            ".submit(",
            "fetch(",
            "XMLHttpRequest",
        ] {
            assert!(!snapshot_script().contains(forbidden), "{forbidden}");
        }
    }
}
