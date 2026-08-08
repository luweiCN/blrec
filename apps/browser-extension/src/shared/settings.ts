export interface ExtensionSettings {
  readonly backendUrl: string;
  readonly username: string;
  readonly token: string;
}

export interface SettingsStorage {
  get(keys: string | string[]): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
}

const STORAGE_KEY = 'blrecExtensionSettings';

export function normalizeBackendUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error('BLREC 地址不能为空');
  }
  const candidate = /^[a-z][a-z\d+.-]*:\/\//i.test(trimmed)
    ? trimmed
    : `http://${trimmed}`;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch (_error) {
    throw new Error('BLREC 地址格式不正确');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error('仅支持 HTTP 或 HTTPS');
  }
  if (url.protocol === 'http:' && !isLocalHostname(url.hostname)) {
    throw new Error('公网地址必须使用 HTTPS');
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('BLREC 地址不能包含账号、查询参数或锚点');
  }
  return `${url.origin}${url.pathname.replace(/\/+$/, '')}`;
}

function isLocalHostname(hostname: string): boolean {
  const value = hostname.toLowerCase().replace(/^\[|\]$/g, '');
  if (
    value === 'localhost' ||
    value.endsWith('.localhost') ||
    value.endsWith('.local') ||
    (!value.includes('.') && !value.includes(':'))
  ) {
    return true;
  }
  if (value.includes(':')) {
    return (
      value === '::1' ||
      value.startsWith('fc') ||
      value.startsWith('fd') ||
      value.startsWith('fe80:')
    );
  }
  const octets = value.split('.').map(Number);
  if (octets.length !== 4 || octets.some((octet) => !Number.isInteger(octet))) {
    return false;
  }
  return (
    octets[0] === 10 ||
    octets[0] === 127 ||
    (octets[0] === 169 && octets[1] === 254) ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168)
  );
}

export function normalizeUsername(value: string): string {
  const username = value.trim();
  if (!username) {
    throw new Error('管理员用户名不能为空');
  }
  if (username.length > 64) {
    throw new Error('管理员用户名不能超过 64 个字符');
  }
  return username;
}

export async function loadSettings(
  storage: SettingsStorage = chromeStorage()
): Promise<ExtensionSettings> {
  const values = await storage.get(STORAGE_KEY);
  const stored = values[STORAGE_KEY];
  if (typeof stored !== 'object' || stored === null) {
    return { backendUrl: '', username: '', token: '' };
  }
  const value = stored as Partial<ExtensionSettings>;
  return {
    backendUrl: typeof value.backendUrl === 'string' ? value.backendUrl : '',
    username: typeof value.username === 'string' ? value.username : '',
    token: typeof value.token === 'string' ? value.token : '',
  };
}

export async function saveSettings(
  settings: ExtensionSettings,
  storage: SettingsStorage = chromeStorage()
): Promise<void> {
  await storage.set({ [STORAGE_KEY]: settings });
}

export function chromeStorage(): SettingsStorage {
  return {
    get: (keys) => chrome.storage.local.get(keys),
    set: (items) => chrome.storage.local.set(items),
  };
}
