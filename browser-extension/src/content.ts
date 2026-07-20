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
  readonly waitForOperationPoll?: () => Promise<void>;
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
  private readonly waitForOperationPoll: () => Promise<void>;
  private roomId: number | null = null;
  private status: RoomStatus | null = null;
  private observer: ObserverLike | null = null;
  private cancelStatusRefresh: (() => void) | null = null;
  private cancelNamePrompt: (() => void) | null = null;
  private readonly playerDelay = new PlayerDelayCalibrator();
  private started = false;
  private membershipGeneration = 0;

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
    this.waitForOperationPoll =
      dependencies.waitForOperationPoll ??
      (() => new Promise((resolve) => setTimeout(resolve, 500)));
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
    this.started = false;
    this.membershipGeneration += 1;
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
    if (!this.isCollectAdmission(response.data)) {
      this.toast('BLREC 返回了无法识别的收录任务', 'error');
      this.setDisabled(container, false);
      return;
    }
    const generation = ++this.membershipGeneration;
    this.toast('收录任务已提交', 'success');
    await this.pollMembership(
      container,
      response.data.operationId,
      upload,
      generation
    );
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

  private isCollectAdmission(
    value: unknown
  ): value is { operationId: string; requestedRoomId: number } {
    return (
      typeof value === 'object' &&
      value !== null &&
      'operationId' in value &&
      typeof value.operationId === 'string' &&
      value.operationId.length > 0 &&
      'requestedRoomId' in value &&
      typeof value.requestedRoomId === 'number'
    );
  }

  private async pollMembership(
    container: HTMLElement,
    operationId: string,
    upload: boolean,
    generation: number
  ): Promise<void> {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await this.waitForOperationPoll();
      if (!this.started || generation !== this.membershipGeneration) {
        return;
      }
      const response = await this.sendMessage({
        type: 'CONTROL_OPERATION',
        operationId,
      });
      if (!this.started || generation !== this.membershipGeneration) {
        return;
      }
      if (!response.ok) {
        this.toast(this.friendlyError(response.message), 'error');
        this.setDisabled(container, false);
        return;
      }
      const operation = this.controlOperation(response.data);
      if (!operation) {
        this.toast('BLREC 返回了无法识别的任务状态', 'error');
        this.setDisabled(container, false);
        return;
      }
      if (operation.status === 'accepted' || operation.status === 'running') {
        continue;
      }
      if (operation.status === 'failed') {
        this.toast(this.membershipError(operation.errorCode), 'error');
        this.setDisabled(container, false);
        return;
      }
      const result = operation.result;
      if (
        !result ||
        typeof result.resolvedRoomId !== 'number' ||
        !Number.isSafeInteger(result.resolvedRoomId) ||
        result.resolvedRoomId <= 0 ||
        result.collected !== true ||
        typeof result.upload !== 'boolean'
      ) {
        this.toast('BLREC 返回的收录结果不完整', 'error');
        this.setDisabled(container, false);
        return;
      }
      if (result.upload !== upload) {
        this.toast('BLREC 返回的投稿设置与请求不一致', 'error');
        this.setDisabled(container, false);
        return;
      }
      this.roomId = result.resolvedRoomId;
      this.toast(result.upload ? '已收录并开启投稿' : '已收录', 'success');
      await this.refreshStatus();
      return;
    }
    this.toast('收录任务查询超时，请稍后刷新页面确认', 'error');
    this.setDisabled(container, false);
  }

  private controlOperation(value: unknown): {
    status: 'accepted' | 'running' | 'succeeded' | 'failed';
    result: {
      resolvedRoomId?: number;
      collected?: boolean;
      upload?: boolean;
    } | null;
    errorCode: string | null;
  } | null {
    if (
      typeof value !== 'object' ||
      value === null ||
      !('status' in value) ||
      !['accepted', 'running', 'succeeded', 'failed'].includes(
        String(value.status)
      )
    ) {
      return null;
    }
    const status = value.status as
      | 'accepted'
      | 'running'
      | 'succeeded'
      | 'failed';
    const result =
      'result' in value && typeof value.result === 'object'
        ? (value.result as {
            resolvedRoomId?: number;
            collected?: boolean;
            upload?: boolean;
          } | null)
        : null;
    const errorCode =
      'errorCode' in value && typeof value.errorCode === 'string'
        ? value.errorCode
        : null;
    return { status, result, errorCode };
  }

  private membershipError(errorCode: string | null): string {
    const messages: Readonly<Record<string, string>> = {
      ROOM_RESOLVE_FAILED: '直播间编号解析失败',
      TASK_ADD_FAILED: '添加录制任务失败',
      TASK_STATE_FAILED: '启动录制任务失败',
      UPLOAD_POLICY_FAILED: '启用投稿设置失败',
      SETTINGS_PERSIST_FAILED: '保存任务设置失败',
      TASK_TEARDOWN_FAILED: '删除录制任务失败',
      DEPENDENCY_FAILED: '前置步骤失败',
    };
    return errorCode && messages[errorCode]
      ? messages[errorCode]
      : errorCode
      ? `收录失败（${errorCode}）`
      : '收录失败';
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
