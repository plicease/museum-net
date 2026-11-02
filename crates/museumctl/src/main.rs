//! Local command line client for `museumd`. It only needs the Unix socket, so
//! it works with every network interface down.

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{ArgGroup, Args, Parser, Subcommand};
use museum_core::client::call;
use museum_core::config::{Flow, PortConfig, Problem, SerialChange};
use museum_core::protocol::{ApiError, Change, PortView, Reply, Request};
use serde_json::json;

#[derive(Parser)]
#[command(version, about = "Manage a Museum Net gateway")]
struct Cli {
    /// The museumd socket
    #[arg(
        long,
        env = "MUSEUMD_SOCKET",
        default_value = "/run/museumd/museumd.sock"
    )]
    socket: PathBuf,
    /// Print the daemon's reply as JSON
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show the service, its current revision, and any problems
    Status,
    /// Inspect and assign serial ports
    #[command(subcommand)]
    Serial(SerialCommand),
    /// Inspect network interfaces
    #[command(subcommand)]
    Network(NetworkCommand),
    /// Inspect the configuration and its change log
    #[command(subcommand)]
    Config(ConfigCommand),
}

#[derive(Subcommand)]
enum SerialCommand {
    /// List ports, what they should do, and what they are doing
    List,
    /// List the modes this gateway accepts
    Adapters,
    /// Store new settings for a port; settings you don't mention stay as they are
    Set(SetArgs),
    /// Store a port as disabled, keeping its line settings
    Disable { port: String },
}

#[derive(Subcommand)]
enum NetworkCommand {
    /// List interfaces, their zones, and their kernel state
    List,
}

#[derive(Args)]
#[command(group(
    ArgGroup::new("settings")
        .required(true)
        .multiple(true)
        .args(["mode", "baud", "framing", "flow", "label"])
))]
struct SetArgs {
    port: String,
    /// `disabled`, or a name from `serial adapters`
    #[arg(long)]
    mode: Option<String>,
    #[arg(long)]
    baud: Option<u32>,
    /// Data bits, parity, and stop bits, such as 8N1
    #[arg(long)]
    framing: Option<String>,
    /// none, rts-cts, or xon-xoff
    #[arg(long)]
    flow: Option<Flow>,
    /// What is connected to this port; empty clears it
    #[arg(long)]
    label: Option<String>,
}

#[derive(Subcommand)]
enum ConfigCommand {
    /// Print the configuration of the current or a given revision
    Show {
        #[arg(long)]
        revision: Option<u64>,
    },
    /// Check the current configuration against this gateway
    Validate,
    /// Show the change log, newest first
    History {
        #[arg(long, default_value_t = 20)]
        limit: usize,
    },
    /// Make the configuration of an earlier revision current
    Rollback { revision: u64 },
}

impl From<Command> for Request {
    fn from(command: Command) -> Self {
        match command {
            Command::Status => Request::Status,
            Command::Serial(SerialCommand::List) => Request::SerialList,
            Command::Serial(SerialCommand::Adapters) => Request::SerialAdapters,
            Command::Serial(SerialCommand::Set(args)) => Request::SerialSet {
                port: args.port,
                change: SerialChange {
                    mode: args.mode,
                    baud: args.baud,
                    framing: args.framing,
                    flow: args.flow,
                    label: args.label,
                },
            },
            Command::Serial(SerialCommand::Disable { port }) => Request::SerialDisable { port },
            Command::Network(NetworkCommand::List) => Request::NetworkList,
            Command::Config(ConfigCommand::Show { revision }) => Request::ConfigShow { revision },
            Command::Config(ConfigCommand::Validate) => Request::ConfigValidate,
            Command::Config(ConfigCommand::History { limit }) => {
                Request::ConfigHistory { limit: Some(limit) }
            }
            Command::Config(ConfigCommand::Rollback { revision }) => {
                Request::ConfigRollback { revision }
            }
        }
    }
}

fn change_line(change: &Change) -> String {
    format!(
        "{:>6}  {}  {}  {}",
        change.revision, change.time, change.actor, change.action
    )
}

