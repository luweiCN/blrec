import { afterEach, describe, expect, it, vi } from 'vitest';

import { handleBackgroundMessage } from '../src/background';
import { SettingsStorage } from '../src/shared/settings';

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

describe('background message handling', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('calls the native fetch implementation with the global scope receiver', async () => {
    const storage = memoryStorage();
    let fetchReceiver: unknown;
    const nativeFetch = vi.fn(function (this: unknown) {
      fetchReceiver = this;
      if (this !== globalThis) {
        throw new TypeError('Illegal invocation');
      }
      return Promise.resolve(
        new Response(JSON.stringify({ tokenId: 7, token: 'blrec_ext_token' }), {
          status: 201,
          headers: { 'content-type': 'application/json' },
        })
      );
    });
    vi.stubGlobal('fetch', nativeFetch);

    const response = await handleBackgroundMessage(
      {
        type: 'PAIR',
        backendUrl: 'http://nas.local:2233',
        username: 'owner',
      },
      { storage }
    );

    expect(response).toEqual({ ok: true, data: { tokenId: 7 } });
    expect(fetchReceiver).toBe(globalThis);
  });

  it('pairs with username only and saves the returned token', async () => {
    const storage = memoryStorage();
    const fetchFn = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ tokenId: 7, token: 'blrec_ext_token' }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      })
    );

    const response = await handleBackgroundMessage(
      {
        type: 'PAIR',
        backendUrl: '192.168.1.100:2233/',
        username: ' owner ',
      },
      { storage, fetchFn }
    );

    expect(response).toEqual({ ok: true, data: { tokenId: 7 } });
    expect(storage.values).toEqual({
      blrecExtensionSettings: {
        backendUrl: 'http://192.168.1.100:2233',
        username: 'owner',
        token: 'blrec_ext_token',
      },
    });
    const request = fetchFn.mock.calls[0];
    expect(request[0]).toBe(
      'http://192.168.1.100:2233/api/v1/browser-extension/pair'
    );
    expect(JSON.parse(String(request[1].body))).toEqual({ username: 'owner' });
    expect(request[1].headers).not.toHaveProperty(
      'X-BLREC-Extension-Token'
    );
  });

  it('uses the stored token for restricted room requests', async () => {
    const storage = memoryStorage();
    storage.values['blrecExtensionSettings'] = {
      backendUrl: 'http://nas:2233',
      username: 'owner',
      token: 'blrec_ext_token',
    };
    const fetchFn = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ collected: true, recording: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    );

    const response = await handleBackgroundMessage(
      { type: 'ROOM_STATUS', roomId: 100 },
      { storage, fetchFn }
    );

    expect(response).toEqual({
      ok: true,
      data: { collected: true, recording: true },
    });
    expect(fetchFn.mock.calls[0][1].headers).toMatchObject({
      'X-BLREC-Extension-Token': 'blrec_ext_token',
    });
  });
});
