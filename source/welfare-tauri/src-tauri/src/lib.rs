mod desktop;
mod manual;
#[cfg(test)]
#[path = "../build_guard.rs"]
mod build_guard;
use tauri::Manager;

/// Read-only release probe: no window, sidecar, settings or database is opened.
pub fn verify_embedded_ui() -> Result<usize, String> {
    let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
    let assets = context.assets();
    let index = assets.get(&"index.html".into()).ok_or("embedded index.html missing")?;
    let html = std::str::from_utf8(&index).map_err(|_| "invalid HTML encoding")?;
    if !html.contains("id=\"root\"") { return Err("React root missing".into()); }
    let mut scripts = 0;
    let mut styles = 0;
    for path in html.split('"').filter(|part| part.starts_with("/assets/")) {
        let asset = assets.get(&path.into()).ok_or_else(|| format!("embedded asset missing: {path}"))?;
        if asset.is_empty() { return Err(format!("empty embedded asset: {path}")); }
        if path.ends_with(".js") { scripts += 1; }
        if path.ends_with(".css") { styles += 1; }
    }
    if scripts == 0 || styles == 0 { return Err("built entry script or stylesheet missing".into()); }
    let manual = assets.get(&"manual.html".into()).ok_or("manual entry missing")?;
    let manual_html = std::str::from_utf8(&manual).map_err(|_| "invalid manual HTML")?;
    for path in manual_html.split('"').filter(|part| part.starts_with("/assets/")) {
        if assets.get(&path.into()).is_none() { return Err(format!("manual asset missing: {path}")); }
    }
    let pdf = assets.get(&"manual/welfare-f5-manual.pdf".into()).ok_or("manual PDF missing")?;
    if !pdf.starts_with(b"%PDF-") { return Err("invalid manual PDF".into()); }
    for page in [11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24] {
        if assets.get(&format!("manual/page-{page}.webp").into()).is_none() {
            return Err(format!("manual page missing: {page}"));
        }
    }
    Ok(assets.iter().count())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_opener::init())
        .manage(desktop::DesktopState::default())
        .setup(|app| { desktop::start(app.handle()); Ok(()) })
        .on_window_event(|window, event| {
            // Auxiliary manual windows must never trigger the backend shutdown flow.
            if window.label() != "main" { return; }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if !desktop::may_exit(window.app_handle()) {
                    api.prevent_close();
                    desktop::request_close(window.app_handle());
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            desktop::desktop_connection, desktop::desktop_status, desktop::desktop_shutdown,
            desktop::desktop_close_ack, manual::open_setup_manual, manual::export_setup_manual
        ])
        .build(tauri::generate_context!())
        .expect("error while building application");
    app.run(|handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !desktop::may_exit(handle) {
                api.prevent_exit();
                desktop::request_close(handle);
            }
        }
    });
}
