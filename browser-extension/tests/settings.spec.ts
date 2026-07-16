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
    expect(normalizeBackendUrl('192.168.50.24:2233')).toBe(
      'http://192.168.50.24:2233'
    );
    expect(normalizeBackendUrl(' https://nas.example/blrec/ ')).toBe(
      'https://nas.example/blrec'
    );
    expect(() => normalizeBackendUrl('ftp://nas.example')).toThrow(
      '仅支持 HTTP 或 HTTPS'
    );
  });

  it('trims the username without changing its case', () => {
    expect(normalizeUsername('  LuWei  ')).toBe('LuWei');
    expect(() => normalizeUsername('   ')).toThrow('管理员用户名不能为空');
  });

  it('round-trips settings through local extension storage', async () => {
    const storage = memoryStorage();
    await saveSettings(
      {
        backendUrl: 'http://192.168.50.24:2233',
        username: 'LuWei',
        token: 'blrec_ext_secret',
      },
      storage
    );

    await expect(loadSettings(storage)).resolves.toEqual({
      backendUrl: 'http://192.168.50.24:2233',
      username: 'LuWei',
      token: 'blrec_ext_secret',
    });
  });
});
