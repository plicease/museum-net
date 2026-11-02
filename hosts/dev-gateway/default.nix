{ pkgs, ... }:
{
  imports = [ ../../modules/gateway.nix ];

  museumNet.networking.interfaces = {
    mgmt0 = "management";
    tun-mgmt = "vpn";
    museum0 = "museum";
    museum1 = "museum";
  };

  fileSystems."/srv/archive" = {
    device = "${pkgs.callPackage ../../dev/nixos/archive-fixture.nix { }}";
    fsType = "none";
    options = [
      "bind"
      "ro"
      "nofail"
    ];
  };

  # Their devices arrive with the virtual serial lab; until then museumd only
  # stores their assignments.
  museumNet.museumd.serialPorts = {
    com1 = {
      device = "/run/museum-serial/com1";
      kind = "virtual";
    };
    com2 = {
      device = "/run/museum-serial/com2";
      kind = "virtual";
    };
  };

  users.users.museum-admin.extraGroups = [
    "museum"
    "museum-admin"
  ];
  # Can read state but not change it, so the tests can check the difference.
  users.users.museum-viewer = {
    isSystemUser = true;
    group = "museum";
  };
}
