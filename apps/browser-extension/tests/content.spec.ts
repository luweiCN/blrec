// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { HighlightContentController } from '../src/content';
import { BackgroundMessage, BackgroundResponse } from '../src/shared/messages';

class FakeObserver {
  readonly observe = vi.fn();
  readonly disconnect = vi.fn();

  constructor(readonly callback: MutationCallback) {}

  trigger(): void {
    this.callback([], this as unknown as MutationObserver);
  }
}

const controllers: HighlightContentController[] = [];

function trackController(
  controller: HighlightContentController
): HighlightContentController {
  controllers.push(controller);
  return controller;
}

function locationAt(pathname: string): Location {
  return { pathname } as Location;
}

function makeController(
  status: { collected: boolean; recording: boolean }
) {
  const sendMessage = vi.fn(
    async (message: BackgroundMessage): Promise<BackgroundResponse> => {
      if (message.type === 'ROOM_STATUS') {
        return { ok: true, data: status };
      }
      return { ok: true, data: {} };
    }
  );
  const observers: FakeObserver[] = [];
  let refresh: (() => Promise<void>) | null = null;
  const controller = trackController(
    new HighlightContentController({
      document,
      location: locationAt('/100'),
      sendMessage,
      now: () => 1_000_000,
      createObserver: (callback) => {
        const observer = new FakeObserver(callback);
        observers.push(observer);
        return observer;
      },
      scheduleRefresh: (callback) => {
        refresh = callback;
        return () => undefined;
      },
    })
  );
  return {
    controller,
    sendMessage,
    get observer() {
      const observer = observers[0];
      if (!observer) {
        throw new Error('observer was not created');
      }
      return observer;
    },
    get modeObserver() {
      const observer = observers[1];
      if (!observer) {
        throw new Error('mode observer was not created');
      }
      return observer;
    },
    async refresh() {
      if (!refresh) {
        throw new Error('refresh was not scheduled');
      }
      await refresh();
    },
  };
}

