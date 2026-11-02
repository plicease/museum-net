# Logical network zones. Physical devices are bound to these names per host.
{ config, lib, ... }:
let
  cfg = config.museumNet.networking;
  inZone = zone: lib.attrNames (lib.filterAttrs (_: value: value == zone) cfg.interfaces);
in
{
  options.museumNet.networking = {
    interfaces = lib.mkOption {
      description = "Network interfaces by name, and the zone each belongs to.";
      default = { };
      type = lib.types.attrsOf (
        lib.types.enum [
          "management"
          "museum"
          "vpn"
          "disabled"
        ]
      );
    };
    museumInterfaces = lib.mkOption {
      description = "Interfaces in the museum zone.";
      type = lib.types.listOf lib.types.str;
      readOnly = true;
      default = inZone "museum";
    };
  };

  config = {
    # The firewall matches museum interfaces by name.
    assertions = lib.mapAttrsToList (name: zone: {
      assertion = (zone == "museum") == lib.hasPrefix "museum" name;
      message = "Interface ${name} is in zone ${zone}: only museum interfaces can be named museum*.";
    }) cfg.interfaces;

    # Museum networks are IPv4 only, so a client can't reach anything by way of
    # an address family the service policy doesn't cover.
    systemd.network.networks = lib.listToAttrs (
      map (name: {
        name = "10-${name}";
        value.networkConfig = {
          LinkLocalAddressing = "no";
          IPv6AcceptRA = false;
        };
      }) cfg.museumInterfaces
    );
  };
}
