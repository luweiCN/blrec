import {
  BackgroundMessage,
  BackgroundResponse,
} from './shared/messages';
import { PlayerDelayCalibrator } from './shared/player';
import { parseRoomId } from './shared/room';

interface ObserverLike {
  observe(target: Node, options?: MutationObserverInit): void;
  disconnect(): void;
}

interface ContentDependencies {
  readonly document: Document;
  readonly location: Location;
  readonly sendMessage: (
    message: BackgroundMessage
  ) => Promise<BackgroundResponse>;
  readonly now?: () => number;
  readonly createObserver?: (callback: MutationCallback) => ObserverLike;
  readonly scheduleRefresh?: (
    callback: () => Promise<void>
  ) => () => void;
}

interface RoomStatus {
  readonly collected: boolean;
  readonly recording: boolean;
}

export class HighlightContentController {
  private readonly document: Document;
  private readonly location: Location;
  private readonly sendMessage: ContentDependencies['sendMessage'];
  private readonly now: () => number;
  private readonly createObserver: (callback: MutationCallback) => ObserverLike;
  private readonly scheduleRefresh: (
    callback: () => Promise<void>
  ) => () => void;
  private roomId: number | null = null;
  private status: RoomStatus | null = null;
  private observer: ObserverLike | null = null;
  private cancelStatusRefresh: (() => void) | null = null;
  private cancelNamePrompt: (() => void) | null = null;
  private readonly playerDelay = new PlayerDelayCalibrator();
  private started = false;

  constructor(dependencies: ContentDependencies) {
    this.document = dependencies.document;
    this.location = dependencies.location;
    this.sendMessage = dependencies.sendMessage;
    this.now = dependencies.now ?? Date.now;
    this.createObserver =
      dependencies.createObserver ??
      ((callback) => new MutationObserver(callback));
    this.scheduleRefresh =
      dependencies.scheduleRefresh ??
      ((callback) => {
        const timer = setInterval(() => void callback(), 30_000);
        return () => clearInterval(timer);
      });
  }

  async start(): Promise<void> {
    if (this.started) {
      return;
    }
    this.started = true;
    this.roomId = parseRoomId(this.location, this.document);
    if (this.roomId === null) {
      return;
    }
    await this.refreshStatus();
    this.observer = this.createObserver(() => this.ensureRendered());
    if (this.document.body) {
      this.observer.observe(this.document.body, {
        childList: true,
        subtree: true,
      });
    }
    this.cancelStatusRefresh = this.scheduleRefresh(() => this.refreshStatus());
  }

  destroy(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.cancelStatusRefresh?.();
    this.cancelStatusRefresh = null;
    this.cancelNamePrompt?.();
    this.cancelNamePrompt = null;
    this.document
      .querySelectorAll(
        '.blrec-highlight-actions, .blrec-highlight-toast, .blrec-highlight-popover',
      )
      .forEach((element) => element.remove());
  }

  private async refreshStatus(): Promise<void> {
    if (this.roomId === null) {
      return;
    }
    const response = await this.sendMessage({
      type: 'ROOM_STATUS',
      roomId: this.roomId,
    });
    if (!response.ok || !this.isRoomStatus(response.data)) {
      this.status = null;
      this.removeActions();
      this.toast(response.ok ? '无法识别房间状态' : response.message, 'error');
      return;
    }
    this.status = response.data;
    if (this.status.recording) {
      this.playerDelay.sample(this.document);
    }
    this.removeActions();
    this.ensureRendered();
  }

  private ensureRendered(): void {
    if (!this.status || !this.needsActions(this.status)) {
      this.removeActions();
      return;
    }
    const existing = this.document.querySelectorAll('.blrec-highlight-actions');
    if (existing.length > 0) {
      Array.from(existing)
        .slice(1)
        .forEach((element) => element.remove());
      return;
    }
    const container = this.document.createElement('div');
    container.className = 'blrec-highlight-actions';
    container.setAttribute('aria-label', 'BLREC 直播操作');
    if (this.status.recording) {
      container.append(
        this.actionButton('添加高光', () => this.addHighlight(container))
      );
    } else {
      container.append(
        this.actionButton('收录', () => this.collect(container, false)),
        this.actionButton('收录并投稿', () => this.collect(container, true))
      );
    }
    const anchor = this.document.querySelector(
      '.right-ctnr, .upper-row .right-ctnr, #head-info-vm'
    );
    if (anchor) {
      anchor.append(container);
    } else if (this.document.body) {
      container.classList.add('blrec-highlight-actions--fallback');
      this.document.body.append(container);
    }
  }

  private actionButton(
    label: string,
    action: () => Promise<void>,
  ): HTMLButtonElement {
    const button = this.document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.addEventListener('click', () => void action());
    return button;
  }

  private async collect(container: HTMLElement, upload: boolean): Promise<void> {
    if (this.roomId === null) {
      return;
    }
    this.setDisabled(container, true);
    const response = await this.sendMessage({
      type: 'COLLECT',
      roomId: this.roomId,
      upload,
    });
    if (!response.ok) {
      this.toast(this.friendlyError(response.message), 'error');
      this.setDisabled(container, false);
      return;
    }
    if (this.isCollectResult(response.data)) {
      this.roomId = response.data.roomId;
    }
    this.toast(upload ? '已收录并开启投稿' : '已收录', 'success');
    await this.refreshStatus();
  }

