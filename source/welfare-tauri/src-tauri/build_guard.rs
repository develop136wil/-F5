use std::path::Path;
use tauri_utils::config::FrontendDist;

pub fn validate(dist: Option<&FrontendDist>, root: &Path, require_files: bool) -> Result<(), String> {
    let path = match dist {
        Some(FrontendDist::Directory(path)) if path.is_relative() && !path.has_root() => path,
        _ => return Err("복지F5 frontendDist must be a relative directory, never a URL or Windows drive URL".into()),
    };
    if require_files && !root.join(path).join("index.html").is_file() {
        return Err("복지F5 embedded frontend index.html is missing".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_drive_urls_and_remote_frontends() {
        for value in ["D:/candidate/frontend", "file:///D:/candidate/frontend", "https://example.invalid", "/absolute/frontend"] {
            let dist: FrontendDist = serde_json::from_value(serde_json::json!(value)).unwrap();
            assert!(validate(Some(&dist), Path::new("."), false).is_err(), "{value}");
        }
        assert!(validate(None, Path::new("."), false).is_err());
    }
    #[test]
    fn accepts_relative_directory_but_requires_release_entry() {
        let dist: FrontendDist = serde_json::from_value(serde_json::json!("../dist")).unwrap();
        assert!(validate(Some(&dist), Path::new("."), false).is_ok());
        assert!(validate(Some(&dist), Path::new("nonexistent-release-fixture"), true).is_err());
    }
}
