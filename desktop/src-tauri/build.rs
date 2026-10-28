fn main() {
    // `macos-proxy` makes wry call nw_proxy_config_create_* (Network.framework,
    // macOS 14+). The app's minimum is macOS 13.0 (design B4): weak-link the
    // framework so the binary still loads on macOS 13, where the browser
    // is disabled at runtime (browser.rs `os_supported`) and those symbols are
    // never called.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        println!("cargo:rustc-link-arg=-Wl,-weak_framework,Network");
    }
    // Restrict the app's own commands to the ones listed here. Without an
    // AppManifest every `invoke_handler` command is allowed for every window
    // and webview (https://v2.tauri.app/security/capabilities/). With it, each
    // command needs an explicit `allow-<command>` permission in a capability,
    // so a future browser-tab webview (no capability) can't call them.
    // Keep this list in sync with `generate_handler!` in src/lib.rs
    // (tests/test_tauri_capabilities.py checks it).
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
            "backend_url",
            "check_for_update",
            "install_update",
            "browser_open",
            "browser_navigate",
            "browser_back",
            "browser_forward",
            "browser_reload",
            "browser_close",
            "browser_activate",
            "browser_set_bounds",
            "browser_set_visible",
            "browser_list",
            "browser_extract",
            "browser_agent_control",
        ])),
    )
    .expect("failed to run tauri-build");
}
