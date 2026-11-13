mod build_guard;

fn main() {
    // Use the same platform config + CLI merge as Tauri itself. A Windows
    // drive path can deserialize as Url and silently omit all embedded assets.
    let root = std::env::current_dir().expect("build directory");
    let target = tauri_utils::platform::Target::from_triple(&std::env::var("TARGET").unwrap());
    let (mut value, _) = tauri_utils::config::parse::read_from(target, &root).expect("Tauri config");
    if let Ok(overlay) = std::env::var("TAURI_CONFIG") {
        json_patch::merge(&mut value, &serde_json::from_str(&overlay).expect("Tauri config override"));
    }
    let config: tauri_utils::config::Config = serde_json::from_value(value).expect("Tauri config schema");
    build_guard::validate(config.build.frontend_dist.as_ref(), &root,
        std::env::var("PROFILE").as_deref() == Ok("release")).expect("Unsafe frontend packaging");
    // Recompile Windows resources when the application icon changes.
    println!("cargo:rerun-if-changed=icons/icon.ico");
    tauri_build::build()
}
