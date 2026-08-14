export type NetworkPurpose =
  | 'roomStatus'
  | 'danmaku'
  | 'recording'
  | 'upload'
  | 'biliApi'
  | 'archiveDownload'
  | 'dashboardPublish'
  | 'database'
  | 'cloudCost'
  | 'visitorAnalytics';

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
  dnsServers: string[];
  kind: 'physical' | 'bridge' | 'tunnel';
  enabled: boolean;
  archiveDownloadEnabled: boolean;
  uploadLimitBps: number;
  uploadBps: number;
  downloadBps: number;
  uploadTotal: number;
  downloadTotal: number;
  probe: NetworkProbe | null;
}

export interface NetworkInterfaceUpdate {
  enabled?: boolean;
  archiveDownloadEnabled?: boolean;
  uploadLimitBps?: number;
}

export interface NetworkInterfaceResponse {
  interfaces: NetworkInterface[];
}
