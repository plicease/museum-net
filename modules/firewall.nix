{
  # Vintage-facing interfaces must never route to other networks. Later
  # milestones add service policy; these stay off regardless.
  boot.kernel.sysctl = {
    "net.ipv4.ip_forward" = 0;
    "net.ipv6.conf.all.forwarding" = 0;
  };
  networking.firewall.enable = true;
  networking.nftables.enable = true;

  # Linux answers for any local address on any interface, so forwarding being
  # off is not enough: a museum client routing through the gateway could still
  # reach its management and VPN addresses. Accept only the address of the
  # interface a packet arrived on.
  networking.nftables.tables.museum-isolation = {
    family = "inet";
    content = ''
      chain input {
        type filter hook input priority filter - 10; policy accept;
        iifname "museum*" fib daddr . iif type != { local, broadcast, multicast } drop
      }
    '';
  };
}
