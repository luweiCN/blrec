export interface PairResult {
  readonly tokenId: number;
  readonly token: string;
}

export interface RoomStatus {
  readonly collected: boolean;
  readonly recording: boolean;
}

export interface CollectResult {
  readonly roomId: number;
  readonly collected: true;
  readonly upload: boolean;
}

export interface HighlightResult {
  readonly id: number;
  readonly name: string;
}

export class ExtensionApi {
  constructor(
    private readonly backendUrl: string,
    private readonly token: string,
    private readonly fetchFn: typeof fetch = fetch
  ) {}

  pair(username: string): Promise<PairResult> {
    return this.request<PairResult>('/pair', {
      method: 'POST',
      body: { username },
      authenticated: false,
    });
  }

  roomStatus(roomId: number): Promise<RoomStatus> {
    return this.request<RoomStatus>(`/rooms/${roomId}`);
  }

  collect(roomId: number, upload: boolean): Promise<CollectResult> {
    return this.request<CollectResult>(`/rooms/${roomId}/collect`, {
      method: 'POST',
      body: { upload },
    });
  }

  addHighlight(
    roomId: number,
    payload: {
      readonly observedAtMs: number;
      readonly playerDelayMs: number;
      readonly currentTimeMs: number | null;
      readonly seekableEndMs: number | null;
      readonly rawDelayMs: number;
      readonly baselineDelayMs: number;
      readonly effectiveRewindMs: number;
      readonly name: string;
      readonly title: string;
      readonly anchorName: string;
    }
  ): Promise<HighlightResult> {
    return this.request<HighlightResult>(`/rooms/${roomId}/highlights`, {
      method: 'POST',
      body: payload,
    });
  }

  private async request<T>(
    path: string,
    options: {
      readonly method?: 'GET' | 'POST';
      readonly body?: unknown;
      readonly authenticated?: boolean;
    } = {}
  ): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);
    const authenticated = options.authenticated !== false;
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    if (authenticated) {
      if (!this.token) {
        throw new Error('浏览器插件尚未连接 BLREC');
      }
      headers['X-BLREC-Extension-Token'] = this.token;
    }
    const url = `${this.backendUrl}/api/v1/browser-extension${path}`;
    try {
      const response = await this.fetchFn.call(globalThis, url, {
        method: options.method ?? 'GET',
        headers,
        body:
          options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: 'omit',
        redirect: 'error',
        signal: controller.signal,
      });
      if (response.url && new URL(response.url).origin !== new URL(url).origin) {
        throw new Error('BLREC 请求被重定向到其他站点');
      }
      const document = await this.readDocument(response);
      if (!response.ok) {
        const detail =
          typeof document === 'object' &&
          document !== null &&
          'detail' in document &&
          typeof document.detail === 'string'
            ? document.detail
            : `BLREC 请求失败（HTTP ${response.status}）`;
        throw new Error(detail);
      }
      return document as T;
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new Error('连接 BLREC 超时');
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  private async readDocument(response: Response): Promise<unknown> {
    const text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text) as unknown;
    } catch (_error) {
      throw new Error('BLREC 返回了无法识别的响应');
    }
  }
}
