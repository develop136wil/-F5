// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if std::env::args().skip(1).collect::<Vec<_>>() == ["--verify-embedded-ui"] {
        match welfare_tauri_lib::verify_embedded_ui() {
            Ok(count) => { println!("embedded_ui_ok assets={count}"); std::process::exit(0); }
            Err(error) => { eprintln!("embedded_ui_failed: {error}"); std::process::exit(2); }
        }
    }
    welfare_tauri_lib::run()
}
