{
  description = "Museum Net gateways and virtual development lab";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    {
      nixosModules.gateway = import ./modules/gateway.nix;
    };
}
