{
  # Vintage-facing interfaces must never route to other networks. Later
  # milestones add the nftables policy; these stay off regardless.
  boot.kernel.sysctl = {
    "net.ipv4.ip_forward" = 0;
    "net.ipv6.conf.all.forwarding" = 0;
  };
  networking.firewall.enable = true;
}
