# Boots a lab VM from a kernel and initrd supplied by libvirt, with the host's
# Nix store mounted read-only and a disposable disk for mutable state.
{
  config,
  lib,
  modulesPath,
  pkgs,
  hostName,
  labMachine,
  ...
}:
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
    ../../modules/vpn.nix
  ];

  nixpkgs.hostPlatform = "x86_64-linux";
  system.stateVersion = "26.05";

  boot.loader.grub.enable = false;
  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.kernelModules = [
    "9p"
    "9pnet_virtio"
    "virtio_blk"
    "virtio_pci"
  ];

  fileSystems."/" = {
    device = "/dev/vda";
    fsType = "ext4";
    autoFormat = true;
  };
  fileSystems."/nix/store" = {
    device = "nix-store";
    fsType = "9p";
    neededForBoot = true;
    options = [
      "trans=virtio"
      "version=9p2000.L"
      "ro"
    ];
  };

  networking.hostName = hostName;
  networking.useDHCP = false;
  networking.useNetworkd = true;

  # Bind interfaces by MAC address, never by kernel device order.
  systemd.network.links = lib.listToAttrs (
    map (interface: {
      name = "10-${interface.name}";
      value = {
        matchConfig.PermanentMACAddress = interface.mac;
        linkConfig.Name = interface.name;
      };
    }) labMachine.interfaces
  );
  systemd.network.networks = lib.listToAttrs (
    map (interface: {
      name = "10-${interface.name}";
      value = {
        matchConfig.Name = interface.name;
        address = [ interface.address ];
        linkConfig.RequiredForOnline = "no";
      };
    }) labMachine.interfaces
  );

  # The lab has no host network path, so it is driven through libvirt: the
  # console for people and the guest agent for tests.
  services.qemuGuest.enable = true;
  services.getty.autologinUser = "root";
  users.allowNoPasswordLogin = true;

  # Every VM can read the shared Nix store, so private files can't be there.
  # Each machine gets its own on a read-only disk instead.
  systemd.services.lab-secrets = lib.mkIf labMachine.hasSecrets {
    description = "Unpack this machine's lab credentials";
    wantedBy = [ "multi-user.target" ];
    before = [
      "sshd.service"
      "openvpn-management.service"
    ];
    requiredBy = lib.optional config.services.openssh.enable "sshd.service"
      ++ lib.optional (labMachine.vpn != null) "openvpn-management.service";
    unitConfig.DefaultDependencies = false;
    requires = [ "dev-disk-by\\x2did-virtio\\x2dlab\\x2dsecrets.device" ];
    after = [ "dev-disk-by\\x2did-virtio\\x2dlab\\x2dsecrets.device" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      RuntimeDirectory = "lab-secrets";
      RuntimeDirectoryMode = "0700";
      RuntimeDirectoryPreserve = true;
      ExecStart = "${pkgs.gnutar}/bin/tar -xf /dev/disk/by-id/virtio-lab-secrets -C /run/lab-secrets --no-same-owner";
    };
  };

  museumNet.vpn = lib.mkIf (labMachine.vpn != null && labMachine.vpn.role == "client") {
    enable = true;
    server = labMachine.vpn.server;
    serverName = labMachine.vpn.serverName;
    ca = pkgs.writeText "lab-ca.crt" labMachine.vpn.ca;
    cert = "/run/lab-secrets/vpn.crt";
    key = "/run/lab-secrets/vpn.key";
  };

  services.openssh.hostKeys = lib.mkIf config.services.openssh.enable [
    {
      path = "/run/lab-secrets/ssh_host_ed25519_key";
      type = "ed25519";
    }
  ];
  users.users.museum-admin = lib.mkIf config.services.openssh.enable {
    isNormalUser = true;
    openssh.authorizedKeys.keys = labMachine.authorizedKeys;
  };
  programs.ssh.knownHosts = lib.mapAttrs (_: publicKey: { inherit publicKey; }) labMachine.knownHosts;

  nix.enable = false;
  documentation.enable = false;
}
