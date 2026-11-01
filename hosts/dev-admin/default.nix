# A separate authorized management client, connected through the VPN. It also
# hosts one-shot OpenVPN attempts for the credential tests.
{ pkgs, ... }:
{
  environment.systemPackages = [ pkgs.openvpn ];
}
