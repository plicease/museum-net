//! The management API. A client connects to the socket, writes one JSON
//! `RequestEnvelope` line, and reads one JSON `Response` line. Every front end
//! uses these operations, so they all see the same state.

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::config::{AdapterDescriptor, Config, DISABLED, PortConfig, Problem, SerialChange};
use crate::inventory::PortKind;

pub const PROTOCOL_VERSION: u32 = 1;

/// The longest request line `museumd` reads.
pub const MAX_REQUEST_BYTES: u64 = 64 * 1024;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RequestEnvelope {
    pub protocol: u32,
    pub request: Request,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    Status,
    SerialList,
    SerialAdapters,
    SerialSet { port: String, change: SerialChange },
    SerialDisable { port: String },
    ConfigShow { revision: Option<u64> },
    ConfigValidate,
    ConfigHistory { limit: Option<usize> },
    ConfigRollback { revision: u64 },
}

impl Request {
    /// Whether this request needs mutation rights. There is deliberately no
    /// catch-all arm: a new request has to be classified here.
    pub fn mutates(&self) -> bool {
        match self {
            Request::SerialSet { .. }
            | Request::SerialDisable { .. }
            | Request::ConfigRollback { .. } => true,
            Request::Status
            | Request::SerialList
            | Request::SerialAdapters
            | Request::ConfigShow { .. }
            | Request::ConfigValidate
            | Request::ConfigHistory { .. } => false,
        }
    }
}

pub type Response = Result<Reply, ApiError>;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum Reply {
    Status(Status),
    Ports { ports: Vec<PortView> },
    Adapters { adapters: Vec<AdapterDescriptor> },
    Stored(Stored),
    Config(ConfigView),
    Validation { problems: Vec<Problem> },
    History { changes: Vec<Change> },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Status {
    pub version: String,
    pub schema: u32,
    pub current: Change,
    pub ports: Vec<PortView>,
    pub problems: Vec<Problem>,
}

/// A port as requested and as it actually is. These can differ, so a client
/// must never present `desired` as what the port is doing.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortView {
    pub name: String,
    pub device: String,
    pub kind: PortKind,
    /// The validated, durable configuration the operator requested.
    pub desired: PortConfig,
    /// What the service controller has applied. `None` means no service owns
    /// the port.
    pub active: Option<PortConfig>,
    pub state: RuntimeState,
    /// Why the port is not as desired, or what went wrong.
    pub message: Option<String>,
}

impl PortView {
    /// Whether the port is doing what was requested.
    pub fn activated(&self) -> bool {
        match &self.active {
            Some(active) => *active == self.desired && self.state == RuntimeState::Running,
            None => self.desired.mode == DISABLED,
        }
    }
}

/// The state of the service that owns a port.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RuntimeState {
    Stopped,
    Starting,
    Running,
    Stopping,
    Unhealthy,
    Failed,
}

impl fmt::Display for RuntimeState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            RuntimeState::Stopped => "stopped",
            RuntimeState::Starting => "starting",
            RuntimeState::Running => "running",
            RuntimeState::Stopping => "stopping",
            RuntimeState::Unhealthy => "unhealthy",
            RuntimeState::Failed => "failed",
        })
    }
}

/// The outcome of a change: the desired configuration is durable. This says
/// nothing about any service, which is what `pending` is for.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Stored {
    pub revision: u64,
    /// False if the request matched the desired configuration, so no revision
    /// was written.
    pub changed: bool,
    /// Ports that are not doing what the desired configuration asks.
    pub pending: Vec<PortView>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConfigView {
    pub revision: u64,
    pub config: Config,
}

/// One entry in the change log.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Change {
    pub revision: u64,
    pub time: String,
    pub actor: String,
    pub action: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApiError {
    pub code: ErrorCode,
    pub message: String,
    #[serde(default)]
    pub problems: Vec<Problem>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ErrorCode {
    BadRequest,
    Denied,
    Invalid,
    NotFound,
    Internal,
}

impl ApiError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            problems: Vec::new(),
        }
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)?;
        for problem in &self.problems {
            write!(f, "\n  {problem}")?;
        }
        Ok(())
    }
}

impl std::error::Error for ApiError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_survive_the_wire() {
        let request = RequestEnvelope {
            protocol: PROTOCOL_VERSION,
            request: Request::SerialSet {
                port: "com1".to_owned(),
                change: SerialChange {
                    mode: Some("slip".to_owned()),
                    ..Default::default()
                },
            },
        };
        let text = serde_json::to_string(&request).unwrap();
        assert!(!text.contains('\n'));
        assert_eq!(
            serde_json::from_str::<RequestEnvelope>(&text).unwrap(),
            request
        );
    }

    #[test]
    fn replies_and_errors_survive_the_wire() {
        for response in [
            Ok(Reply::Stored(Stored {
                revision: 2,
                changed: true,
                pending: Vec::new(),
            })),
            Err(ApiError::new(ErrorCode::Denied, "no")),
        ] {
            let text = serde_json::to_string(&response as &Response).unwrap();
            assert_eq!(serde_json::from_str::<Response>(&text).unwrap(), response);
        }
    }

    fn view(desired: PortConfig, active: Option<PortConfig>, state: RuntimeState) -> PortView {
        PortView {
            name: "com1".to_owned(),
            device: "/dev/a".to_owned(),
            kind: PortKind::Virtual,
            desired,
            active,
            state,
            message: None,
        }
    }

    #[test]
    fn a_stored_mode_is_not_an_activated_one() {
        let slip = PortConfig {
            mode: "slip".to_owned(),
            ..Default::default()
        };
        let ppp = PortConfig {
            mode: "ppp".to_owned(),
            ..Default::default()
        };
        let stopped = RuntimeState::Stopped;
        assert!(!view(slip.clone(), None, stopped).activated());
        assert!(view(PortConfig::default(), None, stopped).activated());

        let running = RuntimeState::Running;
        assert!(view(slip.clone(), Some(slip.clone()), running).activated());
        assert!(!view(slip.clone(), Some(ppp), running).activated());
        let failed = RuntimeState::Failed;
        assert!(!view(slip.clone(), Some(slip), failed).activated());
    }

    #[test]
    fn only_changes_need_mutation_rights() {
        assert!(Request::ConfigRollback { revision: 1 }.mutates());
        assert!(!Request::ConfigShow { revision: Some(1) }.mutates());
    }
}
