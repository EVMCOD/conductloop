//! Updates: a quiet check a little after start-up (release builds) and
//! "Check for Updates…" in the app menu.
//!
//! Up to 1.2.1 nothing ever called the updater: there was no menu item, no
//! check at start-up, and the UI page (served from localhost:7777) has no
//! permission for the updater commands. So no installed app could update
//! itself. Nothing is installed without the user saying yes in the dialog.

use std::time::Duration;

use tauri::AppHandle;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

/// The window and the server come first.
const STARTUP_DELAY: Duration = Duration::from_secs(20);

/// Look for an update once, a little after start-up. Silent unless there is one.
pub fn check_after_startup(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(STARTUP_DELAY);
        tauri::async_runtime::block_on(check(app, false));
    });
}

/// "Check for Updates…": also says when there is nothing new or the check fails.
pub fn check_from_menu(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || tauri::async_runtime::block_on(check(app, true)));
}

// Runs on its own thread: the dialogs block until answered.
async fn check(app: AppHandle, interactive: bool) {
    let current = app.package_info().version.to_string();
    let found = match app.updater() {
        Ok(updater) => updater.check().await.map_err(|e| e.to_string()),
        Err(e) => Err(e.to_string()),
    };
    match found {
        Ok(Some(update)) => {
            let install = app
                .dialog()
                .message(format!(
                    "ConductLoop {} is available (this is {current}).\n\n\
                     Install it now? The app restarts when it is done. Your notes, \
                     settings and keys stay where they are.",
                    update.version
                ))
                .title("Update available")
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Install and restart".into(),
                    "Later".into(),
                ))
                .blocking_show();
            if !install {
                return;
            }
            match update.download_and_install(|_, _| {}, || {}).await {
                Ok(()) => app.restart(),
                Err(e) => tell(
                    &app,
                    MessageDialogKind::Error,
                    "Update failed",
                    &format!("The update could not be installed: {e}"),
                ),
            }
        }
        Ok(None) if interactive => tell(
            &app,
            MessageDialogKind::Info,
            "No updates",
            &format!("ConductLoop {current} is the latest version."),
        ),
        Err(e) if interactive => tell(
            &app,
            MessageDialogKind::Error,
            "Could not check for updates",
            &e,
        ),
        _ => {}
    }
}

fn tell(app: &AppHandle, kind: MessageDialogKind, title: &str, message: &str) {
    app.dialog()
        .message(message)
        .title(title)
        .kind(kind)
        .buttons(MessageDialogButtons::Ok)
        .blocking_show();
}
