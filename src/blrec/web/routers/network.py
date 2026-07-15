from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from blrec.networking.manager import NetworkRouteManager
from blrec.utils.string import camel_case

router = APIRouter(prefix='/network', tags=['network'])
manager: Optional[NetworkRouteManager] = None


class ProbeRequest(BaseModel):
    interface_name: Optional[str] = None

    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


def _manager() -> NetworkRouteManager:
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Network manager is unavailable',
        )
    return manager


def _snapshot() -> Dict[str, List[Dict[str, Any]]]:
    network_manager = _manager()
    probes = network_manager.cached_probes()
    interfaces: List[Dict[str, Any]] = []
    for interface in network_manager.interfaces().values():
        probe = probes.get(interface.name)
        interfaces.append(
            {
                'name': interface.name,
                'address': interface.address,
                'netmask': interface.netmask,
                'gateway': interface.gateway,
                'isUp': interface.is_up,
                'speedMbps': interface.speed_mbps,
                'isDefault': interface.is_default,
                'probe': (
                    None
                    if probe is None
                    else {
                        'reachable': probe.reachable,
                        'latencyMs': probe.latency_ms,
                        'externalIp': probe.external_ip,
                        'error': probe.error,
                        'checkedAt': probe.checked_at,
                    }
                ),
            }
        )
    return {'interfaces': interfaces}


@router.get('/interfaces')
async def get_interfaces() -> Dict[str, List[Dict[str, Any]]]:
    return _snapshot()


@router.post('/probe')
async def probe_networks(request: ProbeRequest) -> Dict[str, List[Dict[str, Any]]]:
    try:
        await _manager().probe(request.interface_name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Network interface not found'
        ) from None
    return _snapshot()
