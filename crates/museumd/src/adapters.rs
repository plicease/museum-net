//! The serial adapters this build accepts, and what is running them. There is
//! no service controller yet, so nothing runs: see "Activation" in the design.

use museum_core::config::{AdapterDescriptor, DISABLED, PortConfig};
use museum_core::protocol::RuntimeState;

/// What a port is actually doing.
pub struct Observed {
    pub active: Option<PortConfig>,
    pub state: RuntimeState,
    pub message: Option<String>,
}

/// Report a port's runtime state. No service can own a port yet, so this is
/// always stopped, and says so if something else was requested.
pub fn observe(desired: &PortConfig) -> Observed {
    Observed {
        active: None,
        state: RuntimeState::Stopped,
        message: (desired.mode != DISABLED).then(|| {
            format!(
                "{} is stored but not active: this build can't start adapters",
                desired.mode
            )
        }),
    }
}

pub fn catalog() -> Vec<AdapterDescriptor> {
    [
        ("interloper", "Bootstrap and disk image delivery"),
        ("ppp", "Point-to-point IP with negotiated settings"),
        ("slip", "Point-to-point IP"),
        ("sprocket", "File transfer and disk imaging"),
    ]
    .into_iter()
    .map(|(name, description)| AdapterDescriptor {
        name: name.to_owned(),
        description: description.to_owned(),
    })
    .collect()
}
