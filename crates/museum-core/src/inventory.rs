//! The serial ports and network interfaces a gateway has. NixOS generates this; `museumd` only reads
//! it, so it can't assign a device the host doesn't declare.

use std::collections::BTreeSet;
use std::fmt;

use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const INVENTORY_VERSION: u32 = 1;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Inventory {
    pub version: u32,
    pub ports: Vec<SerialPort>,
    pub interfaces: Vec<NetworkInterface>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetworkInterface {
    pub name: String,
    pub zone: Zone,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Zone {
    Management,
    Museum,
    Vpn,
    Disabled,
}

impl fmt::Display for Zone {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Zone::Management => "management",
            Zone::Museum => "museum",
            Zone::Vpn => "vpn",
            Zone::Disabled => "disabled",
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SerialPort {
    pub name: String,
    pub device: String,
    pub kind: PortKind,
}

/// A virtual port doesn't prove anything about real UART behavior.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum PortKind {
    Uart,
    Virtual,
}

#[derive(Debug, Error)]
pub enum InventoryError {
    #[error("{0}")]
    Malformed(#[from] serde_json::Error),
    #[error("unsupported inventory version {0}")]
    Version(u32),
    #[error("invalid port name {0:?}")]
    Name(String),
    #[error("{0} appears more than once")]
    Duplicate(String),
}

impl Inventory {
    pub fn parse(text: &str) -> Result<Self, InventoryError> {
        let inventory: Self = serde_json::from_str(text)?;
        if inventory.version != INVENTORY_VERSION {
            return Err(InventoryError::Version(inventory.version));
        }
        // Two names for one device would break exclusive ownership.
        let mut seen = BTreeSet::new();
        for port in &inventory.ports {
            if !valid_name(&port.name) {
                return Err(InventoryError::Name(port.name.clone()));
            }
            for value in [&port.name, &port.device] {
                if !seen.insert(value) {
                    return Err(InventoryError::Duplicate(value.clone()));
                }
            }
        }
        Ok(inventory)
    }

    pub fn port(&self, name: &str) -> Option<&SerialPort> {
        self.ports.iter().find(|port| port.name == name)
    }
}

fn valid_name(name: &str) -> bool {
    name.starts_with(|c: char| c.is_ascii_lowercase())
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(ports: &str) -> Result<Inventory, InventoryError> {
        Inventory::parse(&format!(
            r#"{{"version":1,"ports":[{ports}],"interfaces":[]}}"#
        ))
    }

    const COM1: &str = r#"{"name":"com1","device":"/dev/ttyS0","kind":"uart"}"#;

    #[test]
    fn accepts_distinct_ports() {
        let second = r#"{"name":"com2","device":"/dev/ttyS1","kind":"virtual"}"#;
        let inventory = parse(&format!("{COM1},{second}")).unwrap();
        assert_eq!(inventory.port("com2").unwrap().kind, PortKind::Virtual);
        assert!(inventory.port("com3").is_none());
    }

    #[test]
    fn two_names_cannot_share_a_device() {
        let alias = r#"{"name":"com2","device":"/dev/ttyS0","kind":"uart"}"#;
        assert!(matches!(
            parse(&format!("{COM1},{alias}")),
            Err(InventoryError::Duplicate(_))
        ));
    }

    #[test]
    fn rejects_duplicate_names_bad_names_and_other_versions() {
        let reused = r#"{"name":"com1","device":"/dev/ttyS1","kind":"uart"}"#;
        assert!(matches!(
            parse(&format!("{COM1},{reused}")),
            Err(InventoryError::Duplicate(_))
        ));
        let bad = r#"{"name":"../com1","device":"/dev/ttyS1","kind":"uart"}"#;
        assert!(matches!(parse(bad), Err(InventoryError::Name(_))));
        assert!(matches!(
            Inventory::parse(r#"{"version":2,"ports":[],"interfaces":[]}"#),
            Err(InventoryError::Version(2))
        ));
    }
}
