{
  description = "Museum Net gateways and virtual development lab";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # Machine addresses and MACs. dev/scripts override this with values
    # generated for the local host.
    lab = {
      url = "path:./dev/lab-example";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      lab,
    }:
    let
      inherit (nixpkgs) lib;
      machines = (lib.importJSON "${lab}/lab.json").machines;
      labHost =
        name: machine:
        lib.nixosSystem {
          specialArgs = {
            hostName = name;
            labMachine = machine;
          };
          modules = [
            ./hosts/${name}
            ./dev/nixos/lab-guest.nix
          ];
        };
    in
    {
      packages.x86_64-linux.default = nixpkgs.legacyPackages.x86_64-linux.callPackage ./package.nix { };
      nixosModules.gateway = import ./modules/gateway.nix;
      nixosConfigurations = lib.mapAttrs labHost machines;
    };
}
