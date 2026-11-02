# A lightweight Linux client on a museum network. Its firewall stays off so an
# active FTP data connection can reach it.
{ pkgs, ... }:
{
  # Python and VDE are for testing client network cleanup against a real
  # kernel, as an unprivileged user.
  environment.systemPackages = [
    pkgs.curl
    pkgs.python3
    pkgs.vde2
  ];
  networking.firewall.enable = false;

  users.users.lab-tester = {
    isNormalUser = true;
  };
}
