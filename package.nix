{ lib, rustPlatform }:
rustPlatform.buildRustPackage {
  pname = "museum-net";
  version = "0.0.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Cargo.toml
      ./Cargo.lock
      ./crates
    ];
  };
  cargoLock.lockFile = ./Cargo.lock;

  cargoBuildFlags = [
    "--package=museumd"
    "--package=museumctl"
  ];
  cargoTestFlags = [
    "--package=museum-core"
    "--package=museumd"
    "--package=museumctl"
  ];

  meta.mainProgram = "museumctl";
}
