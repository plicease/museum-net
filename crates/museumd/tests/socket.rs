//! The real daemon binary, driven over its real socket.

use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::thread::sleep;
use std::time::Duration;

use museum_core::client::{ClientError, call};
use museum_core::config::SerialChange;
use museum_core::protocol::{ErrorCode, Reply, Request, Response};
use nix::unistd::{Group, Uid, getgid};

const INVENTORY: &str = r#"{"version":1,"ports":[
    {"name":"com1","device":"/dev/a","kind":"virtual"},
    {"name":"com2","device":"/dev/b","kind":"virtual"}]}"#;

struct Daemon {
    child: Child,
    dir: PathBuf,
}

fn scratch() -> PathBuf {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!(
        "socket-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir_all(dir.join("state")).unwrap();
    fs::write(dir.join("inventory.json"), INVENTORY).unwrap();
    dir
}

fn spawn(dir: &Path, admin_group: &str) -> Child {
    Command::new(env!("CARGO_BIN_EXE_museumd"))
        .arg("--socket")
        .arg(dir.join("sock"))
        .arg("--state-dir")
        .arg(dir.join("state"))
        .arg("--inventory")
        .arg(dir.join("inventory.json"))
        .arg("--admin-group")
        .arg(admin_group)
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}

impl Daemon {
    fn start(dir: PathBuf, admin_group: &str) -> Self {
        let child = spawn(&dir, admin_group);
        let daemon = Self { child, dir };
        for _ in 0..200 {
            if daemon.ask(Request::Status).is_ok() {
                return daemon;
            }
            sleep(Duration::from_millis(10));
        }
        panic!("museumd did not start");
    }

    fn socket(&self) -> PathBuf {
        self.dir.join("sock")
    }

    fn ask(&self, request: Request) -> Result<Response, ClientError> {
        call(&self.socket(), request)
    }

    fn kill(mut self) -> PathBuf {
        self.child.kill().unwrap();
        self.child.wait().unwrap();
        self.dir.clone()
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn my_group() -> String {
    Group::from_gid(getgid()).unwrap().unwrap().name
}

fn set_slip(port: &str) -> Request {
    Request::SerialSet {
        port: port.to_owned(),
        change: SerialChange {
            mode: Some("slip".to_owned()),
            ..Default::default()
        },
    }
}

fn mode(daemon: &Daemon, port: &str) -> String {
    match daemon.ask(Request::SerialList).unwrap().unwrap() {
        Reply::Ports { ports } => {
            let port = ports.into_iter().find(|p| p.name == port).unwrap();
            port.desired.mode
        }
        other => panic!("{other:?}"),
    }
}

fn raw(daemon: &Daemon, line: &[u8]) -> Response {
    let mut stream = UnixStream::connect(daemon.socket()).unwrap();
    stream.write_all(line).unwrap();
    stream.shutdown(std::net::Shutdown::Write).unwrap();
    let mut reply = String::new();
    stream.read_to_string(&mut reply).unwrap();
    serde_json::from_str(&reply).unwrap()
}

#[test]
fn a_change_survives_being_killed_and_replaces_the_stale_socket() {
    let group = my_group();
    let daemon = Daemon::start(scratch(), &group);
    daemon.ask(set_slip("com1")).unwrap().unwrap();

    let dir = daemon.kill();
    assert!(
        fs::metadata(dir.join("sock"))
            .unwrap()
            .file_type()
            .is_socket()
    );

    let daemon = Daemon::start(dir, &group);
    assert_eq!(mode(&daemon, "com1"), "slip");
    assert_eq!(mode(&daemon, "com2"), "disabled");
}

#[test]
fn a_damaged_current_revision_stops_startup_instead_of_undoing_a_change() {
    let group = my_group();
    let daemon = Daemon::start(scratch(), &group);
    daemon.ask(set_slip("com1")).unwrap().unwrap();
    let dir = daemon.kill();

    fs::write(dir.join("state/00000002.json"), "damaged").unwrap();
    assert!(!spawn(&dir, &group).wait().unwrap().success());

    // Moving it aside is the documented way back to the previous revision.
    fs::remove_file(dir.join("state/00000002.json")).unwrap();
    let daemon = Daemon::start(dir, &group);
    assert_eq!(mode(&daemon, "com1"), "disabled");
}

#[test]
fn outside_the_admin_group_a_change_is_denied() {
    // Root may do anything, and nobody else is in the root group.
    if Uid::current().is_root() {
        return;
    }
    let daemon = Daemon::start(scratch(), "root");
    let error = daemon.ask(set_slip("com1")).unwrap().unwrap_err();
    assert_eq!(error.code, ErrorCode::Denied);
    assert_eq!(mode(&daemon, "com1"), "disabled");
}

#[test]
fn bad_requests_get_an_answer_and_do_not_stop_the_daemon() {
    let daemon = Daemon::start(scratch(), &my_group());
    for line in [
        &b"not json\n"[..],
        br#"{"protocol":99,"request":{"op":"status"}}"#,
        br#"{"protocol":1,"request":{"op":"format-c"}}"#,
    ] {
        assert_eq!(raw(&daemon, line).unwrap_err().code, ErrorCode::BadRequest);
    }
    assert!(daemon.ask(Request::Status).unwrap().is_ok());
}

#[test]
fn an_oversized_request_is_cut_off_without_harm() {
    let daemon = Daemon::start(scratch(), &my_group());
    let mut stream = UnixStream::connect(daemon.socket()).unwrap();
    // The daemon stops reading at its limit and closes, so this may fail.
    let _ = stream.write_all(&vec![b'x'; 256 * 1024]);
    assert!(daemon.ask(Request::Status).unwrap().is_ok());
}

#[test]
fn a_silent_client_does_not_block_others() {
    let daemon = Daemon::start(scratch(), &my_group());
    let _idle = UnixStream::connect(daemon.socket()).unwrap();
    assert!(daemon.ask(Request::Status).unwrap().is_ok());
}

#[test]
fn it_refuses_to_start_without_its_admin_group() {
    let dir = scratch();
    let status = spawn(&dir, "no-such-group-here").wait().unwrap();
    assert!(!status.success());
}

#[test]
fn a_client_explains_a_missing_daemon() {
    let dir = scratch();
    let error = call(&dir.join("sock"), Request::Status).unwrap_err();
    assert!(matches!(error, ClientError::NotRunning { .. }));
}
