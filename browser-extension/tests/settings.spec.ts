import { describe, expect, it } from 'vitest';

import {
  loadSettings,
  normalizeBackendUrl,
  normalizeUsername,
  saveSettings,
  SettingsStorage,
} from '../src/shared/settings';

function memoryStorage(): SettingsStorage & { values: Record<string, unknown> } {
  const values: Record<string, unknown> = {};
  return {
    values,
    async get() {
      return { ...values };
    },
    async set(items) {
      Object.assign(values, items);
    },
  };
}

describe('extension settings', () => {
  it('normalizes common NAS addresses', () => {
    expect(normalizeBackendUrl('192.168.1.100:2233')).toBe(
      'http://192.168.1.100:2233'
    );
    expect(normalizeBackendUrl(' https://nas.example/blrec/ ')).toBe(
      'https://nas.example/blrec'
    );
    expect(() => normalizeBackendUrl('ftp://nas.example')).toThrow(
      '仅支持 HTTP 或 HTTPS'
    );
  });

  it('requires HTTPS outside localhost and private networks', () => {
    expect(normalizeBackendUrl('http://localhost:2233')).toBe(
      'http://localhost:2233'
    );
    expect(normalizeBackendUrl('http://10.0.0.8:2233')).toBe(
      'http://10.0.0.8:2233'
    );
    expect(normalizeBackendUrl('http://172.20.0.8:2233')).toBe(
      'http://172.20.0.8:2233'
    );
    expect(normalizeBackendUrl('http://192.168.1.100:2233')).toBe(
      'http://192.168.1.100:2233'
    );
    expect(normalizeBackendUrl('http://[fd00::1]:2233')).toBe(
      'http://[fd00::1]:2233'
    );
    expect(() => normalizeBackendUrl('http://blrec.example.com')).toThrow(
      '公网地址必须使用 HTTPS'
    );
    expect(() => normalizeBackendUrl('http://fcloud.example.com')).toThrow(
      '公网地址必须使用 HTTPS'
    );
    expect(() =>
      normalizeBackendUrl('http://[2001:4860:4860::8888]:2233')
    ).toThrow('公网地址必须使用 HTTPS');
  });

  it('trims the username without changing its case', () => {
    expect(normalizeUsername('  LuWei  ')).toBe('LuWei');
    expect(() => normalizeUsername('   ')).toThrow('管理员用户名不能为空');
  });

  it('round-trips settings through local extension storage', async () => {
    const storage = memoryStorage();
    await saveSettings(
      {
        backendUrl: 'http://192.168.1.100:2233',
        username: 'LuWei',
        token: 'blrec_ext_secret',
      },
      storage
    );

    await expect(loadSettings(storage)).resolves.toEqual({
      backendUrl: 'http://192.168.1.100:2233',
      username: 'LuWei',
      token: 'blrec_ext_secret',
    });
  });
});
