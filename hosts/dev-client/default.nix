# A lightweight Linux client on a museum network. Its firewall stays off so an
# active FTP data connection can reach it.
{ pkgs, ... }:
{
  environment.systemPackages = [ pkgs.curl ];
  networking.firewall.enable = false;
}
