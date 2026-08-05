import {
  BackgroundMessage,
  BackgroundResponse,
} from './shared/messages';

interface VideoContentDependencies {
  readonly document: Document;
  readonly location: Location;
  readonly sendMessage: (
    message: BackgroundMessage
  ) => Promise<BackgroundResponse>;
  readonly scheduleRefresh?: (callback: () => void) => () => void;
}

interface VideoIdentity {
  readonly bvid: string;
  readonly page: number;
}

export class MatchMarkerContentController {
  private readonly document: Document;
  private readonly location: Location;
  private readonly sendMessage: VideoContentDependencies['sendMessage'];
  private readonly scheduleRefresh: (callback: () => void) => () => void;
  private cancelRefresh: (() => void) | null = null;
  private identityKey = '';
  private indexed = false;
  private saving = false;

  constructor(dependencies: VideoContentDependencies) {
    this.document = dependencies.document;
    this.location = dependencies.location;
    this.sendMessage = dependencies.sendMessage;
    this.scheduleRefresh =
      dependencies.scheduleRefresh ??
      ((callback) => {
        const timer = setInterval(callback, 2_000);
        return () => clearInterval(timer);
      });
  }

  start(): void {
    this.refreshIdentity();
    this.cancelRefresh = this.scheduleRefresh(() => this.refreshIdentity());
  }

  destroy(): void {
    this.cancelRefresh?.();
    this.cancelRefresh = null;
    this.removeActions();
  }

  private refreshIdentity(): void {
    const identity = this.videoIdentity();
    const key = identity === null ? '' : `${identity.bvid}:${identity.page}`;
    if (key === this.identityKey) {
      if (this.indexed) {
        this.ensureRendered(identity);
      }
      return;
    }
    this.identityKey = key;
    this.indexed = false;
    this.removeActions();
    if (identity !== null) {
      void this.loadStatus(identity);
    }
  }

  private async loadStatus(identity: VideoIdentity): Promise<void> {
    const expectedKey = `${identity.bvid}:${identity.page}`;
    const response = await this.sendMessage({
      type: 'VIDEO_STATUS',
      bvid: identity.bvid,
      page: identity.page,
    });
    if (this.identityKey !== expectedKey) {
      return;
    }
    this.indexed =
      response.ok &&
      typeof response.data === 'object' &&
      response.data !== null &&
      'indexed' in response.data &&
      response.data.indexed === true;
    if (this.indexed) {
      this.ensureRendered(identity);
    }
  }

  private ensureRendered(identity: VideoIdentity | null): void {
    if (identity === null || !this.indexed) {
      this.removeActions();
      return;
    }
    let container = this.document.querySelector<HTMLElement>(
      '.blrec-match-marker-actions'
    );
    if (container !== null) {
      return;
    }
    container = this.document.createElement('div');
    container.className = 'blrec-match-marker-actions';
    container.setAttribute('aria-label', 'BLREC 对局标记');
    const button = this.document.createElement('button');
    button.type = 'button';
    button.textContent = '标记对局';
    button.addEventListener('click', () => void this.mark(identity, button));
    container.append(button);
    this.document.body?.append(container);
  }

  private async mark(
    identity: VideoIdentity,
    button: HTMLButtonElement
  ): Promise<void> {
    if (this.saving) {
      return;
    }
    const video = this.document.querySelector<HTMLVideoElement>('video');
    if (video === null || !Number.isFinite(video.currentTime)) {
      this.toast('暂时读不到播放时间', 'error');
      return;
    }
    this.saving = true;
    button.disabled = true;
    const response = await this.sendMessage({
      type: 'MARK_MATCH',
      bvid: identity.bvid,
      page: identity.page,
      currentTimeMs: Math.max(0, Math.round(video.currentTime * 1_000)),
    });
    this.toast(
      response.ok
        ? '已标记，该分 P 已加入重新识别队列'
        : response.message,
      response.ok ? 'success' : 'error'
    );
    this.saving = false;
    button.disabled = false;
  }

  private videoIdentity(): VideoIdentity | null {
    const match = this.location.pathname.match(/\/video\/(BV[0-9A-Za-z]+)/i);
    if (match === null) {
      return null;
    }
    const pageValue = new URLSearchParams(this.location.search).get('p');
    const parsedPage = pageValue === null ? 1 : Number.parseInt(pageValue, 10);
    return {
      bvid: match[1],
      page: Number.isSafeInteger(parsedPage) && parsedPage > 0 ? parsedPage : 1,
    };
  }

  private toast(message: string, state: 'success' | 'error'): void {
    this.document.querySelector('.blrec-highlight-toast')?.remove();
    const toast = this.document.createElement('div');
    toast.className = 'blrec-highlight-toast';
    toast.dataset['state'] = state;
    toast.setAttribute('role', 'status');
    toast.textContent = message;
    this.document.body?.append(toast);
    setTimeout(() => toast.remove(), 3_500);
  }

  private removeActions(): void {
    this.document.querySelector('.blrec-match-marker-actions')?.remove();
  }
}

interface VideoContentWindow extends Window {
  __blrecMatchMarkerController?: MatchMarkerContentController;
}

if (
  typeof chrome !== 'undefined' &&
  chrome.runtime?.sendMessage &&
  location.hostname === 'www.bilibili.com'
) {
  const contentWindow = window as VideoContentWindow;
  if (!contentWindow.__blrecMatchMarkerController) {
    const controller = new MatchMarkerContentController({
      document,
      location,
      sendMessage: (message) => chrome.runtime.sendMessage(message),
    });
    contentWindow.__blrecMatchMarkerController = controller;
    controller.start();
  }
}
