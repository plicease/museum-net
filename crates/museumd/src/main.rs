//! The management service, and the single authority for operational
//! configuration on a gateway.

mod adapters;
mod auth;
mod server;
mod service;
mod store;

use std::error::Error;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Parser;
use museum_core::inventory::Inventory;
use nix::unistd::Group;
use tokio::net::UnixListener;
use tracing::{error, info};

use crate::service::Service;
use crate::store::Store;

#[derive(Parser)]
#[command(version, about = "Museum Net management service")]
struct Args {
    /// Socket to listen on. Its permissions decide who can reach the service,
    /// and come from the process's group and umask.
    #[arg(long)]
    socket: PathBuf,
    /// Directory for the configuration revisions
    #[arg(long)]
    state_dir: PathBuf,
    /// This gateway's serial port inventory
    #[arg(long)]
    inventory: PathBuf,
    /// Group whose members may change the configuration
    #[arg(long)]
    admin_group: String,
}

fn bind(path: &Path) -> std::io::Result<UnixListener> {
    match fs::remove_file(path) {
        Err(error) if error.kind() != ErrorKind::NotFound => return Err(error),
        _ => {}
    }
    UnixListener::bind(path)
}

async fn run(args: Args) -> Result<(), Box<dyn Error>> {
    let admin_group = Group::from_name(&args.admin_group)
        .ok()
        .flatten()
        .ok_or_else(|| format!("no group named {}", args.admin_group))?
        .gid;
    let inventory = fs::read_to_string(&args.inventory)
        .map_err(|error| format!("{}: {error}", args.inventory.display()))?;
    let inventory = Inventory::parse(&inventory)
        .map_err(|error| format!("{}: {error}", args.inventory.display()))?;

    let store = Store::open(&args.state_dir)?;
    let service = Service::new(store, inventory, adapters::catalog())?;
    let listener =
        bind(&args.socket).map_err(|error| format!("{}: {error}", args.socket.display()))?;

    info!(socket = %args.socket.display(), "listening");
    server::serve(listener, service, admin_group).await?;
    Ok(())
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = Args::parse();
    // The journal adds its own timestamps.
    tracing_subscriber::fmt()
        .without_time()
        .with_writer(std::io::stderr)
        .init();
    match run(args).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(failure) => {
            error!("{failure}");
            ExitCode::FAILURE
        }
    }
}
