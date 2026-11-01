# A lightweight Linux client on a museum network.
{ pkgs, ... }:
{
  environment.systemPackages = [ pkgs.curl ];
}
