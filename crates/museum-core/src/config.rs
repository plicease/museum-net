//! The operational configuration owned by `museumd`.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::inventory::Inventory;

/// Bump this when a stored revision can't be read by the previous version, and
/// add a migration in `museumd`.
pub const SCHEMA_VERSION: u32 = 1;

/// The mode of a port no service owns. Every other mode names an adapter.
pub const DISABLED: &str = "disabled";

pub const BAUD_RATES: [u32; 10] = [
    300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
];

const MAX_LABEL_CHARS: usize = 64;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Ports missing from this map are disabled with default line settings.
    pub serial: BTreeMap<String, PortConfig>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortConfig {
    pub mode: String,
    pub baud: u32,
    /// Data bits, parity, and stop bits, such as `8N1`.
    pub framing: String,
    pub flow: Flow,
    /// What is connected to this port.
    pub label: Option<String>,
}

impl Default for PortConfig {
    fn default() -> Self {
        Self {
            mode: DISABLED.to_owned(),
            baud: 9600,
            framing: "8N1".to_owned(),
            flow: Flow::None,
            label: None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Flow {
    None,
    RtsCts,
    XonXoff,
}

impl fmt::Display for Flow {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Flow::None => "none",
            Flow::RtsCts => "rts-cts",
            Flow::XonXoff => "xon-xoff",
        })
    }
}

impl FromStr for Flow {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, String> {
        match value {
            "none" => Ok(Flow::None),
            "rts-cts" => Ok(Flow::RtsCts),
            "xon-xoff" => Ok(Flow::XonXoff),
            _ => Err(format!(
                "unknown flow control {value:?}: expected none, rts-cts, or xon-xoff"
            )),
        }
    }
}

/// Fields to change on a port. `None` keeps the current value.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SerialChange {
    pub mode: Option<String>,
    pub baud: Option<u32>,
    pub framing: Option<String>,
    pub flow: Option<Flow>,
    /// An empty label clears the current one.
    pub label: Option<String>,
}

impl PortConfig {
    pub fn with(&self, change: SerialChange) -> Self {
        Self {
            mode: change.mode.unwrap_or_else(|| self.mode.clone()),
            baud: change.baud.unwrap_or(self.baud),
            framing: change.framing.unwrap_or_else(|| self.framing.clone()),
            flow: change.flow.unwrap_or(self.flow),
            label: match change.label {
                Some(label) if label.is_empty() => None,
                Some(label) => Some(label),
                None => self.label.clone(),
            },
        }
    }
}

impl fmt::Display for PortConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} {} {} flow {}",
            self.mode, self.baud, self.framing, self.flow
        )
    }
}

/// A protocol a port can be assigned to. Clients learn these from the daemon
/// instead of carrying their own list.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdapterDescriptor {
    pub name: String,
    pub description: String,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Problem {
    /// What the problem is about, such as `serial.com1`.
    pub subject: String,
    pub message: String,
}

impl fmt::Display for Problem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.subject, self.message)
    }
}

impl Config {
    /// This port's settings, or the defaults if it has none stored.
    pub fn port(&self, name: &str) -> PortConfig {
        self.serial.get(name).cloned().unwrap_or_default()
    }

    /// Everything wrong with this configuration on this gateway.
    pub fn problems(&self, inventory: &Inventory, adapters: &[AdapterDescriptor]) -> Vec<Problem> {
        let mut problems = Vec::new();
        for (name, port) in &self.serial {
            let mut report = |message: String| {
                problems.push(Problem {
                    subject: format!("serial.{name}"),
                    message,
                });
            };
            if inventory.port(name).is_none() {
                report("this gateway has no such port".to_owned());
            }
            if port.mode != DISABLED && !adapters.iter().any(|a| a.name == port.mode) {
                let known: Vec<&str> = adapters.iter().map(|a| a.name.as_str()).collect();
                report(format!(
                    "unknown mode {:?}: available modes are {DISABLED}, {}",
                    port.mode,
                    known.join(", ")
                ));
            }
            if !BAUD_RATES.contains(&port.baud) {
                report(format!("unsupported baud rate {}", port.baud));
            }
            if !valid_framing(&port.framing) {
                report(format!(
                    "invalid framing {:?}: expected data bits 5-8, parity N, E, or O, and stop bits 1 or 2, such as 8N1",
                    port.framing
                ));
            }
            if let Some(message) = port.label.as_deref().and_then(label_problem) {
                report(message);
            }
        }
        problems
    }

