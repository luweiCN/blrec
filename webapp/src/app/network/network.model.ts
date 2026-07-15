export type NetworkPurpose =
  | 'roomStatus'
  | 'danmaku'
  | 'recording'
  | 'upload'
  | 'biliApi';

export interface NetworkProbe {
  reachable: boolean;
  latencyMs: number | null;
  externalIp: string | null;
  error: string | null;
  checkedAt: number;
}

export interface NetworkInterface {
  name: string;
  address: string;
  netmask: string | null;
  gateway: string | null;
  isUp: boolean;
  speedMbps: number;
  isDefault: boolean;
  probe: NetworkProbe | null;
}

export interface NetworkInterfaceResponse {
  interfaces: NetworkInterface[];
}
