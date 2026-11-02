//! The operations behind every front end. Nothing here knows about sockets.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use museum_core::config::{
    AdapterDescriptor, Config, DISABLED, PortConfig, Problem, SCHEMA_VERSION, SerialChange,
};
use museum_core::inventory::Inventory;
use museum_core::protocol::{
    ApiError, ConfigView, ErrorCode, InterfaceView, PortView, Reply, Request, Response, Status,
    Stored,
};
use tracing::{error, info, warn};

use crate::adapters::{Observed, observe};
use crate::auth::Peer;
use crate::store::{Revision, Store, StoreError};

pub struct Service {
    store: Store,
    inventory: Inventory,
    adapters: Vec<AdapterDescriptor>,
    current: Revision,
    sys_class_net: PathBuf,
}

impl Service {
    pub fn new(
        mut store: Store,
        inventory: Inventory,
        adapters: Vec<AdapterDescriptor>,
    ) -> Result<Self, StoreError> {
        let current = match store.latest() {
            0 => store.append(
                now(),
                "museumd".to_owned(),
                "initial configuration".to_owned(),
                Config::default(),
            )?,
            latest => store.read(latest)?,
        };
        let service = Self {
            store,
            inventory,
            adapters,
            current,
            sys_class_net: PathBuf::from("/sys/class/net"),
        };
        for problem in service.problems() {
            warn!(%problem, "stored configuration has a problem");
        }
        Ok(service)
    }

    pub fn handle(&mut self, peer: &Peer, request: Request) -> Response {
        if request.mutates() && !peer.may_mutate {
            warn!(peer = peer.name, ?request, "denied");
            return Err(ApiError::new(
                ErrorCode::Denied,
                format!("{} may not change the configuration", peer.name),
            ));
        }
        match request {
            Request::Status => Ok(Reply::Status(self.status())),
            Request::SerialList => Ok(Reply::Ports {
                ports: self.ports(),
            }),
            Request::SerialAdapters => Ok(Reply::Adapters {
                adapters: self.adapters.clone(),
            }),
            Request::NetworkList => Ok(Reply::Interfaces {
                interfaces: self.interfaces(),
            }),
            Request::SerialSet { port, change } => self.serial_set(peer, &port, change),
            Request::SerialDisable { port } => self.serial_disable(peer, &port),
            Request::ConfigShow { revision } => self.config_show(revision),
            Request::ConfigValidate => Ok(Reply::Validation {
                problems: self.problems(),
            }),
            Request::ConfigHistory { limit } => Ok(Reply::History {
                changes: self.store.history(limit).map_err(internal)?,
            }),
            Request::ConfigRollback { revision } => self.rollback(peer, revision),
        }
    }

    fn problems(&self) -> Vec<Problem> {
        self.current
            .config
            .problems(&self.inventory, &self.adapters)
    }

    fn ports(&self) -> Vec<PortView> {
        self.inventory
            .ports
            .iter()
            .map(|port| {
                let desired = self.current.config.port(&port.name);
                let Observed {
                    active,
                    state,
                    message,
                } = observe(&desired);
                PortView {
                    name: port.name.clone(),
                    device: port.device.clone(),
                    kind: port.kind,
                    desired,
                    active,
                    state,
                    message,
                }
            })
            .collect()
    }

    fn interfaces(&self) -> Vec<InterfaceView> {
        self.inventory
            .interfaces
            .iter()
            .map(|interface| InterfaceView {
                name: interface.name.clone(),
                zone: interface.zone,
                state: operational_state(&self.sys_class_net, &interface.name),
            })
            .collect()
    }

    fn status(&self) -> Status {
        Status {
            version: env!("CARGO_PKG_VERSION").to_owned(),
            schema: SCHEMA_VERSION,
            current: self.current.change.clone(),
            ports: self.ports(),
            problems: self.problems(),
        }
    }

    fn require_port(&self, name: &str) -> Result<(), ApiError> {
        if self.inventory.port(name).is_some() {
            return Ok(());
        }
        let known: Vec<&str> = self
            .inventory
            .ports
            .iter()
            .map(|p| p.name.as_str())
            .collect();
        Err(ApiError::new(
            ErrorCode::NotFound,
            format!(
                "no serial port {name:?}: this gateway has {}",
                known.join(", ")
            ),
        ))
    }

