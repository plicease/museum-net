//! A blocking client for the management socket.

use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

use thiserror::Error;

use crate::protocol::{PROTOCOL_VERSION, Request, RequestEnvelope, Response};

/// Longer than any request takes, even while `museumd` waits for the disk.
const TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("can't connect to {path}: is museumd running?")]
    NotRunning { path: PathBuf },
    #[error("permission denied on {path}: this needs membership of the museum group")]
    NotAllowed { path: PathBuf },
    #[error("can't talk to museumd: {0}")]
    Io(#[from] io::Error),
    #[error("museumd sent a reply this client can't read: {0}")]
    Reply(#[from] serde_json::Error),
}

pub fn call(socket: &Path, request: Request) -> Result<Response, ClientError> {
    let mut stream = UnixStream::connect(socket).map_err(|error| {
        let path = socket.to_owned();
        match error.kind() {
            io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused => {
                ClientError::NotRunning { path }
            }
            io::ErrorKind::PermissionDenied => ClientError::NotAllowed { path },
            _ => ClientError::Io(error),
        }
    })?;
    stream.set_read_timeout(Some(TIMEOUT))?;
    stream.set_write_timeout(Some(TIMEOUT))?;

    let envelope = RequestEnvelope {
        protocol: PROTOCOL_VERSION,
        request,
    };
    let mut line = serde_json::to_vec(&envelope)?;
    line.push(b'\n');
    stream.write_all(&line)?;

    let mut reply = String::new();
    BufReader::new(stream).read_line(&mut reply)?;
    Ok(serde_json::from_str(&reply)?)
}