describe('Bilibili live controls', () => {
  afterEach(() => {
    controllers.splice(0).forEach((controller) => controller.destroy());
  });

  beforeEach(() => {
    document.head.innerHTML = '';
    document.body.className = '';
    document.body.innerHTML = [
      '<div class="right-ctnr"></div>',
      '<div id="fullscreen-container"><div id="live-player"></div></div>',
    ].join('');
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      value: null,
    });
  });

  it('shows the two collection actions for an uncollected room', async () => {
    const { controller } = makeController({ collected: false, recording: false });

    await controller.start();

    const text = document.querySelector('.blrec-highlight-actions')?.textContent;
    expect(text).toContain('收录');
    expect(text).toContain('收录并投稿');
    expect(text).not.toContain('添加高光');
  });

  it('shows no label or action while collected but not recording', async () => {
    const { controller } = makeController({ collected: true, recording: false });

    await controller.start();

    expect(document.querySelector('.blrec-highlight-actions')).toBeNull();
    expect(document.body.textContent).not.toContain('已收录');
  });

  it('shows only add-highlight while recording', async () => {
    const { controller } = makeController({ collected: true, recording: true });

    await controller.start();

    const text = document.querySelector('.blrec-highlight-actions')?.textContent;
    expect(text).toBe('添加高光');
  });

  it('moves the existing action into the player while web fullscreen is active', async () => {
    const setup = makeController({ collected: true, recording: true });
    await setup.controller.start();
    const actions = document.querySelector<HTMLElement>(
      '.blrec-highlight-actions'
    )!;
    expect(setup.modeObserver.observe).toHaveBeenCalledWith(document.body, {
      attributes: true,
      attributeFilter: ['class'],
    });

    document.body.classList.add('player-full-win');
    setup.modeObserver.trigger();

    expect(actions.parentElement?.id).toBe('live-player');
    expect(actions.classList.contains('blrec-highlight-actions--player')).toBe(
      true
    );
    expect(document.querySelectorAll('.blrec-highlight-actions')).toHaveLength(1);

    document.body.classList.remove('player-full-win');
    setup.modeObserver.trigger();

    expect(actions.parentElement?.className).toBe('right-ctnr');
    expect(actions.classList.contains('blrec-highlight-actions--player')).toBe(
      false
    );
  });

  it('keeps the action and feedback inside the browser fullscreen tree', async () => {
    const setup = makeController({ collected: true, recording: true });
    await setup.controller.start();
    const fullscreenContainer = document.querySelector<HTMLElement>(
      '#fullscreen-container'
    )!;
    const player = document.querySelector<HTMLElement>('#live-player')!;
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      value: fullscreenContainer,
    });

    document.dispatchEvent(new Event('fullscreenchange'));

    const actions = document.querySelector<HTMLElement>(
      '.blrec-highlight-actions'
    )!;
    expect(actions.parentElement).toBe(player);
    actions.querySelector<HTMLButtonElement>('button')!.click();
    actions
      .querySelector<HTMLButtonElement>('[data-action="save-highlight"]')!
      .click();
    await vi.waitFor(() =>
      expect(document.querySelector('.blrec-highlight-toast')?.parentElement).toBe(
        player
      )
    );

    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      value: null,
    });
    document.dispatchEvent(new Event('fullscreenchange'));

    expect(actions.parentElement?.className).toBe('right-ctnr');
  });

  it('keeps controls inside an internal fullscreen target', async () => {
    const player = document.querySelector<HTMLElement>('#live-player')!;
    player.innerHTML = '<div id="internal-fullscreen"></div>';
    const fullscreenTarget = document.querySelector<HTMLElement>(
      '#internal-fullscreen'
    )!;
    const setup = makeController({ collected: true, recording: true });
    await setup.controller.start();
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      value: fullscreenTarget,
    });

    document.dispatchEvent(new Event('fullscreenchange'));

    expect(
      document.querySelector('.blrec-highlight-actions')?.parentElement
    ).toBe(fullscreenTarget);
  });

  it('keeps failed highlight feedback inside the fullscreen tree', async () => {
    const setup = makeController({ collected: true, recording: true });
    setup.sendMessage.mockImplementation(async (message) => {
      if (message.type === 'ROOM_STATUS') {
        return { ok: true, data: { collected: true, recording: true } };
      }
      return { ok: false, message: '保存失败' };
    });
    await setup.controller.start();
    const fullscreenContainer = document.querySelector<HTMLElement>(
      '#fullscreen-container'
    )!;
    const player = document.querySelector<HTMLElement>('#live-player')!;
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      value: fullscreenContainer,
    });
    document.dispatchEvent(new Event('fullscreenchange'));

    const actions = document.querySelector<HTMLElement>(
      '.blrec-highlight-actions'
    )!;
    actions.querySelector<HTMLButtonElement>('button')!.click();
    actions
      .querySelector<HTMLButtonElement>('[data-action="save-highlight"]')!
      .click();

    await vi.waitFor(() => {
      const toast = document.querySelector<HTMLElement>(
        '.blrec-highlight-toast'
      );
      expect(toast?.parentElement).toBe(player);
      expect(toast?.dataset['state']).toBe('error');
    });
  });

  it('does not revive observers or controls after destroy during status load', async () => {
    let resolveStatus:
      | ((response: BackgroundResponse) => void)
      | undefined;
    const sendMessage = vi.fn(
      () =>
        new Promise<BackgroundResponse>((resolve) => {
          resolveStatus = resolve;
        })
    );
    const createObserver = vi.fn(
      (callback: MutationCallback) => new FakeObserver(callback)
    );
    const scheduleRefresh = vi.fn(() => () => undefined);
    const controller = trackController(
      new HighlightContentController({
        document,
        location: locationAt('/100'),
        sendMessage,
        createObserver,
        scheduleRefresh,
      })
    );

    const starting = controller.start();
    await vi.waitFor(() => expect(sendMessage).toHaveBeenCalledOnce());
    controller.destroy();
    resolveStatus?.({
      ok: true,
      data: { collected: true, recording: true },
    });
    await starting;

    expect(createObserver).not.toHaveBeenCalled();
    expect(scheduleRefresh).not.toHaveBeenCalled();
    expect(document.querySelector('.blrec-highlight-actions')).toBeNull();
  });

  it('starts once and restores a removed container without duplication', async () => {
    const setup = makeController({ collected: false, recording: false });

    await setup.controller.start();
    await setup.controller.start();
    expect(document.querySelectorAll('.blrec-highlight-actions')).toHaveLength(1);
    expect(setup.sendMessage).toHaveBeenCalledTimes(1);

    document.querySelector('.blrec-highlight-actions')?.remove();
    setup.observer.trigger();

    expect(document.querySelectorAll('.blrec-highlight-actions')).toHaveLength(1);
  });

  it('refreshes local room status so add-highlight appears after recording starts', async () => {
    const status = { collected: true, recording: false };
    const setup = makeController(status);
    await setup.controller.start();
    expect(document.querySelector('.blrec-highlight-actions')).toBeNull();

    status.recording = true;
    await setup.refresh();

    expect(document.querySelector('.blrec-highlight-actions')?.textContent).toBe(
      '添加高光'
    );
  });

  it('uses the canonical room ID returned after collecting a short room', async () => {
    const statusRoomIds: number[] = [];
    const sendMessage = vi.fn(async (message: BackgroundMessage) => {
      if (message.type === 'ROOM_STATUS') {
        statusRoomIds.push(message.roomId);
        return {
          ok: true as const,
          data: {
            collected: message.roomId === 3582149,
            recording: false,
          },
        };
      }
      if (message.type === 'CONTROL_OPERATION') {
        return {
          ok: true as const,
          data: {
            id: 'operation-1',
            status: 'succeeded',
            result: {
              requestedRoomId: 6,
              resolvedRoomId: 3582149,
              collected: true,
              upload: false,
            },
            errorCode: null,
          },
        };
      }
      return {
        ok: true as const,
        data: {
          operationId: 'operation-1',
          status: 'accepted',
          requestedRoomId: 6,
        },
      };
    });
    const controller = trackController(
      new HighlightContentController({
        document,
        location: locationAt('/6'),
        sendMessage,
        waitForOperationPoll: () => Promise.resolve(),
        createObserver: (callback) => new FakeObserver(callback),
        scheduleRefresh: () => () => undefined,
      })
    );
    await controller.start();

    document
      .querySelector<HTMLButtonElement>('.blrec-highlight-actions button')!
      .click();
    await vi.waitFor(() => expect(statusRoomIds).toEqual([6, 3582149]));
  });

  it('rejects a succeeded membership result that omits collected', async () => {
    const setup = makeController({ collected: false, recording: false });
    setup.sendMessage.mockImplementation(async (message) => {
      if (message.type === 'ROOM_STATUS') {
        return { ok: true, data: { collected: false, recording: false } };
      }
      if (message.type === 'COLLECT') {
        return {
          ok: true,
          data: {
            operationId: 'operation-1',
            status: 'accepted',
            requestedRoomId: 100,
          },
        };
      }
      return {
        ok: true,
        data: {
          id: 'operation-1',
          status: 'succeeded',
          result: { resolvedRoomId: 100, upload: false },
          errorCode: null,
        },
      };
    });
    const controller = trackController(
      new HighlightContentController({
        document,
        location: locationAt('/100'),
        sendMessage: setup.sendMessage,
        waitForOperationPoll: () => Promise.resolve(),
        createObserver: (callback) => new FakeObserver(callback),
        scheduleRefresh: () => () => undefined,
      })
    );
    await controller.start();

    document
      .querySelector<HTMLButtonElement>('.blrec-highlight-actions button')!
      .click();

    await vi.waitFor(() =>
      expect(document.querySelector('.blrec-highlight-toast')?.textContent).toBe(
        'BLREC 返回的收录结果不完整'
      )
    );
    expect(
      document.querySelector<HTMLButtonElement>(
        '.blrec-highlight-actions button'
      )?.disabled
    ).toBe(false);
  });

  it('rejects a terminal upload result that differs from the requested action', async () => {
    const setup = makeController({ collected: false, recording: false });
    setup.sendMessage.mockImplementation(async (message) => {
      if (message.type === 'ROOM_STATUS') {
        return { ok: true, data: { collected: false, recording: false } };
      }
      if (message.type === 'COLLECT') {
        return {
          ok: true,
          data: {
            operationId: 'operation-1',
            status: 'accepted',
            requestedRoomId: 100,
          },
        };
      }
      return {
        ok: true,
        data: {
          id: 'operation-1',
          status: 'succeeded',
          result: { resolvedRoomId: 100, collected: true, upload: false },
          errorCode: null,
        },
      };
    });
    const controller = trackController(
      new HighlightContentController({
        document,
        location: locationAt('/100'),
        sendMessage: setup.sendMessage,
        waitForOperationPoll: () => Promise.resolve(),
        createObserver: (callback) => new FakeObserver(callback),
        scheduleRefresh: () => () => undefined,
      })
    );
    await controller.start();

    const buttons = document.querySelectorAll<HTMLButtonElement>(
      '.blrec-highlight-actions button'
    );
    buttons[1].click();

    await vi.waitFor(() =>
      expect(document.querySelector('.blrec-highlight-toast')?.textContent).toBe(
        'BLREC 返回的投稿设置与请求不一致'
      )
    );
    expect(buttons[0].disabled).toBe(false);
    expect(buttons[1].disabled).toBe(false);
  });

  it('stops client polling on destroy without cancelling the durable operation', async () => {
    let releasePoll: (() => void) | undefined;
    const waitForOperationPoll = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          releasePoll = resolve;
        })
    );
    const setup = makeController({ collected: false, recording: false });
    const controller = trackController(
      new HighlightContentController({
        document,
        location: locationAt('/100'),
        sendMessage: setup.sendMessage,
        createObserver: (callback) => new FakeObserver(callback),
        scheduleRefresh: () => () => undefined,
        waitForOperationPoll,
      })
    );
    setup.sendMessage.mockImplementation(async (message) => {
      if (message.type === 'ROOM_STATUS') {
        return { ok: true, data: { collected: false, recording: false } };
      }
      if (message.type === 'COLLECT') {
        return {
          ok: true,
          data: {
            operationId: 'operation-1',
            status: 'accepted',
            requestedRoomId: 100,
          },
        };
      }
      return {
        ok: true,
        data: { id: 'operation-1', status: 'running', result: null },
      };
    });
    await controller.start();
    document
      .querySelector<HTMLButtonElement>('.blrec-highlight-actions button')!
      .click();
    await vi.waitFor(() => expect(waitForOperationPoll).toHaveBeenCalledOnce());

    controller.destroy();
    releasePoll?.();
    await Promise.resolve();

    expect(
      setup.sendMessage.mock.calls.filter(
        ([message]) => message.type === 'CONTROL_OPERATION'
      )
    ).toHaveLength(0);
  });

  it('locks the click time, accepts a name and allows repeated highlights', async () => {
    document.title = '直播标题';
    document.body.insertAdjacentHTML(
      'beforeend',
      '<span class="room-owner-username">主播</span><video></video>'
    );
    const video = document.querySelector('video')!;
    video.currentTime = 100.5;
    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 119 },
    });
    let now = 1_000_000;
    const setup = makeController({ collected: true, recording: true });
    (setup.controller as unknown as { now: () => number }).now = () => now;
    await setup.controller.start();
    const button = document.querySelector<HTMLButtonElement>(
      '.blrec-highlight-actions button'
    )!;

    button.click();
    const input = document.querySelector<HTMLInputElement>(
      '.blrec-highlight-popover input'
    )!;
    expect(input).not.toBeNull();
    expect(input.autocomplete).toBe('off');
    expect(input.getAttribute('data-1p-ignore')).toBe('true');
    expect(input.getAttribute('data-op-ignore')).toBe('true');
    now = 1_005_000;
    input.value = '精彩操作';
    input.dispatchEvent(new Event('input'));
    document
      .querySelector<HTMLButtonElement>('[data-action="save-highlight"]')!
      .click();
    await vi.waitFor(() => expect(button.disabled).toBe(false));
    button.click();
    document
      .querySelector<HTMLButtonElement>('[data-action="save-highlight"]')!
      .click();
    await vi.waitFor(() =>
      expect(
        setup.sendMessage.mock.calls.filter(
          ([message]) => message.type === 'ADD_HIGHLIGHT'
        )
      ).toHaveLength(2)
    );

    expect(setup.sendMessage).toHaveBeenCalledWith({
      type: 'ADD_HIGHLIGHT',
      roomId: 100,
      observedAtMs: 1_000_000,
      currentTimeMs: 100_500,
      seekableEndMs: 119_000,
      rawDelayMs: 18_500,
      baselineDelayMs: 18_500,
      effectiveRewindMs: 0,
      playerDelayMs: 0,
      name: '精彩操作',
      title: '直播标题',
      anchorName: '主播',
    });
  });

  it('cancels the naming popover with Escape without saving', async () => {
    const setup = makeController({ collected: true, recording: true });
    await setup.controller.start();
    document
      .querySelector<HTMLButtonElement>('.blrec-highlight-actions button')!
      .click();

    const input = document.querySelector<HTMLInputElement>(
      '.blrec-highlight-popover input'
    )!;
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));

    expect(document.querySelector('.blrec-highlight-popover')).toBeNull();
    expect(
      setup.sendMessage.mock.calls.some(
        ([message]) => message.type === 'ADD_HIGHLIGHT'
      )
    ).toBe(false);
  });
});