    /// Problems in this configuration that `previous` doesn't have. A change
    /// can't make things worse, but an existing problem, such as a stored port
    /// that was since removed from the inventory, doesn't block fixing others.
    pub fn new_problems(
        &self,
        previous: &Config,
        inventory: &Inventory,
        adapters: &[AdapterDescriptor],
    ) -> Vec<Problem> {
        let known: BTreeSet<Problem> = previous.problems(inventory, adapters).into_iter().collect();
        let mut problems = self.problems(inventory, adapters);
        problems.retain(|problem| !known.contains(problem));
        problems
    }
}

fn valid_framing(framing: &str) -> bool {
    matches!(
        framing.as_bytes(),
        [b'5'..=b'8', b'N' | b'E' | b'O', b'1' | b'2']
    )
}

fn label_problem(label: &str) -> Option<String> {
    if label.chars().count() > MAX_LABEL_CHARS {
        Some(format!("label is longer than {MAX_LABEL_CHARS} characters"))
    } else if label.chars().any(char::is_control) {
        Some("label contains control characters".to_owned())
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn inventory() -> Inventory {
        Inventory::parse(
            r#"{"version":1,"ports":[
                {"name":"com1","device":"/dev/ttyS0","kind":"uart"},
                {"name":"com2","device":"/dev/ttyS1","kind":"uart"}]}"#,
        )
        .unwrap()
    }

    fn adapters() -> Vec<AdapterDescriptor> {
        vec![AdapterDescriptor {
            name: "slip".to_owned(),
            description: "SLIP".to_owned(),
        }]
    }

    fn config(port: &str, settings: PortConfig) -> Config {
        Config {
            serial: BTreeMap::from([(port.to_owned(), settings)]),
        }
    }

    fn problems(settings: PortConfig) -> Vec<Problem> {
        config("com1", settings).problems(&inventory(), &adapters())
    }

    #[test]
    fn empty_and_default_configurations_are_valid() {
        assert!(
            Config::default()
                .problems(&inventory(), &adapters())
                .is_empty()
        );
        assert!(problems(PortConfig::default()).is_empty());
    }

    #[test]
    fn modes_are_the_adapters_the_daemon_lists() {
        let slip = PortConfig {
            mode: "slip".to_owned(),
            ..Default::default()
        };
        assert!(problems(slip.clone()).is_empty());

        let unknown = problems(PortConfig {
            mode: "gopher".to_owned(),
            ..slip
        });
        assert_eq!(unknown.len(), 1);
        assert!(unknown[0].message.contains("disabled, slip"), "{unknown:?}");
    }

    #[test]
    fn line_settings_are_checked_even_while_disabled() {
        for settings in [
            PortConfig {
                baud: 31337,
                ..Default::default()
            },
            PortConfig {
                framing: "9N1".to_owned(),
                ..Default::default()
            },
            PortConfig {
                framing: "8N".to_owned(),
                ..Default::default()
            },
        ] {
            assert_eq!(problems(settings).len(), 1);
        }
    }

    #[test]
    fn labels_cannot_carry_control_characters_or_grow_without_limit() {
        for label in ["evil\x1b[2J", "two\nlines", &"x".repeat(65)] {
            let settings = PortConfig {
                label: Some(label.to_owned()),
                ..Default::default()
            };
            assert_eq!(problems(settings).len(), 1, "{label:?}");
        }
    }

    #[test]
    fn a_port_outside_the_inventory_is_a_problem() {
        let config = config("com9", PortConfig::default());
        let problems = config.problems(&inventory(), &adapters());
        assert_eq!(problems[0].subject, "serial.com9");
    }

    #[test]
    fn only_problems_a_change_adds_count_against_it() {
        let orphaned = config("com9", PortConfig::default());
        let mut fixed_elsewhere = orphaned.clone();
        fixed_elsewhere.serial.insert(
            "com1".to_owned(),
            PortConfig {
                mode: "slip".to_owned(),
                ..Default::default()
            },
        );
        assert!(
            fixed_elsewhere
                .new_problems(&orphaned, &inventory(), &adapters())
                .is_empty()
        );

        let clean = Config::default();
        assert_eq!(
            orphaned
                .new_problems(&clean, &inventory(), &adapters())
                .len(),
            1
        );
    }

    #[test]
    fn a_change_keeps_what_it_does_not_mention() {
        let port = PortConfig {
            mode: "slip".to_owned(),
            baud: 38400,
            framing: "7E1".to_owned(),
            flow: Flow::RtsCts,
            label: Some("PS/2 Model 70".to_owned()),
        };
        let changed = port.with(SerialChange {
            baud: Some(9600),
            ..Default::default()
        });
        assert_eq!(
            changed,
            PortConfig {
                baud: 9600,
                ..port.clone()
            }
        );

        let cleared = port.with(SerialChange {
            label: Some(String::new()),
            ..Default::default()
        });
        assert_eq!(cleared.label, None);
    }
}
