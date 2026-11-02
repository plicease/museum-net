//! The serial adapters this build supports. Nothing starts them yet, so a
//! mode assignment is stored and validated but doesn't drive a port.

use museum_core::config::AdapterDescriptor;

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
