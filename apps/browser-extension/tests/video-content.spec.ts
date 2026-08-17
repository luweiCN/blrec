// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MatchMarkerContentController } from '../src/video-content';
import { BackgroundMessage, BackgroundResponse } from '../src/shared/messages';

const controllers: MatchMarkerContentController[] = [];

function locationAt(pathname: string, search = ''): Location {
  return { pathname, search } as Location;
}

describe('Bilibili video match marker', () => {
  beforeEach(() => {
    document.body.innerHTML = '<video></video>';
  });

  afterEach(() => {
    controllers.splice(0).forEach((controller) => controller.destroy());
  });

  it('stops polling when the video is outside the allowed indexed account', async () => {
    const cancelRefresh = vi.fn();
    const scheduleRefresh = vi.fn(() => cancelRefresh);
    const sendMessage = vi.fn(
      async (_message: BackgroundMessage): Promise<BackgroundResponse> => ({
        ok: true,
        data: {
          indexed: false,
          sessionId: null,
          partId: null,
          partIndex: null,
        },
      }),
    );
    const controller = new MatchMarkerContentController({
      document,
      location: locationAt('/video/BV1WogL61E64/'),
      sendMessage,
      scheduleRefresh,
    });
    controllers.push(controller);

    controller.start();

    await vi.waitFor(() => expect(cancelRefresh).toHaveBeenCalledOnce());
    expect(sendMessage).toHaveBeenCalledOnce();
    expect(document.querySelector('.blrec-match-marker-actions')).toBeNull();
  });

  it('keeps the marker active for an allowed indexed video', async () => {
    const cancelRefresh = vi.fn();
    const sendMessage = vi.fn(
      async (_message: BackgroundMessage): Promise<BackgroundResponse> => ({
        ok: true,
        data: {
          indexed: true,
          sessionId: 12,
          partId: 34,
          partIndex: 2,
        },
      }),
    );
    const controller = new MatchMarkerContentController({
      document,
      location: locationAt('/video/BV1abcdefgh/', '?p=2'),
      sendMessage,
      scheduleRefresh: () => cancelRefresh,
    });
    controllers.push(controller);

    controller.start();

    await vi.waitFor(() =>
      expect(
        document.querySelector('.blrec-match-marker-actions')?.textContent,
      ).toBe('标记对局'),
    );
    expect(cancelRefresh).not.toHaveBeenCalled();
  });
});
