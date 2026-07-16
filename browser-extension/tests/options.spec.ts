// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest';

function renderOptions(): void {
  document.body.innerHTML = `
    <main>
      <section id="setup-panel">
        <form id="connection-form">
          <input id="backend-url" />
          <input id="username" />
          <button id="connect" type="submit">连接</button>
        </form>
      </section>
      <section id="connected-panel" hidden>
        <span id="connected-backend"></span>
        <span id="connected-username"></span>
        <button id="edit-connection" type="button">修改</button>
      </section>
      <p id="connection-status"></p>
    </main>
  `;
}

function installChrome(
  response: { ok: true; data: unknown } | { ok: false; message: string }
) {
  const sendMessage = vi.fn().mockResolvedValue(response);
  vi.stubGlobal('chrome', {
    storage: {
      local: {
        get: vi.fn().mockResolvedValue({
          blrecExtensionSettings: {
            backendUrl: 'http://nas.local:2234',
            username: 'owner',
            token: 'stored-token',
          },
        }),
        set: vi.fn().mockResolvedValue(undefined),
      },
    },
    runtime: { sendMessage },
    permissions: { request: vi.fn().mockResolvedValue(true) },
  });
  return sendMessage;
}

describe('extension options', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
    renderOptions();
  });

  it('validates a stored token and shows a compact connected state', async () => {
    const sendMessage = installChrome({
      ok: true,
      data: { collected: false, recording: false },
    });

    await import('../src/options');

    await vi.waitFor(() => {
      expect(
        document.querySelector<HTMLElement>('#connected-panel')?.hidden
      ).toBe(false);
    });
    expect(
      document.querySelector<HTMLElement>('#setup-panel')?.hidden
    ).toBe(true);
    expect(document.querySelector('#connected-backend')?.textContent).toBe(
      'http://nas.local:2234'
    );
    expect(document.querySelector('#connected-username')?.textContent).toBe(
      'owner'
    );
    expect(sendMessage).toHaveBeenCalledWith({
      type: 'ROOM_STATUS',
      roomId: 0,
    });

    document.querySelector<HTMLButtonElement>('#edit-connection')?.click();

    expect(
      document.querySelector<HTMLElement>('#connected-panel')?.hidden
    ).toBe(true);
    expect(
      document.querySelector<HTMLElement>('#setup-panel')?.hidden
    ).toBe(false);
  });

  it('shows the form when the stored token is no longer valid', async () => {
    installChrome({ ok: false, message: '插件令牌无效或已撤销' });

    await import('../src/options');

    await vi.waitFor(() => {
      expect(document.querySelector('#connection-status')?.textContent).toContain(
        '插件令牌无效或已撤销'
      );
    });
    expect(
      document.querySelector<HTMLElement>('#setup-panel')?.hidden
    ).toBe(false);
    expect(
      document.querySelector<HTMLElement>('#connected-panel')?.hidden
    ).toBe(true);
  });
});
