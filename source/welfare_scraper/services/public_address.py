"""Shared connection-address rule, independent of sites and policy names."""
import ipaddress

# RFC 6052 well-known /96. Network-specific NAT64 prefixes cannot be inferred
# from an arbitrary IPv6 address; routing policy must also isolate internal nets.
_NAT64_WELL_KNOWN = ipaddress.IPv6Network('64:ff9b::/96')


def is_public_address(value):
    address = ipaddress.ip_address(value)
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_global
        if address in _NAT64_WELL_KNOWN:
            return ipaddress.IPv4Address(int(address) & 0xffffffff).is_global
    return True
