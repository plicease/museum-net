# OpenVPN client for the management VPN. There is deliberately no default
# server, so a host can only reach the VPN it is explicitly configured for.
{ config, lib, ... }:
let
  cfg = config.museumNet.vpn;
in
{
  options.museumNet.vpn = {
    enable = lib.mkEnableOption "the management VPN client";
    server = lib.mkOption {
      type = lib.types.str;
      description = "Address of the OpenVPN server.";
    };
    serverName = lib.mkOption {
      type = lib.types.str;
      description = "Expected common name of the server certificate.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 1194;
    };
    ca = lib.mkOption {
      type = lib.types.path;
      description = "CA certificate. Public, so it may be in the Nix store.";
    };
    cert = lib.mkOption {
      type = lib.types.str;
      description = "Path to this host's certificate, outside the Nix store.";
    };
    key = lib.mkOption {
      type = lib.types.str;
      description = "Path to this host's private key, outside the Nix store.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Restarted by systemd and by OpenVPN's own keepalive, and never ordered
    # before anything else: museum services must not depend on the VPN.
    services.openvpn.servers.management = {
      autoStart = true;
      updateResolvConf = false;
      config = ''
        client
        dev tun-mgmt
        dev-type tun
        proto udp
        remote ${cfg.server} ${toString cfg.port}
        nobind
        persist-key
        resolv-retry infinite
        connect-retry 2 10
        connect-retry-max unlimited
        ca ${cfg.ca}
        cert ${cfg.cert}
        key ${cfg.key}
        remote-cert-tls server
        verify-x509-name ${cfg.serverName} name
        auth-nocache

        # The VPN is for administration. It must never become a route for
        # anything else, whatever the server pushes.
        route-nopull
        pull-filter ignore "redirect-gateway"
        pull-filter ignore "route"
        pull-filter ignore "dhcp-option"
      '';
    };
    networking.firewall.interfaces.tun-mgmt.allowedTCPPorts = [ 22 ];
  };
}