    fn serial_set(&mut self, peer: &Peer, name: &str, change: SerialChange) -> Response {
        self.require_port(name)?;
        let port = self.current.config.port(name).with(change);
        let action = format!("set {name} to {port}");
        self.commit(peer, action, with_port(&self.current.config, name, port))
    }

    fn serial_disable(&mut self, peer: &Peer, name: &str) -> Response {
        // A port removed from the inventory can't be disabled, but its stale
        // entry has to be removable.
        if self.inventory.port(name).is_none() && self.current.config.serial.contains_key(name) {
            let mut config = self.current.config.clone();
            config.serial.remove(name);
            return self.commit(peer, format!("remove {name}"), config);
        }
        self.require_port(name)?;
        let port = PortConfig {
            mode: DISABLED.to_owned(),
            ..self.current.config.port(name)
        };
        let action = format!("disable {name}");
        self.commit(peer, action, with_port(&self.current.config, name, port))
    }

    fn config_show(&self, revision: Option<u64>) -> Response {
        let record = match revision {
            None => self.current.clone(),
            Some(revision) => self.existing(revision)?,
        };
        Ok(Reply::Config(ConfigView {
            revision: record.change.revision,
            config: record.config,
        }))
    }

    fn rollback(&mut self, peer: &Peer, revision: u64) -> Response {
        let target = self.existing(revision)?;
        self.commit(
            peer,
            format!("roll back to revision {revision}"),
            target.config,
        )
    }

    fn existing(&self, revision: u64) -> Result<Revision, ApiError> {
        if !self.store.contains(revision) {
            return Err(ApiError::new(
                ErrorCode::NotFound,
                format!("no revision {revision}"),
            ));
        }
        self.store.read(revision).map_err(internal)
    }

    /// Validate and persist a new desired configuration, which doesn't activate
    /// anything. In-memory state only changes after the revision is on disk.
    fn commit(&mut self, peer: &Peer, action: String, config: Config) -> Response {
        if config == self.current.config {
            return Ok(self.stored(false));
        }
        let problems = config.new_problems(&self.current.config, &self.inventory, &self.adapters);
        if !problems.is_empty() {
            warn!(peer = peer.name, action, ?problems, "refused");
            return Err(ApiError {
                code: ErrorCode::Invalid,
                message: "change refused; the configuration is unchanged".to_owned(),
                problems,
            });
        }
        let record = self
            .store
            .append(now(), peer.name.clone(), action, config)
            .map_err(internal)?;
        info!(
            peer = peer.name,
            revision = record.change.revision,
            action = record.change.action,
            "committed"
        );
        self.current = record;
        Ok(self.stored(true))
    }

    fn stored(&self, changed: bool) -> Reply {
        Reply::Stored(Stored {
            revision: self.current.change.revision,
            changed,
            pending: self
                .ports()
                .into_iter()
                .filter(|port| !port.activated())
                .collect(),
        })
    }
}

/// `config` with one port replaced. A port at its defaults isn't stored, so
/// two configurations that behave the same always compare equal.
fn with_port(config: &Config, name: &str, port: PortConfig) -> Config {
    let mut config = config.clone();
    if port == PortConfig::default() {
        config.serial.remove(name);
    } else {
        config.serial.insert(name.to_owned(), port);
    }
    config
}

fn operational_state(sys_class_net: &Path, interface: &str) -> String {
    fs::read_to_string(sys_class_net.join(interface).join("operstate"))
        .map_or_else(|_| "missing".to_owned(), |state| state.trim().to_owned())
}

fn now() -> String {
    humantime::format_rfc3339_seconds(SystemTime::now()).to_string()
}

