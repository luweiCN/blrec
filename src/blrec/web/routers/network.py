from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from blrec.networking.manager import NetworkRouteManager
from blrec.utils.string import camel_case

router = APIRouter(prefix='/network', tags=['network'])
manager: Optional[NetworkRouteManager] = None


class ProbeRequest(BaseModel):
    interface_name: Optional[str] = None

    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class InterfaceUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    upload_limit_bps: Optional[int] = Field(None, ge=0)

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


def snapshot() -> Dict[str, List[Dict[str, Any]]]:
    network_manager = _manager()
    probes = network_manager.cached_probes()
    traffic: Dict[str, Dict[str, float]] = {}
    for item in network_manager.traffic_meter.snapshot():
        if item.interface_name is None:
            continue
        totals = traffic.setdefault(
            item.interface_name,
            {
                'uploadBps': 0.0,
                'downloadBps': 0.0,
                'uploadTotal': 0.0,
                'downloadTotal': 0.0,
            },
        )
        totals['uploadBps'] += item.upload_bps
        totals['downloadBps'] += item.download_bps
        totals['uploadTotal'] += item.upload_total
        totals['downloadTotal'] += item.download_total
    interfaces: List[Dict[str, Any]] = []
    for interface in network_manager.interfaces().values():
        probe = probes.get(interface.name)
        interface_traffic = traffic.get(interface.name, {})
        interfaces.append(
            {
                'name': interface.name,
                'address': interface.address,
                'netmask': interface.netmask,
                'gateway': interface.gateway,
                'isUp': interface.is_up,
                'speedMbps': interface.speed_mbps,
                'isDefault': interface.is_default,
                'dnsServers': list(interface.dns_servers),
                'kind': interface.kind,
                'enabled': interface.enabled,
                'uploadLimitBps': interface.upload_limit_bps,
                'uploadBps': interface_traffic.get('uploadBps', 0.0),
                'downloadBps': interface_traffic.get('downloadBps', 0.0),
                'uploadTotal': int(interface_traffic.get('uploadTotal', 0.0)),
                'downloadTotal': int(interface_traffic.get('downloadTotal', 0.0)),
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
    await _manager().refresh_interfaces()
    return snapshot()


@router.patch('/interfaces/{interface_name}')
async def update_interface(
    interface_name: str, request: InterfaceUpdateRequest
) -> Dict[str, List[Dict[str, Any]]]:
    network_manager = _manager()
    try:
        await network_manager.update_interface(
            interface_name,
            enabled=request.enabled,
            upload_limit_bps=request.upload_limit_bps,
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Network interface not found'
        ) from None
    await network_manager.refresh_interfaces(force=True)
    return snapshot()


@router.post('/probe')
async def probe_networks(request: ProbeRequest) -> Dict[str, List[Dict[str, Any]]]:
    network_manager = _manager()
    await network_manager.refresh_interfaces()
    try:
        await network_manager.probe(request.interface_name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Network interface not found'
        ) from None
    return snapshot()
