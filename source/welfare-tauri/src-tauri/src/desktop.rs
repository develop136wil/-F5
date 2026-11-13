use serde::Serialize;
use serde_json::{json, Value};
use std::{net::TcpListener, sync::Mutex, time::Duration};
use tauri::{Emitter, Manager};
use tauri_plugin_shell::{process::CommandEvent, ShellExt};

const BASE: &str = "http://127.0.0.1:8421";

#[derive(Default)]
pub struct DesktopState { inner: Mutex<Session> }
#[derive(Default)]
struct Session {
    token: String,
    instance: String,
    error: Option<String>,
    child: Option<tauri_plugin_shell::process::CommandChild>,
    shutdown_requested: bool,
    shutdown_confirmed: bool,
    close_request: CloseRequest,
    _instance_guard: Option<InstanceGuard>,
}
struct InstanceGuard(usize);
#[cfg(windows)]
impl Drop for InstanceGuard {
    fn drop(&mut self) { unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0 as _); } }
}
fn acquire_instance() -> Result<InstanceGuard, String> {
    acquire_named_instance("Local\\SearchMaster.WelfareF5.Desktop")
}
fn acquire_named_instance(name: &str) -> Result<InstanceGuard, String> {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::{Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS}, System::Threading::CreateMutexW};
        let name: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
        if handle.is_null() { return Err("중복 실행 여부를 확인하지 못했습니다. 앱을 다시 실행해주세요.".into()); }
        if GetLastError() == ERROR_ALREADY_EXISTS {
            CloseHandle(handle);
            return Err("복지F5가 이미 실행 중입니다. 작업 표시줄에서 기존 창을 확인해주세요.".into());
        }
        Ok(InstanceGuard(handle as usize))
    }
    #[cfg(not(windows))]
    { Ok(InstanceGuard(0)) }
}
pub fn start(app: &tauri::AppHandle) {
    if let Err(message) = start_inner(app) {
        app.state::<DesktopState>().inner.lock().unwrap().error = Some(message);
    }
}
fn start_inner(app: &tauri::AppHandle) -> Result<(), String> {
    let guard = acquire_instance()?;
    let probe = TcpListener::bind("127.0.0.1:8421")
        .map_err(|_| "연결 포트(8421)를 사용할 수 없습니다. 다른 프로그램이 사용 중이거나, 창을 닫은 뒤 이전 백엔드만 남아 있을 수 있습니다. 기존 창이 있다면 먼저 닫아주세요. 창이 없는데도 계속되면 진단을 요청해주세요. 안전을 위해 다른 프로세스는 종료하지 않았습니다.".to_string())?;
    let instance = uuid::Uuid::new_v4().to_string();
    let token = uuid::Uuid::new_v4().to_string();
    let command = app.shell().sidecar("api-server").map_err(|_| "내장 실행 파일을 찾지 못했습니다.".to_string())?
        .env("WELFARE_DESKTOP_INSTANCE", &instance).env("WELFARE_DESKTOP_TOKEN", &token)
        .env("WELFARE_DESKTOP_PARENT_PID", std::process::id().to_string()).env("WELFARE_API_PORT", "8421");
    // If another process wins this handoff race, the child fails its exclusive bind.
    drop(probe);
    let (mut events, child) = command.spawn().map_err(|_| "내장 프로그램을 시작하지 못했습니다.".to_string())?;
    {
        let state = app.state::<DesktopState>();
        let mut session = state.inner.lock().unwrap();
        session.instance = instance;
        session.token = token;
        session.child = Some(child);
        session._instance_guard = Some(guard);
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            if let CommandEvent::Terminated(_) = event {
                let closing = {
                    let state = handle.state::<DesktopState>();
                    let mut session = state.inner.lock().unwrap();
                    session.child = None;
                    session.error = Some("내장 프로그램이 종료되었습니다. 앱을 닫고 다시 실행해주세요.".into());
                    session.shutdown_requested
                };
                if closing { handle.exit(0); }
                break;
            }
        }
    });
    Ok(())
}
pub fn may_exit(app: &tauri::AppHandle) -> bool {
    app.state::<DesktopState>().inner.lock().unwrap().child.is_none()
}
#[derive(Default)]
struct CloseRequest { sequence: u64, pending: Option<u64> }
impl CloseRequest {
    fn begin(&mut self) -> Option<u64> {
        if self.pending.is_some() { return None; }
        self.sequence += 1;
        self.pending = Some(self.sequence);
        self.pending
    }
    fn acknowledge(&mut self, id: u64) {
        if self.pending == Some(id) { self.pending = None; }
    }
}
#[tauri::command]
pub fn desktop_close_ack(app: tauri::AppHandle, request_id: u64) {
    app.state::<DesktopState>().inner.lock().unwrap().close_request.acknowledge(request_id);
}
fn native_close_confirmation(app: &tauri::AppHandle) -> bool {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2, IDYES};
        let owner = app.get_webview_window("main").and_then(|w| w.hwnd().ok())
            .map(|h| h.0 as _).unwrap_or(std::ptr::null_mut());
        let title: Vec<u16> = "복지F5 · 종료 확인\0".encode_utf16().collect();
        let message: Vec<u16> = "화면이 종료 요청에 응답하지 않습니다.\n저장하지 않은 화면 입력은 잃을 수 있습니다. 종료할까요?\n\n진행 중인 저장 작업은 완료를 기다리며, 강제로 종료하지 않습니다.\0".encode_utf16().collect();
        MessageBoxW(owner, message.as_ptr(), title.as_ptr(), MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES
    }
    #[cfg(not(windows))]
    { let _ = app; false }
}
pub fn request_close(app: &tauri::AppHandle) {
    let id = {
        let state = app.state::<DesktopState>();
        let mut session = state.inner.lock().unwrap();
        match session.close_request.begin() { Some(id) => id, None => return }
    };
    let _ = app.emit("desktop-close-requested", id);
    let app = app.clone();
    // Independent of WebView/JS. Healthy UI acknowledges before checking
    // drafts or busy work, retaining its existing cancel/confirmation flow.
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(2));
        if app.state::<DesktopState>().inner.lock().unwrap().close_request.pending != Some(id) { return; }
        if native_close_confirmation(&app) {
            // Backend startup can still be in progress. Only our authenticated
            // shutdown endpoint is used; no child kill or job deadline.
            let mut accepted = false;
            for _ in 0..15 {
                if tauri::async_runtime::block_on(desktop_shutdown(app.clone())).is_ok() { accepted = true; break; }
                std::thread::sleep(Duration::from_secs(2));
            }
            if !accepted {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.set_title("복지F5 · 종료 연결 확인 실패 — 닫기 버튼으로 다시 시도해주세요");
                }
            }
        }
        app.state::<DesktopState>().inner.lock().unwrap().close_request.acknowledge(id);
    });
}
fn credentials(app: &tauri::AppHandle) -> Result<(String, String), String> {
    let state = app.state::<DesktopState>();
    let session = state.inner.lock().unwrap();
    if let Some(error) = &session.error { return Err(error.clone()); }
    if session.child.is_none() { return Err("내장 프로그램을 준비하고 있습니다.".into()); }
    Ok((session.token.clone(), session.instance.clone()))
}
#[derive(Debug)]
struct StatusError { message: String, transient: bool }
impl StatusError {
    fn fatal(message: impl Into<String>) -> Self { Self { message: message.into(), transient: false } }
    fn transport(error: reqwest::Error) -> Self {
        Self { message: "내장 프로그램의 응답을 기다리고 있습니다.".into(),
               transient: error.is_timeout() || error.is_connect() }
    }
}
async fn fetch_owned_status(client: &reqwest::Client, base: &str, token: &str, instance: &str) -> Result<Value, StatusError> {
    let response = client.get(format!("{base}/api/desktop/status")).header("X-Welfare-Session", token)
        .send().await.map_err(StatusError::transport)?;
    if !response.status().is_success() { return Err(StatusError::fatal("이번 실행의 백엔드 연결을 확인하지 못했습니다. 다른 서버에는 연결하지 않습니다.")); }
    let value: Value = response.json().await.map_err(|_| StatusError::fatal("백엔드 응답을 확인하지 못했습니다."))?;
    if value["instance"].as_str() != Some(instance) {
        return Err(StatusError::fatal("다른 실행의 서버가 응답했습니다. 안전을 위해 연결하지 않습니다."));
    }
    if !value["busy"].is_boolean() || !value["shutdown_requested"].is_boolean() {
        return Err(StatusError::fatal("백엔드 작업 상태를 확인하지 못했습니다."));
    }
    Ok(value)
}
async fn owned_status(app: &tauri::AppHandle) -> Result<Value, StatusError> {
    let (token, instance) = credentials(app).map_err(StatusError::fatal)?;
    let client = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(2)).build().map_err(|_| StatusError::fatal("연결 준비 실패"))?;
    fetch_owned_status(&client, BASE, &token, &instance).await
}
#[derive(Serialize)]
pub struct Connection { pub base_url: String, pub token: String }
#[tauri::command]
pub async fn desktop_connection(app: tauri::AppHandle) -> Result<Connection, String> {
    owned_status(&app).await.map_err(|error| error.message)?;
    let (token, _) = credentials(&app)?;
    Ok(Connection { base_url: format!("{BASE}/api"), token })
}
#[tauri::command]
pub async fn desktop_status(app: tauri::AppHandle) -> Value {
    if may_exit(&app) {
        let state = app.state::<DesktopState>();
        let session = state.inner.lock().unwrap();
        return json!({"phase": "error", "can_exit": true, "message": session.error});
    }
    match owned_status(&app).await {
        Ok(value) => {
            let confirmed = value["shutdown_requested"].as_bool() == Some(true);
            if confirmed { app.state::<DesktopState>().inner.lock().unwrap().shutdown_confirmed = true; }
            json!({"phase": "ready", "can_exit": false, "busy": value["busy"], "shutdown_requested": confirmed,
                "shutdown_stage": value["shutdown_stage"], "job": value["job"]})
        },
        Err(error) => {
            let confirmed = app.state::<DesktopState>().inner.lock().unwrap().shutdown_confirmed;
            json!({"phase": if confirmed { "stopping" } else { "starting" }, "can_exit": false,
                "shutdown_requested": confirmed, "shutdown_stage": if confirmed { "백엔드 종료 확인 중" } else { "" },
                "message": error.message, "transient": error.transient && !confirmed})
        },
    }
}
async fn send_shutdown(client: &reqwest::Client, base: &str, token: &str) -> Result<(), String> {
    let response = client.post(format!("{base}/api/desktop/shutdown")).header("X-Welfare-Session", token)
        .send().await.map_err(|_| "종료 요청의 전달 여부를 확인하지 못했습니다. ‘종료 다시 확인’을 눌러주세요. 강제 종료하지 않았습니다.")?;
    if !response.status().is_success() { return Err("종료 요청이 거부되었습니다. ‘종료 다시 확인’을 눌러주세요.".into()); }
    let value: Value = response.json().await.map_err(|_| "종료 응답을 읽지 못했습니다. ‘종료 다시 확인’을 눌러주세요.")?;
    if value["accepted"].as_bool() != Some(true) { return Err("종료 요청이 접수됐는지 확인하지 못했습니다. 다시 확인해주세요.".into()); }
    Ok(())
}
#[tauri::command]
pub async fn desktop_shutdown(app: tauri::AppHandle) -> Result<(), String> {
    if may_exit(&app) { app.exit(0); return Ok(()); }
    let status = owned_status(&app).await.map_err(|error| error.message)?;
    let (token, _) = credentials(&app)?;
    let client = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(3)).build().map_err(|_| "연결 준비 실패")?;
    // Exit intent is distinct from server acceptance. Keep intent before sending
    // so a very fast child exit cannot race the response; never report acceptance
    // until either the reply or an authenticated status confirms it.
    app.state::<DesktopState>().inner.lock().unwrap().shutdown_requested = true;
    if status["shutdown_requested"].as_bool() != Some(true) {
        if let Err(error) = send_shutdown(&client, BASE, &token).await {
            if may_exit(&app) { app.exit(0); return Ok(()); }
            // A timed-out response does not mean the server rejected the request.
            if !matches!(owned_status(&app).await, Ok(ref state) if state["shutdown_requested"].as_bool() == Some(true)) {
                return Err(error);
            }
        }
    }
    app.state::<DesktopState>().inner.lock().unwrap().shutdown_confirmed = true;
    if may_exit(&app) { app.exit(0); }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn close_ack_is_request_scoped_and_duplicates_are_coalesced() {
        let mut gate = CloseRequest::default();
        let first = gate.begin().unwrap();
        assert!(gate.begin().is_none());
        gate.acknowledge(first + 1);
        assert_eq!(gate.pending, Some(first));
        gate.acknowledge(first);
        let second = gate.begin().unwrap();
        gate.acknowledge(first);
        assert_eq!(gate.pending, Some(second));
        gate.acknowledge(second);
        assert_eq!(gate.pending, None);
    }
    fn reply_server(replies: Vec<(&'static str, &'static str)>) -> (String, std::thread::JoinHandle<()>) {
        use std::io::{Read, Write};
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            for (status, body) in replies {
                let (mut stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                let mut buffer = [0; 4096];
                stream.read(&mut buffer).unwrap();
                write!(stream, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            }
        });
        (base, worker)
    }
    #[test]
    fn shutdown_failure_can_be_retried() {
        let (base, worker) = reply_server(vec![("503 Unavailable", "{}"), ("200 OK", r#"{"accepted":true}"#)]);
        tauri::async_runtime::block_on(async {
            let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(3)).build().unwrap();
            assert!(send_shutdown(&client, &base, "test-only").await.is_err());
            assert!(send_shutdown(&client, &base, "test-only").await.is_ok());
        });
        worker.join().unwrap();
    }
    #[test]
    fn status_rejects_untrusted_and_malformed_replies_without_grace() {
        let (base, worker) = reply_server(vec![
            ("403 Forbidden", "{}"), ("200 OK", "not-json"),
            ("200 OK", r#"{"instance":"other","busy":false,"shutdown_requested":false}"#),
            ("200 OK", r#"{"instance":"test"}"#),
            ("200 OK", r#"{"instance":"test","busy":true,"shutdown_requested":false}"#),
        ]);
        tauri::async_runtime::block_on(async {
            let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(2)).build().unwrap();
            for _ in 0..4 {
                let error = fetch_owned_status(&client, &base, "test-only", "test").await.unwrap_err();
                assert!(!error.transient);
            }
            assert_eq!(fetch_owned_status(&client, &base, "test-only", "test").await.unwrap()["busy"], true);
        });
        worker.join().unwrap();
    }
    #[test]
    fn status_transport_timeout_is_explicitly_transient() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            let (_stream, _) = listener.accept().unwrap();
            std::thread::sleep(Duration::from_millis(250));
        });
        tauri::async_runtime::block_on(async {
            let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_millis(100)).build().unwrap();
            assert!(fetch_owned_status(&client, &base, "test-only", "test").await.unwrap_err().transient);
        });
        worker.join().unwrap();
    }
    #[test]
    fn shutdown_does_not_accept_missing_or_invalid_acknowledgement() {
        let (base, worker) = reply_server(vec![("200 OK", "{}"), ("200 OK", "not-json"), ("200 OK", r#"{"accepted":false}"#)]);
        tauri::async_runtime::block_on(async {
            let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(3)).build().unwrap();
            for _ in 0..3 { assert!(send_shutdown(&client, &base, "test-only").await.is_err()); }
        });
        worker.join().unwrap();
    }
    #[test]
    #[cfg(windows)]
    fn duplicate_instance_is_rejected_and_handle_is_released() {
        let name = format!("Local\\SearchMaster.WelfareF5.Test.{}", uuid::Uuid::new_v4());
        let first = acquire_named_instance(&name).expect("first instance");
        assert!(acquire_named_instance(&name).is_err());
        drop(first);
        assert!(acquire_named_instance(&name).is_ok());
    }
}
