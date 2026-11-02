//! The Unix socket front end: one JSON request and one JSON reply per
//! connection.

use std::io;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use museum_core::protocol::{
    ApiError, ErrorCode, MAX_REQUEST_BYTES, PROTOCOL_VERSION, RequestEnvelope, Response,
};
use nix::unistd::Gid;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::task::spawn_blocking;
use tokio::time::{sleep, timeout};
use tracing::{debug, warn};

use crate::auth::identify;
use crate::service::Service;

/// How long a client gets to send its request.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

pub async fn serve(listener: UnixListener, service: Service, admin_group: Gid) -> io::Result<()> {
    let service = Arc::new(Mutex::new(service));
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                let service = Arc::clone(&service);
                tokio::spawn(async move {
                    if let Err(error) = connection(stream, service, admin_group).await {
                        debug!(%error, "connection ended early");
                    }
                });
            }
            Err(error) => {
                // Out of descriptors, most likely. Connections finish and free some.
                warn!(%error, "accept failed");
                sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

async fn connection(
    mut stream: UnixStream,
    service: Arc<Mutex<Service>>,
    admin_group: Gid,
) -> io::Result<()> {
    let uid = stream.peer_cred()?.uid();
    let (reader, mut writer) = stream.split();

    let mut line = Vec::new();
    let mut reader = BufReader::new(reader.take(MAX_REQUEST_BYTES));
    let read = timeout(REQUEST_TIMEOUT, reader.read_until(b'\n', &mut line)).await;
    let read = read.map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "no request"))?;
    read?;

    let response: Response = match decode(&line) {
        Ok(request) => {
            // User lookups and fsync block, so they stay off the async workers.
            spawn_blocking(move || {
                let peer = identify(uid, admin_group);
                service
                    .lock()
                    .expect("no poison with panic=abort")
                    .handle(&peer, request)
            })
            .await?
        }
        Err(error) => Err(error),
    };

    let mut reply = serde_json::to_vec(&response).map_err(io::Error::other)?;
    reply.push(b'\n');
    writer.write_all(&reply).await
}

fn decode(line: &[u8]) -> Result<museum_core::protocol::Request, ApiError> {
    let bad = |message: String| ApiError::new(ErrorCode::BadRequest, message);
    let envelope: RequestEnvelope =
        serde_json::from_slice(line).map_err(|error| bad(format!("malformed request: {error}")))?;
    if envelope.protocol != PROTOCOL_VERSION {
        return Err(bad(format!(
            "this museumd speaks protocol {PROTOCOL_VERSION}, not {}",
            envelope.protocol
        )));
    }
    Ok(envelope.request)
}
