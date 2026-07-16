import { ExtensionApi } from './shared/api';
import {
  BackgroundMessage,
  BackgroundResponse,
} from './shared/messages';
import {
  chromeStorage,
  loadSettings,
  normalizeBackendUrl,
  normalizeUsername,
  saveSettings,
  SettingsStorage,
} from './shared/settings';

export interface BackgroundDependencies {
  readonly storage?: SettingsStorage;
  readonly fetchFn?: typeof fetch;
}

export async function handleBackgroundMessage(
  message: BackgroundMessage,
  dependencies: BackgroundDependencies = {}
): Promise<BackgroundResponse> {
  const storage = dependencies.storage ?? chromeStorage();
  const fetchFn = dependencies.fetchFn ?? fetch;
  try {
    if (message.type === 'PAIR') {
      const backendUrl = normalizeBackendUrl(message.backendUrl);
      const username = normalizeUsername(message.username);
      const paired = await new ExtensionApi(backendUrl, '', fetchFn).pair(
        username
      );
      await saveSettings(
        { backendUrl, username, token: paired.token },
        storage
      );
      return { ok: true, data: { tokenId: paired.tokenId } };
    }

    const settings = await loadSettings(storage);
    if (!settings.backendUrl || !settings.token) {
      throw new Error('请先在插件设置中连接 BLREC');
    }
    const api = new ExtensionApi(settings.backendUrl, settings.token, fetchFn);
    if (message.type === 'ROOM_STATUS') {
      return { ok: true, data: await api.roomStatus(message.roomId) };
    }
    if (message.type === 'COLLECT') {
      return {
        ok: true,
        data: await api.collect(message.roomId, message.upload),
      };
    }
    return {
      ok: true,
      data: await api.addHighlight(message.roomId, {
        observedAtMs: message.observedAtMs,
        playerDelayMs: message.playerDelayMs,
        title: message.title,
        anchorName: message.anchorName,
      }),
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : 'BLREC 请求失败',
    };
  }
}

if (typeof chrome !== 'undefined' && chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    void handleBackgroundMessage(message as BackgroundMessage).then(sendResponse);
    return true;
  });
}
