# Emulates the observable OpenVPN and SSH behaviour of production nullray.
{
  imports = [
    ../../dev/nixos/lab-vpn-server.nix
    ../../modules/management.nix
  ];
}
