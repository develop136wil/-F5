use std::io::Write;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

#[tauri::command]
pub async fn open_setup_manual(app: tauri::AppHandle, topic: String) -> Result<(), String> {
    if !["google", "signup", "notion", "policy", "news", "policy-register", "news-register"].contains(&topic.as_str()) {
        return Err("알 수 없는 매뉴얼 단계입니다.".into());
    }
    let path = format!("manual.html?topic={topic}");
    if let Some(window) = app.get_webview_window("setup-manual") {
        let url = window.url().map_err(|e| e.to_string())?.join(&path).map_err(|e| e.to_string())?;
        window.navigate(url).map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
    } else {
        let manual = WebviewWindowBuilder::new(&app, "setup-manual", WebviewUrl::App(path.into()))
            .title("복지F5 설정 매뉴얼").inner_size(720.0, 560.0)
            .min_inner_size(420.0, 360.0).resizable(true)
            .build().map_err(|e| e.to_string())?;
        // Tile only when both windows fit without shrinking the tutorial below its minimum.
        // Restore the original main-window geometry when the independent manual is closed.
        if let Some(main) = app.get_webview_window("main") {
            if let (Ok(Some(monitor)), Ok(old_position), Ok(old_size), Ok(maximized)) =
                (main.current_monitor(), main.outer_position(), main.inner_size(), main.is_maximized()) {
                let area = monitor.work_area();
                let scale = monitor.scale_factor();
                let width = area.size.width as f64 / scale;
                let height = area.size.height as f64 / scale;
                let x = area.position.x as f64 / scale;
                let y = area.position.y as f64 / scale;
                if width >= 1380.0 && height >= 720.0 {
                    let left = (width * 0.55).max(870.0).min(1000.0);
                    if maximized { main.unmaximize().map_err(|e| e.to_string())?; }
                    let restore_main = main.clone();
                    manual.on_window_event(move |event| {
                        if matches!(event, tauri::WindowEvent::Destroyed) {
                            let _ = restore_main.set_size(old_size);
                            let _ = restore_main.set_position(old_position);
                            if maximized { let _ = restore_main.maximize(); }
                        }
                    });
                    main.set_size(tauri::LogicalSize::new(left, height - 70.0)).map_err(|e| e.to_string())?;
                    main.set_position(tauri::LogicalPosition::new(x + 8.0, y + 8.0)).map_err(|e| e.to_string())?;
                    manual.set_size(tauri::LogicalSize::new(width - left - 40.0, (height - 90.0).min(660.0))).map_err(|e| e.to_string())?;
                    manual.set_position(tauri::LogicalPosition::new(x + left + 24.0, y + 8.0)).map_err(|e| e.to_string())?;
                } else {
                    manual.set_position(tauri::LogicalPosition::new(x + (width - 740.0).max(0.0), y + 16.0)).map_err(|e| e.to_string())?;
                }
            }
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn export_setup_manual(app: tauri::AppHandle, open: bool) -> Result<String, String> {
    let pdf = app.asset_resolver().get("manual/welfare-f5-manual.pdf".into())
        .ok_or("설치본에서 매뉴얼 PDF를 찾지 못했습니다.")?;
    if !pdf.bytes.starts_with(b"%PDF-") { return Err("매뉴얼 파일 형식이 올바르지 않습니다.".into()); }
    let folder = if open { app.path().app_cache_dir() } else { app.path().download_dir() }.map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&folder).map_err(|e| e.to_string())?;
    // Never overwrite a user's existing manual. No caller-supplied paths or bytes.
    for number in 0..1000 {
        let name = if number == 0 { "복지F5 매뉴얼.pdf".to_string() } else { format!("복지F5 매뉴얼 ({number}).pdf") };
        let path = folder.join(name);
        let mut file = match std::fs::OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.to_string()),
        };
        file.write_all(&pdf.bytes).map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        drop(file);
        let path = path.to_string_lossy().to_string();
        if open { app.opener().open_path(&path, None::<&str>).map_err(|e| e.to_string())?; }
        return Ok(path);
    }
    Err("같은 이름의 매뉴얼이 너무 많습니다. 다운로드 폴더를 확인해주세요.".into())
}
