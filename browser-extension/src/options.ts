import { BackgroundResponse } from './shared/messages';
import {
  loadSettings,
  normalizeBackendUrl,
  normalizeUsername,
} from './shared/settings';

const form = document.querySelector<HTMLFormElement>('#connection-form');
const backendInput = document.querySelector<HTMLInputElement>('#backend-url');
const usernameInput = document.querySelector<HTMLInputElement>('#username');
const statusElement = document.querySelector<HTMLElement>('#connection-status');
const submitButton = document.querySelector<HTMLButtonElement>('#connect');

function setStatus(message: string, state: 'idle' | 'success' | 'error'): void {
  if (!statusElement) {
    return;
  }
  statusElement.textContent = message;
  statusElement.dataset['state'] = state;
}

async function initialize(): Promise<void> {
  const settings = await loadSettings();
  if (backendInput) {
    backendInput.value = settings.backendUrl;
  }
  if (usernameInput) {
    usernameInput.value = settings.username;
  }
  if (settings.token) {
    setStatus('已连接 BLREC', 'success');
  }
}

form?.addEventListener('submit', (event) => {
  event.preventDefault();
  void connect();
});

async function connect(): Promise<void> {
  if (!backendInput || !usernameInput) {
    return;
  }
  submitButton?.setAttribute('disabled', '');
  setStatus('正在连接…', 'idle');
  try {
    const backendUrl = normalizeBackendUrl(backendInput.value);
    const username = normalizeUsername(usernameInput.value);
    const origin = `${new URL(backendUrl).origin}/*`;
    const granted = await chrome.permissions.request({ origins: [origin] });
    if (!granted) {
      throw new Error('需要允许访问该 BLREC 地址');
    }
    const response = (await chrome.runtime.sendMessage({
      type: 'PAIR',
      backendUrl,
      username,
    })) as BackgroundResponse<{ tokenId: number }>;
    if (!response.ok) {
      throw new Error(response.message);
    }
    backendInput.value = backendUrl;
    usernameInput.value = username;
    setStatus('连接成功', 'success');
  } catch (error) {
    setStatus(error instanceof Error ? error.message : '连接失败', 'error');
  } finally {
    submitButton?.removeAttribute('disabled');
  }
}

void initialize().catch((error: unknown) => {
  setStatus(error instanceof Error ? error.message : '读取设置失败', 'error');
});
