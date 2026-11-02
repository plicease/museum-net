# Read-only anonymous FTP and plain HTTP for museum networks. Both serve the
# same tree from `root`, where a host mounts its archive.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  root = "/srv/archive";
  notice = "ARCHIVE-UNAVAILABLE.TXT";
  passivePorts = {
    from = 2120;
    to = 2139;
  };
  museumInterfaces = config.museumNet.networking.museumInterfaces;
in
{
  # The notice lives in the mount point's own directory, so mounted media hides
  # it and missing media shows it, with no state to keep in step.
  systemd.services.archive-notice = {
    description = "Report a missing archive";
    wantedBy = [ "multi-user.target" ];
    before = [
      "srv-archive.mount"
      "nginx.service"
      "vsftpd.service"
    ];
    unitConfig.DefaultDependencies = false;
    after = [ "systemd-remount-fs.service" ];
    path = [ pkgs.util-linux ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      mkdir -p ${root}
      if ! mountpoint -q ${root}; then
        chmod 0755 ${root}
        echo "The archive is unavailable: its media isn't mounted." > ${root}/${notice}
        chmod 0444 ${root}/${notice}
        chmod 0555 ${root}
      fi
    '';
  };

  services.vsftpd = {
    enable = true;
    anonymousUser = true;
    anonymousUserNoPassword = true;
    anonymousUserHome = root;
    extraConfig = ''
      listen=YES
      listen_ipv6=NO
      connect_from_port_20=YES
      hide_ids=YES
      ftpd_banner=Museum Net archive. Anonymous, read-only.
      pasv_enable=YES
      pasv_min_port=${toString passivePorts.from}
      pasv_max_port=${toString passivePorts.to}
    '';
  };

  services.nginx = {
    enable = true;
    serverTokens = false;
    virtualHosts.archive = {
      default = true;
      listen = [
        {
          addr = "0.0.0.0";
          port = 80;
        }
      ];
      root = root;
      extraConfig = ''
        autoindex on;
        # A client without a Host header gets the address it connected to.
        server_name_in_redirect off;
        port_in_redirect off;
      '';
      locations."/".extraConfig = ''
        if (-f ${root}/${notice}) {
          return 503 "The archive is unavailable.\n";
        }
        limit_except GET {
          deny all;
        }
      '';
    };
  };

  # The firewall decides who can reach these. This also stops them answering on
  # any other interface if the firewall is ever wrong.
  systemd.services.vsftpd.serviceConfig.RestrictNetworkInterfaces = museumInterfaces;
  systemd.services.nginx.serviceConfig.RestrictNetworkInterfaces = museumInterfaces;

  networking.firewall.interfaces = lib.genAttrs museumInterfaces (_: {
    allowedTCPPorts = [
      21
      80
    ];
    allowedTCPPortRanges = [ passivePorts ];
  });
}
