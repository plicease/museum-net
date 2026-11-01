# The lab's OpenVPN server. Clients get fixed addresses, and only the names
# listed in the client config directory can connect.
{
  lib,
  pkgs,
  labMachine,
  ...
}:
let
  vpn = labMachine.vpn;
  clientConfigs = pkgs.linkFarm "vpn-client-configs" (
    lib.mapAttrsToList (name: address: {
      inherit name;
      path = pkgs.writeText "vpn-client-${name}" "ifconfig-push ${address} ${vpn.netmask}\n";
    }) vpn.clients
  );
in
{
  services.openvpn.servers.management = {
    autoStart = true;
    updateResolvConf = false;
    config = ''
      dev tun-mgmt
      dev-type tun
      proto udp
      port 1194
      topology subnet
      server ${vpn.network} ${vpn.netmask} nopool
      client-config-dir ${clientConfigs}
      ccd-exclusive
      client-to-client
      keepalive 2 10
      persist-key
      dh none
      ca ${pkgs.writeText "lab-ca.crt" vpn.ca}
      cert /run/lab-secrets/vpn.crt
      key /run/lab-secrets/vpn.key
      remote-cert-tls client
    '';
  };
  networking.firewall.interfaces.mgmt0.allowedUDPPorts = [ 1194 ];
  networking.firewall.interfaces.tun-mgmt.allowedTCPPorts = [ 22 ];
}