fn internal(source: StoreError) -> ApiError {
    error!(%source, "store failure");
    ApiError::new(ErrorCode::Internal, source.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adapters;
    use crate::store::tests::scratch;
    use museum_core::inventory::Zone;
    use museum_core::protocol::RuntimeState;

    const INVENTORY: &str = r#"{"version":1,"ports":[
        {"name":"com1","device":"/dev/a","kind":"virtual"},
        {"name":"com2","device":"/dev/b","kind":"virtual"}],
        "interfaces":[
        {"name":"mgmt0","zone":"management"},
        {"name":"museum0","zone":"museum"}]}"#;

    fn admin() -> Peer {
        Peer {
            name: "alice".to_owned(),
            may_mutate: true,
        }
    }

    fn open_with(dir: &Path, inventory: &str) -> Service {
        Service::new(
            Store::open(dir).unwrap(),
            Inventory::parse(inventory).unwrap(),
            adapters::catalog(),
        )
        .unwrap()
    }

    fn open(dir: &Path) -> Service {
        open_with(dir, INVENTORY)
    }

    fn set(port: &str, mode: &str, baud: u32) -> Request {
        Request::SerialSet {
            port: port.to_owned(),
            change: SerialChange {
                mode: Some(mode.to_owned()),
                baud: Some(baud),
                ..Default::default()
            },
        }
    }

    fn applied(response: Response) -> Stored {
        match response.unwrap() {
            Reply::Stored(stored) => stored,
            other => panic!("{other:?}"),
        }
    }

    fn revision(service: &Service) -> u64 {
        service.current.change.revision
    }

    #[test]
    fn a_new_store_starts_with_every_port_disabled() {
        let service = open(&scratch());
        assert_eq!(revision(&service), 1);
        assert!(
            service
                .ports()
                .iter()
                .all(|port| port.desired.mode == DISABLED)
        );
    }

    #[test]
    fn changes_survive_a_restart_and_name_their_actor() {
        let dir = scratch();
        {
            let mut service = open(&dir);
            applied(service.handle(&admin(), set("com1", "slip", 38400)));
        }
        let mut service = open(&dir);
        assert_eq!(service.current.config.port("com1").mode, "slip");
        let Ok(Reply::History { changes }) =
            service.handle(&admin(), Request::ConfigHistory { limit: Some(1) })
        else {
            panic!()
        };
        assert_eq!(changes[0].actor, "alice");
        assert_eq!(changes[0].action, "set com1 to slip 38400 8N1 flow none");
    }

    #[test]
    fn interfaces_report_their_zone_and_the_kernel_state() {
        let mut service = open(&scratch());
        let sys = scratch();
        fs::create_dir(sys.join("mgmt0")).unwrap();
        fs::write(sys.join("mgmt0/operstate"), "up\n").unwrap();
        service.sys_class_net = sys;

        let Ok(Reply::Interfaces { interfaces }) = service.handle(&admin(), Request::NetworkList)
        else {
            panic!()
        };
        let states: Vec<_> = interfaces
            .iter()
            .map(|i| (i.name.as_str(), i.zone, i.state.as_str()))
            .collect();
        assert_eq!(
            states,
            [
                ("mgmt0", Zone::Management, "up"),
                ("museum0", Zone::Museum, "missing")
            ]
        );
    }

    #[test]
    fn a_stored_change_reports_the_port_as_not_active() {
        let mut service = open(&scratch());
        let stored = applied(service.handle(&admin(), set("com1", "slip", 38400)));
        assert!(stored.changed);
        assert_eq!(stored.pending.len(), 1);

        let port = &stored.pending[0];
        assert_eq!(port.name, "com1");
        assert_eq!(port.desired.mode, "slip");
        assert_eq!(port.active, None);
        assert_eq!(port.state, RuntimeState::Stopped);
        assert!(port.message.as_deref().unwrap().contains("not active"));

        let disabled = applied(service.handle(
            &admin(),
            Request::SerialDisable {
                port: "com1".to_owned(),
            },
        ));
        assert!(disabled.pending.is_empty());
    }

    #[test]
    fn changing_one_port_leaves_the_other_alone() {
        let mut service = open(&scratch());
        applied(service.handle(&admin(), set("com2", "sprocket", 57600)));
        let before = service.current.config.port("com2");
        applied(service.handle(&admin(), set("com1", "slip", 38400)));
        assert_eq!(service.current.config.port("com2"), before);
    }

    #[test]
    fn a_refused_change_writes_nothing() {
        let mut service = open(&scratch());
        for request in [set("com1", "gopher", 9600), set("com1", "slip", 31337)] {
            let error = service.handle(&admin(), request).unwrap_err();
            assert_eq!(error.code, ErrorCode::Invalid);
            assert_eq!(error.problems.len(), 1);
        }
        assert_eq!(revision(&service), 1);
        let error = service
            .handle(&admin(), set("com9", "slip", 9600))
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::NotFound);
    }

    #[test]
    fn a_failed_commit_changes_nothing() {
        let dir = scratch();
        let mut service = open(&dir);
        // A directory in the way of the rename fails even when run as root.
        fs::create_dir(dir.join("00000002.json")).unwrap();
        let error = service
            .handle(&admin(), set("com1", "slip", 9600))
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Internal);
        assert_eq!(service.current.config, Config::default());
    }

    #[test]
    fn an_unprivileged_peer_can_read_but_not_change() {
        let mut service = open(&scratch());
        let viewer = Peer {
            name: "bob".to_owned(),
            may_mutate: false,
        };
        assert!(service.handle(&viewer, Request::Status).is_ok());
        for request in [
            set("com1", "slip", 9600),
            Request::SerialDisable {
                port: "com1".to_owned(),
            },
            Request::ConfigRollback { revision: 1 },
        ] {
            let error = service.handle(&viewer, request).unwrap_err();
            assert_eq!(error.code, ErrorCode::Denied);
        }
        assert_eq!(revision(&service), 1);
    }

    #[test]
    fn rollback_appends_and_can_itself_be_rolled_back() {
        let mut service = open(&scratch());
        applied(service.handle(&admin(), set("com1", "slip", 38400)));
        applied(service.handle(&admin(), set("com1", "ppp", 57600)));

        let result = applied(service.handle(&admin(), Request::ConfigRollback { revision: 2 }));
        assert_eq!(result.revision, 4);
        assert_eq!(service.current.config.port("com1").mode, "slip");

        applied(service.handle(&admin(), Request::ConfigRollback { revision: 3 }));
        assert_eq!(service.current.config.port("com1").mode, "ppp");
    }

    #[test]
    fn rollback_to_a_missing_revision_is_not_found() {
        let mut service = open(&scratch());
        let error = service
            .handle(&admin(), Request::ConfigRollback { revision: 9 })
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::NotFound);
    }

    #[test]
    fn a_change_that_changes_nothing_writes_no_revision() {
        let mut service = open(&scratch());
        let result = service.handle(
            &admin(),
            Request::SerialDisable {
                port: "com1".to_owned(),
            },
        );
        assert!(!applied(result).changed);

        applied(service.handle(&admin(), set("com1", "slip", 38400)));
        let again = applied(service.handle(&admin(), set("com1", "slip", 38400)));
        assert!(!again.changed);
        assert_eq!(again.revision, 2);
    }

    #[test]
    fn disabling_keeps_line_settings_for_later() {
        let mut service = open(&scratch());
        applied(service.handle(&admin(), set("com1", "slip", 38400)));
        applied(service.handle(
            &admin(),
            Request::SerialDisable {
                port: "com1".to_owned(),
            },
        ));
        let port = service.current.config.port("com1");
        assert_eq!((port.mode.as_str(), port.baud), (DISABLED, 38400));
    }

    #[test]
    fn a_port_gone_from_the_inventory_is_reported_and_removable() {
        let dir = scratch();
        {
            let mut service = open(&dir);
            applied(service.handle(&admin(), set("com2", "slip", 9600)));
        }
        let mut service = open_with(
            &dir,
            r#"{"version":1,"ports":[{"name":"com1","device":"/dev/a","kind":"virtual"}],"interfaces":[]}"#,
        );
        let Ok(Reply::Validation { problems }) = service.handle(&admin(), Request::ConfigValidate)
        else {
            panic!()
        };
        assert_eq!(problems[0].subject, "serial.com2");

        // The stale entry doesn't block an unrelated change, and can't be
        // reintroduced by rolling back to it afterwards.
        applied(service.handle(&admin(), set("com1", "ppp", 9600)));
        let removed = service.handle(
            &admin(),
            Request::SerialDisable {
                port: "com2".to_owned(),
            },
        );
        assert!(applied(removed).changed);
        assert!(service.problems().is_empty());
        let error = service
            .handle(&admin(), Request::ConfigRollback { revision: 2 })
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Invalid);
    }
}
