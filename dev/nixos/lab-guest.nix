# Boots a lab VM from a kernel and initrd supplied by libvirt, with the host's
# Nix store mounted read-only and a disposable disk for mutable state.
{
  lib,
  modulesPath,
  hostName,
  labMachine,
  ...
}:
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];

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

  nix.enable = false;
  documentation.enable = false;
}
