# The management service. NixOS declares what a gateway has; museumd owns how
# it is assigned, in /var/lib/museumd.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.museumNet.museumd;
  inventory = pkgs.writeText "museum-inventory.json" (
    builtins.toJSON {
      version = 1;
      ports = lib.mapAttrsToList (name: port: { inherit name; inherit (port) device kind; }) cfg.serialPorts;
      interfaces = lib.mapAttrsToList (name: zone: { inherit name zone; }) config.museumNet.networking.interfaces;
    }
  );
in
{
  options.museumNet.museumd = {
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../package.nix { };
      description = "Package providing museumd and museumctl.";
    };
    serialPorts = lib.mkOption {
      description = "Serial ports museumd may assign, by name.";
      default = { };
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            device = lib.mkOption {
              type = lib.types.str;
              description = "Stable device path.";
            };
            kind = lib.mkOption {
              type = lib.types.enum [
                "uart"
                "virtual"
              ];
              description = "Whether this is real hardware or an emulated port.";
            };
          };
        }
      );
    };
  };

  config = {
    environment.systemPackages = [ cfg.package ];

    # Members of `museum` can read. Changes also need `museum-admin` or root.
    users.groups.museum = { };
    users.groups.museum-admin = { };

    # Deliberately independent of the network: this is the way back in when
    # every interface is misconfigured.
    systemd.services.museumd = {
      description = "Museum Net management service";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/museumd"
          "--socket"
          "/run/museumd/museumd.sock"
          "--state-dir"
          "/var/lib/museumd"
          "--inventory"
          "${inventory}"
          "--admin-group"
          "museum-admin"
        ];
        Restart = "on-failure";

        # The socket inherits root:museum and 0660 from these.
        Group = "museum";
        UMask = "0117";
        RuntimeDirectory = "museumd";
        RuntimeDirectoryMode = "0750";
        StateDirectory = "museumd";
        StateDirectoryMode = "0700";

        # museumd only talks to its socket and state, and reads interface
        # state from /sys. Adapters and the web portal will need some of this
        # back.
        CapabilityBoundingSet = "";
        DevicePolicy = "closed";
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        IPAddressDeny = "any";
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RestrictAddressFamilies = "AF_UNIX";
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" ];
      };
    };
  };
}