  private async addHighlight(container: HTMLElement): Promise<void> {
    if (this.roomId === null) {
      return;
    }
    this.setDisabled(container, true);
    const observation = this.playerDelay.observe(this.document, this.now());
    const name = await this.requestHighlightName(container);
    if (name === null) {
      this.setDisabled(container, false);
      return;
    }
    const response = await this.sendMessage({
      type: 'ADD_HIGHLIGHT',
      roomId: this.roomId,
      observedAtMs: observation.observedAtMs,
      playerDelayMs: Math.min(300_000, observation.effectiveRewindMs),
      currentTimeMs: observation.currentTimeMs,
      seekableEndMs: observation.seekableEndMs,
      rawDelayMs: observation.rawDelayMs,
      baselineDelayMs: observation.baselineDelayMs,
      effectiveRewindMs: observation.effectiveRewindMs,
      name,
      title: this.document.title,
      anchorName: this.anchorName(),
    });
    this.toast(
      response.ok ? '高光点已保存' : this.friendlyError(response.message),
      response.ok ? 'success' : 'error'
    );
    this.setDisabled(container, false);
  }

  private requestHighlightName(container: HTMLElement): Promise<string | null> {
    this.cancelNamePrompt?.();
    return new Promise((resolve) => {
      const popover = this.document.createElement('form');
      popover.className = 'blrec-highlight-popover';
      popover.autocomplete = 'off';
      popover.setAttribute('aria-label', '高光名称');
      const input = this.document.createElement('input');
      input.type = 'text';
      input.name = 'blrec-highlight-name';
      input.autocomplete = 'off';
      input.maxLength = 200;
      input.placeholder = '高光名称（可不填）';
      input.setAttribute('aria-label', '高光名称');
      input.setAttribute('data-1p-ignore', 'true');
      input.setAttribute('data-op-ignore', 'true');
      const actions = this.document.createElement('div');
      const cancel = this.document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = '取消';
      cancel.dataset['action'] = 'cancel-highlight';
      const save = this.document.createElement('button');
      save.type = 'submit';
      save.textContent = '保存';
      save.dataset['action'] = 'save-highlight';
      actions.append(cancel, save);
      popover.append(input, actions);

      let settled = false;
      const finish = (value: string | null) => {
        if (settled) {
          return;
        }
        settled = true;
        popover.remove();
        this.cancelNamePrompt = null;
        resolve(value);
      };
      this.cancelNamePrompt = () => finish(null);
      cancel.addEventListener('click', () => finish(null));
      popover.addEventListener('submit', (event) => {
        event.preventDefault();
        finish(input.value.trim().slice(0, 200));
      });
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          finish(null);
        }
      });
      container.append(popover);
      input.focus();
    });
  }

  private anchorName(): string {
    const element = this.document.querySelector(
      '.room-owner-username, .anchor-name, [data-testid="anchor-name"]'
    );
    return element?.textContent?.trim().slice(0, 100) ?? '';
  }

  private setDisabled(container: HTMLElement, disabled: boolean): void {
    container
      .querySelectorAll<HTMLButtonElement>('button')
      .forEach((button) => (button.disabled = disabled));
  }

  private toast(message: string, state: 'success' | 'error'): void {
    this.document.querySelector('.blrec-highlight-toast')?.remove();
    const toast = this.document.createElement('div');
    toast.className = 'blrec-highlight-toast';
    toast.dataset['state'] = state;
    toast.setAttribute('role', 'status');
    toast.textContent = message;
    this.document.body?.append(toast);
    setTimeout(() => toast.remove(), 2800);
  }

  private friendlyError(message: string): string {
    return message.includes('授权')
      ? '请在插件设置中重新连接 BLREC'
      : message;
  }

  private needsActions(status: RoomStatus): boolean {
    return !status.collected || status.recording;
  }

  private removeActions(): void {
    this.cancelNamePrompt?.();
    this.cancelNamePrompt = null;
    this.document
      .querySelectorAll('.blrec-highlight-actions')
      .forEach((element) => element.remove());
  }

  private isRoomStatus(value: unknown): value is RoomStatus {
    return (
      typeof value === 'object' &&
      value !== null &&
      'collected' in value &&
      typeof value.collected === 'boolean' &&
      'recording' in value &&
      typeof value.recording === 'boolean'
    );
  }

  private isCollectResult(value: unknown): value is { roomId: number } {
    return (
      typeof value === 'object' &&
      value !== null &&
      'roomId' in value &&
      typeof value.roomId === 'number' &&
      Number.isSafeInteger(value.roomId) &&
      value.roomId > 0
    );
  }
}

interface ContentWindow extends Window {
  __blrecHighlightController?: HighlightContentController;
}

if (
  typeof chrome !== 'undefined' &&
  chrome.runtime?.sendMessage &&
  location.hostname === 'live.bilibili.com'
) {
  const contentWindow = window as ContentWindow;
  if (!contentWindow.__blrecHighlightController) {
    const controller = new HighlightContentController({
      document,
      location,
      sendMessage: (message) => chrome.runtime.sendMessage(message),
    });
    contentWindow.__blrecHighlightController = controller;
    void controller.start();
  }
}
