from pathlib import Path
from typing import Dict, Literal, Mapping, Optional

import toml
from pydantic import BaseModel, Field, root_validator
from typing_extensions import Annotated

from blrec.utils.string import camel_case


class _NetworkModel(BaseModel):
    class Config:
        validate_assignment = True
        anystr_strip_whitespace = True
        allow_population_by_field_name = True
        alias_generator = camel_case


class NetworkRouteSettings(_NetworkModel):
    mode: Literal['fixed', 'round_robin', 'parallel'] = 'fixed'
    interface: Optional[str] = None
    failover_enabled: bool = True

    @root_validator(pre=True)
    def _migrate_legacy_route(cls, values: Dict[str, object]) -> Dict[str, object]:
        migrated = dict(values)
        if 'interface' not in migrated:
            if 'primary_interface' in migrated:
                migrated['interface'] = migrated.get('primary_interface')
            elif 'primaryInterface' in migrated:
                migrated['interface'] = migrated.get('primaryInterface')
        migrated.setdefault('mode', 'fixed')
        return migrated


class NetworkInterfaceSettings(_NetworkModel):
    enabled: bool = True
    archive_download_enabled: bool = True
    upload_limit_bps: Annotated[int, Field(ge=0)] = 0


class NetworkSettings(_NetworkModel):
    interfaces: Dict[str, NetworkInterfaceSettings] = {}
    room_status: NetworkRouteSettings = NetworkRouteSettings()
    danmaku: NetworkRouteSettings = NetworkRouteSettings()
    recording: NetworkRouteSettings = NetworkRouteSettings()
    upload: NetworkRouteSettings = NetworkRouteSettings()
    bili_api: NetworkRouteSettings = NetworkRouteSettings()
    archive_download: NetworkRouteSettings = NetworkRouteSettings()
    dashboard_publish: NetworkRouteSettings = NetworkRouteSettings()

    @root_validator(pre=True)
    def _inherit_dashboard_publish_route(
        cls, values: Dict[str, object]
    ) -> Dict[str, object]:
        migrated = dict(values)
        if 'dashboard_publish' in migrated or 'dashboardPublish' in migrated:
            return migrated
        upload = migrated.get('upload')
        if isinstance(upload, dict):
            migrated['dashboard_publish'] = dict(upload)
        elif isinstance(upload, NetworkRouteSettings):
            migrated['dashboard_publish'] = upload.dict()
        return migrated

    @root_validator(pre=True)
    def _inherit_archive_download_route(
        cls, values: Dict[str, object]
    ) -> Dict[str, object]:
        migrated = dict(values)
        if 'archive_download' in migrated or 'archiveDownload' in migrated:
            return migrated
        recording = migrated.get('recording')
        if not isinstance(recording, dict):
            return migrated
        interface = recording.get('interface')
        if interface is None:
            interface = recording.get(
                'primary_interface', recording.get('primaryInterface')
            )
        if isinstance(interface, str) and interface:
            migrated['archive_download'] = {
                'mode': 'fixed',
                'interface': interface,
                'failover_enabled': False,
            }
        return migrated

    @root_validator
    def _credential_routes_must_be_fixed(
        cls, values: Dict[str, object]
    ) -> Dict[str, object]:
        for field in ('upload', 'bili_api', 'dashboard_publish'):
            route = values.get(field)
            if isinstance(route, NetworkRouteSettings) and route.mode != 'fixed':
                raise ValueError('{} network route must use fixed mode'.format(field))
        archive_route = values.get('archive_download')
        if isinstance(
            archive_route, NetworkRouteSettings
        ) and archive_route.mode not in ('fixed', 'parallel'):
            raise ValueError(
                'archive_download network route must use fixed or parallel mode'
            )
        for field in ('room_status', 'danmaku', 'recording'):
            route = values.get(field)
            if isinstance(route, NetworkRouteSettings) and route.mode == 'parallel':
                raise ValueError(
                    '{} network route cannot use parallel mode'.format(field)
                )
        for field in ('upload', 'archive_download', 'dashboard_publish'):
            route = values.get(field)
            if isinstance(route, NetworkRouteSettings) and route.failover_enabled:
                values[field] = route.copy(update={'failover_enabled': False})
        return values


def load_network_settings(path: Path) -> NetworkSettings:
    document = toml.load(str(path))
    network = document.get('network', {})
    if not isinstance(network, Mapping):
        raise ValueError('network settings must be a table')
    return NetworkSettings.parse_obj(dict(network))
