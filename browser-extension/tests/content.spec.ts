// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest';

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
  let observer: FakeObserver | null = null;
  const controller = new HighlightContentController({
    document,
    location: locationAt('/100'),
    sendMessage,
    now: () => 1_000_000,
    createObserver: (callback) => {
      observer = new FakeObserver(callback);
      return observer;
    },
  });
  return {
    controller,
    sendMessage,
    get observer() {
      if (!observer) {
        throw new Error('observer was not created');
      }
      return observer;
    },
  };
}

describe('Bilibili live controls', () => {
  beforeEach(() => {
    document.head.innerHTML = '';
    document.body.innerHTML = '<div class="right-ctnr"></div>';
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

  it('sends player-adjusted data and allows repeated highlights', async () => {
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
    const setup = makeController({ collected: true, recording: true });
    await setup.controller.start();
    const button = document.querySelector<HTMLButtonElement>(
      '.blrec-highlight-actions button'
    )!;

    button.click();
    await vi.waitFor(() => expect(button.disabled).toBe(false));
    button.click();
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
      playerDelayMs: 18_500,
      title: '直播标题',
      anchorName: '主播',
    });
  });
});
