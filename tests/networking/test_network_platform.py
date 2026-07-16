from types import SimpleNamespace

from blrec.networking import platform


def _stats():
    return {
        'ovs_eth0': SimpleNamespace(isup=True, speed=1000),
        'ovs_eth1': SimpleNamespace(isup=True, speed=1000),
        'docker0': SimpleNamespace(isup=True, speed=0),
    }


def test_linux_policy_routes_supply_all_interface_gateways() -> None:
    addresses = [
        {
            'ifname': 'ovs_eth0',
            'operstate': 'UP',
            'addr_info': [
                {
                    'family': 'inet',
                    'local': '192.168.1.24',
                    'prefixlen': 24,
                    'scope': 'global',
                }
            ],
        },
        {
            'ifname': 'ovs_eth1',
            'operstate': 'UP',
            'addr_info': [
                {
                    'family': 'inet',
                    'local': '192.168.50.100',
                    'prefixlen': 24,
                    'scope': 'global',
                }
            ],
        },
    ]
    routes = [
        {
            'dst': 'default',
            'gateway': '192.168.1.1',
            'dev': 'ovs_eth0',
            'table': 'ovs_eth0-table',
            'prefsrc': '192.168.1.24',
        },
        {
            'dst': 'default',
            'gateway': '192.168.50.1',
            'dev': 'ovs_eth1',
            'table': 'main',
            'prefsrc': '192.168.50.100',
        },
    ]

    interfaces = platform._parse_linux_interfaces(
        addresses, routes, stats=_stats(), dns_servers=('8.8.8.8', '114.114.114.114')
    )

    assert interfaces['ovs_eth0'].gateway == '192.168.1.1'
    assert interfaces['ovs_eth1'].gateway == '192.168.50.1'
    assert interfaces['ovs_eth0'].dns_servers == ('8.8.8.8', '114.114.114.114')
    assert interfaces['ovs_eth1'].is_default is True


def test_bridge_without_external_route_defaults_disabled() -> None:
    addresses = [
        {
            'ifname': 'docker0',
            'operstate': 'UP',
            'addr_info': [
                {
                    'family': 'inet',
                    'local': '172.17.0.1',
                    'prefixlen': 16,
                    'scope': 'global',
                }
            ],
        }
    ]

    interfaces = platform._parse_linux_interfaces(
        addresses, [], stats=_stats(), dns_servers=('8.8.8.8',)
    )

    assert interfaces['docker0'].kind == 'bridge'
    assert interfaces['docker0'].gateway is None
    assert interfaces['docker0'].enabled is False