fn print_ports(ports: &[PortView]) {
    println!(
        "{:<8} {:<12} {:<12} {:<10} {:<7} {:<8} {:<8} {:<9} LABEL",
        "PORT", "DESIRED", "ACTIVE", "STATE", "BAUD", "FRAMING", "FLOW", "KIND"
    );
    for port in ports {
        let PortConfig {
            mode,
            baud,
            framing,
            flow,
            label,
        } = &port.desired;
        let active = port.active.as_ref().map_or("none", |active| &active.mode);
        let kind = serde_json::to_value(port.kind).expect("kinds serialize");
        println!(
            "{:<8} {mode:<12} {active:<12} {:<10} {baud:<7} {framing:<8} {:<8} {:<9} {}",
            port.name,
            port.state.to_string(),
            flow.to_string(),
            kind.as_str().unwrap_or_default(),
            label.as_deref().unwrap_or(""),
        );
    }
    print_port_messages(ports);
}

fn print_port_messages(ports: &[PortView]) {
    for port in ports {
        if let Some(message) = &port.message {
            println!("{}: {message}", port.name);
        }
    }
}

fn print_problems(problems: &[Problem]) {
    for problem in problems {
        println!("problem: {problem}");
    }
}

fn print_reply(reply: &Reply) {
    match reply {
        Reply::Status(status) => {
            println!(
                "museumd {} (config schema {})",
                status.version, status.schema
            );
            println!("revision {}", change_line(&status.current).trim_start());
            println!();
            print_ports(&status.ports);
            print_problems(&status.problems);
        }
        Reply::Ports { ports } => print_ports(ports),
        Reply::Adapters { adapters } => {
            for adapter in adapters {
                println!("{:<12} {}", adapter.name, adapter.description);
            }
        }
        Reply::Interfaces { interfaces } => {
            println!("{:<16} {:<12} STATE", "INTERFACE", "ZONE");
            for interface in interfaces {
                println!(
                    "{:<16} {:<12} {}",
                    interface.name,
                    interface.zone.to_string(),
                    interface.state
                );
            }
        }
        Reply::Stored(stored) => {
            if stored.changed {
                println!("stored as revision {}", stored.revision);
            } else {
                println!("unchanged, still revision {}", stored.revision);
            }
            print_port_messages(&stored.pending);
        }
        Reply::Config(view) => {
            let text = serde_json::to_string_pretty(view).expect("views serialize");
            println!("{text}");
        }
        Reply::Validation { problems } if problems.is_empty() => println!("valid"),
        Reply::Validation { problems } => print_problems(problems),
        Reply::History { changes } => {
            for change in changes {
                println!("{}", change_line(change));
            }
        }
    }
}

fn report(error: &ApiError, as_json: bool) {
    if as_json {
        eprintln!("{}", json!(error));
    } else {
        eprintln!("museumctl: {error}");
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let response = match call(&cli.socket, cli.command.into()) {
        Ok(response) => response,
        Err(error) => {
            eprintln!("museumctl: {error}");
            return ExitCode::FAILURE;
        }
    };
    match response {
        Ok(reply) => {
            if cli.json {
                println!("{}", json!(reply));
            } else {
                print_reply(&reply);
            }
            match reply {
                Reply::Validation { problems } if !problems.is_empty() => ExitCode::FAILURE,
                _ => ExitCode::SUCCESS,
            }
        }
        Err(error) => {
            report(&error, cli.json);
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn the_command_line_is_well_formed() {
        Cli::command().debug_assert();
    }

    fn parse(args: &[&str]) -> Result<Request, clap::Error> {
        Cli::try_parse_from(["museumctl"].iter().chain(args)).map(|cli| cli.command.into())
    }

    #[test]
    fn the_designed_commands_parse() {
        let request = parse(&["serial", "set", "com1", "--mode", "slip", "--baud", "38400"]);
        assert!(matches!(request, Ok(Request::SerialSet { .. })));
        // Modes come from the daemon, so an adapter this build has never heard
        // of still reaches it.
        assert!(parse(&["serial", "set", "com1", "--mode", "future-thing"]).is_ok());
    }

    #[test]
    fn set_needs_something_to_change() {
        assert!(parse(&["serial", "set", "com1"]).is_err());
    }

    #[test]
    fn bad_flow_control_is_caught_locally() {
        assert!(parse(&["serial", "set", "com1", "--flow", "smoke-signals"]).is_err());
    }
}
