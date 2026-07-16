import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  NgZone,
  OnDestroy,
  OnInit,
  ViewChild,
} from '@angular/core';
import { ActivatedRoute } from '@angular/router';

import { Subscription } from 'rxjs';

import {
  RealtimeEvent,
  RealtimeService,
} from 'src/app/core/services/realtime.service';
import {
  PartPlayer,
  PartPlayerFactory,
} from '../part-video-dialog/part-player.factory';
import {
  HighlightClip,
  HighlightClipInspection,
  HighlightProgressEvent,
  HighlightTimeline,
  HighlightTimelinePart,
  MappedHighlight,
} from '../shared/highlight.model';
import { HighlightService } from '../shared/highlight.service';
import { RecordingMediaAccess } from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';

@Component({
  selector: 'app-highlight-editor',
  templateUrl: './highlight-editor.component.html',
  styleUrls: ['./highlight-editor.component.scss'],
})
export class HighlightEditorComponent implements OnInit, OnDestroy {
  readonly sessionId: number;
  readonly initialPartId: number | null;

  timeline: HighlightTimeline | null = null;
  selectedPart: HighlightTimelinePart | null = null;
  selectedMarkerId: number | null = null;
  playheadMs = 0;
  startMs = 0;
  endMs = 0;
  clipName = '';
  inspection: HighlightClipInspection | null = null;
  confirmKeyframe = false;
  clips: HighlightClip[] = [];
  uploadJobIds = new Map<number, number>();
  taskEditVisible = false;
  taskEditJobIds: readonly number[] = [];
  clipPreviewId: number | null = null;
  clipPreviewUrl: string | null = null;
  clipPreviewLoading = false;

  loading = true;
  mediaLoading = false;
  inspecting = false;
  creating = false;
  error: string | null = null;
  mediaError: string | null = null;
  actionError: string | null = null;

  editingMarkerId: number | null = null;
  markerName = '';
  markerNote = '';

  mediaUrl: string | null = null;
  mediaAccess: RecordingMediaAccess | null = null;

  private videoElement: HTMLVideoElement | null = null;
  private player: PartPlayer | null = null;
  private pendingSeekSeconds: number | null = null;
  private mediaRequest?: Subscription;
  private readonly subscriptions = new Subscription();

  constructor(
    route: ActivatedRoute,
    private highlights: HighlightService,
    private recordings: RecordingSessionService,
    private playerFactory: PartPlayerFactory,
    private changeDetector: ChangeDetectorRef,
    private zone: NgZone,
    realtime: RealtimeService
  ) {
    this.sessionId = Number(route.snapshot.paramMap.get('sessionId'));
    const partId = Number(route.snapshot.queryParamMap?.get('partId'));
    this.initialPartId = Number.isInteger(partId) && partId > 0 ? partId : null;
    this.subscriptions.add(
      realtime.events$.subscribe((event) => this.handleRealtimeEvent(event))
    );
  }

  @ViewChild('videoElement')
  set videoElementRef(value: ElementRef<HTMLVideoElement> | undefined) {
    this.videoElement = value?.nativeElement ?? null;
    if (this.videoElement === null) {
      this.teardownPlayer();
      return;
    }
    this.attachFlvPlayer();
    this.applyPendingSeek();
  }

  get clip(): HighlightClip | null {
    return this.clips.length > 0 ? this.clips[this.clips.length - 1] : null;
  }

  get selectionStartSeconds(): number {
    return this.startMs / 1000;
  }

  set selectionStartSeconds(value: number) {
    this.startMs = Math.round(Number(value) * 1000);
    this.selectionChanged();
  }

  get selectionEndSeconds(): number {
    return this.endMs / 1000;
  }

  set selectionEndSeconds(value: number) {
    this.endMs = Math.round(Number(value) * 1000);
    this.selectionChanged();
  }

  get selectionError(): string | null {
    if (!this.timeline) {
      return null;
    }
    if (this.startMs < 0 || this.endMs > this.timeline.durationMs) {
      return '裁剪范围超出录像时长';
    }
    if (this.endMs <= this.startMs) {
      return '结束位置必须晚于开始位置';
    }
    if (this.endMs > this.timeline.stableEndMs) {
      return '结束位置仍在录制安全区之外，请稍后刷新';
    }
    return null;
  }

  get canInspect(): boolean {
    return this.selectionError === null && !this.inspecting && !this.creating;
  }

  get canCreate(): boolean {
    return (
      this.inspection !== null &&
      this.inspection.compatible &&
      (!this.inspection.confirmationRequired || this.confirmKeyframe) &&
      !this.creating
    );
  }

  ngOnInit(): void {
    if (!Number.isInteger(this.sessionId) || this.sessionId <= 0) {
      this.loading = false;
      this.error = '录像场次编号无效';
      return;
    }
    this.loadTimeline(true);
  }

  ngOnDestroy(): void {
    this.mediaRequest?.unsubscribe();
    this.subscriptions.unsubscribe();
    this.teardownPlayer();
  }

  refreshTimeline(): void {
    this.loadTimeline(false);
  }

  selectMarker(item: MappedHighlight): void {
    this.selectedMarkerId = item.marker.id;
    this.clipName = item.marker.name;
    this.startMs = Math.max(0, item.timelineOffsetMs - 30_000);
    this.endMs = Math.min(
      this.timeline?.stableEndMs ?? item.timelineOffsetMs + 60_000,
      item.timelineOffsetMs + 60_000
    );
    this.inspection = null;
    this.confirmKeyframe = false;
    this.seekTimeline(item.timelineOffsetMs);
  }

  selectPart(part: HighlightTimelinePart, localOffsetMs = 0): void {
    const changed = this.selectedPart?.partId !== part.partId;
    this.selectedPart = part;
    this.playheadMs = part.timelineStartMs + localOffsetMs;
    this.pendingSeekSeconds = Math.max(0, localOffsetMs / 1000);
    if (changed || this.mediaUrl === null) {
      this.loadMedia();
    } else {
      this.applyPendingSeek();
    }
  }

  selectionChanged(): void {
    this.inspection = null;
    this.confirmKeyframe = false;
    this.actionError = null;
    if (this.selectedMarkerId === null) {
      this.clipName = `高光片段 ${this.formatTime(this.startMs)}`;
    }
  }

  inspectSelection(): void {
    if (!this.canInspect) {
      return;
    }
    this.inspecting = true;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights
        .inspectClip(this.sessionId, this.startMs, this.endMs)
        .subscribe({
          next: (inspection) => {
            this.inspection = inspection;
            this.inspecting = false;
            this.changeDetector.markForCheck();
          },
          error: (error: unknown) => {
            this.inspecting = false;
            this.actionError = this.describeError(error, '无法检查裁剪范围');
            this.changeDetector.markForCheck();
          },
        })
    );
  }

  createClip(): void {
    if (!this.canCreate) {
      return;
    }
    this.creating = true;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights
        .createClip(this.sessionId, {
          markerId: this.selectedMarkerId,
          name: this.clipName.trim() || `高光片段 ${this.formatTime(this.startMs)}`,
          startMs: this.startMs,
          endMs: this.endMs,
          confirmKeyframe: this.confirmKeyframe,
        })
        .subscribe({
          next: (clip) => {
            this.clips = [...this.clips, clip];
            this.creating = false;
            this.changeDetector.markForCheck();
          },
          error: (error: unknown) => {
            this.creating = false;
            this.actionError = this.describeError(error, '创建高光片段失败');
            this.changeDetector.markForCheck();
          },
        })
    );
  }

  createUploadTask(clip: HighlightClip): void {
    this.actionError = null;
    this.subscriptions.add(
      this.highlights.createUploadTask(clip.id).subscribe({
        next: ({ jobId }) => {
          this.uploadJobIds.set(clip.id, jobId);
          this.taskEditJobIds = [jobId];
          this.taskEditVisible = true;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.describeError(error, '创建上传任务失败');
          this.changeDetector.markForCheck();
        },
      })
    );
  }

  openClipPreview(clip: HighlightClip): void {
    this.clipPreviewId = clip.id;
    this.clipPreviewUrl = null;
    this.clipPreviewLoading = true;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights.createMediaAccess(clip.id).subscribe({
        next: (access) => {
          if (this.clipPreviewId !== clip.id) {
            return;
          }
          this.clipPreviewUrl = this.highlights.mediaUrl(clip.id, access);
          this.clipPreviewLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.clipPreviewLoading = false;
          this.actionError = this.describeError(error, '打开高光片段失败');
          this.changeDetector.markForCheck();
        },
      })
    );
  }

  closeClipPreview(): void {
    this.clipPreviewId = null;
    this.clipPreviewUrl = null;
    this.clipPreviewLoading = false;
  }

  handleClipPreviewError(): void {
    this.actionError = '高光片段播放失败';
  }

  closeTaskEdit(): void {
    this.taskEditVisible = false;
    this.taskEditJobIds = [];
  }

  taskEditSaved(): void {
    const jobIds = this.taskEditJobIds;
    this.closeTaskEdit();
    if (jobIds.length === 0) {
      return;
    }
    this.subscriptions.add(
      this.recordings.runJobAction('resume_upload', jobIds).subscribe({
        next: ({ results }) => {
          const rejected = results.find((result) => !result.accepted);
          if (rejected) {
            this.actionError = rejected.message;
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.describeError(error, '继续上传任务失败');
          this.changeDetector.markForCheck();
        },
      })
    );
  }

  deleteClip(clip: HighlightClip): void {
    this.subscriptions.add(
      this.highlights.deleteClip(clip.id).subscribe({
        next: () => {
          this.clips = this.clips.filter((item) => item.id !== clip.id);
          this.uploadJobIds.delete(clip.id);
          if (this.clipPreviewId === clip.id) {
            this.closeClipPreview();
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.describeError(error, '删除高光片段失败');
          this.changeDetector.markForCheck();
        },
      })
    );
  }

  beginMarkerEdit(item: MappedHighlight): void {
    this.editingMarkerId = item.marker.id;
    this.markerName = item.marker.name;
    this.markerNote = item.marker.note;
  }

  cancelMarkerEdit(): void {
    this.editingMarkerId = null;
    this.markerName = '';
    this.markerNote = '';
  }

  saveMarker(): void {
    if (this.editingMarkerId === null || !this.markerName.trim()) {
      return;
    }
    const markerId = this.editingMarkerId;
    this.subscriptions.add(
      this.highlights
        .updateMarker(markerId, this.markerName.trim(), this.markerNote.trim())
        .subscribe({
          next: (marker) => {
            if (this.timeline) {
              this.timeline = {
                ...this.timeline,
                markers: this.timeline.markers.map((item) =>
                  item.marker.id === markerId ? { ...item, marker } : item
                ),
              };
            }
            if (this.selectedMarkerId === markerId) {
              this.clipName = marker.name;
            }
            this.cancelMarkerEdit();
            this.changeDetector.markForCheck();
          },
          error: (error: unknown) => {
            this.actionError = this.describeError(error, '保存高光点失败');
            this.changeDetector.markForCheck();
          },
        })
    );
  }

  deleteMarker(item: MappedHighlight): void {
    const markerId = item.marker.id;
    this.subscriptions.add(
      this.highlights.deleteMarker(markerId).subscribe({
        next: () => {
          if (this.timeline) {
            this.timeline = {
              ...this.timeline,
              markers: this.timeline.markers.filter(
                (value) => value.marker.id !== markerId
              ),
            };
          }
          if (this.selectedMarkerId === markerId) {
            this.selectedMarkerId = null;
          }
          if (this.editingMarkerId === markerId) {
            this.cancelMarkerEdit();
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.describeError(error, '删除高光点失败');
          this.changeDetector.markForCheck();
        },
      })
    );
  }

  handleTimeUpdate(): void {
    if (!this.videoElement || !this.selectedPart) {
      return;
    }
    this.playheadMs =
      this.selectedPart.timelineStartMs + this.videoElement.currentTime * 1000;
  }

  handleMediaError(): void {
    this.mediaError = '本地视频播放失败，请刷新后重试';
  }

  handleMediaStalled(): void {
    this.mediaError = '本地视频加载停滞，请检查连接后重试';
  }

  formatTime(valueMs: number | null): string {
    if (valueMs === null || !Number.isFinite(valueMs)) {
      return '--:--';
    }
    const totalSeconds = Math.max(0, Math.floor(valueMs / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const base = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(
      2,
      '0'
    )}`;
    return hours > 0 ? `${String(hours).padStart(2, '0')}:${base}` : base;
  }

  positionPercent(valueMs: number): number {
    if (!this.timeline || this.timeline.durationMs <= 0) {
      return 0;
    }
    return Math.max(0, Math.min(100, (valueMs / this.timeline.durationMs) * 100));
  }

  partWidthPercent(part: HighlightTimelinePart): number {
    return this.positionPercent(part.durationMs);
  }

  trackClip(_index: number, clip: HighlightClip): number {
    return clip.id;
  }

  trackMarker(_index: number, item: MappedHighlight): number {
    return item.marker.id;
  }

  trackPart(_index: number, part: HighlightTimelinePart): number {
    return part.partId;
  }

  private loadTimeline(initial: boolean): void {
    this.loading = true;
    this.error = null;
    this.subscriptions.add(
      this.highlights.getTimeline(this.sessionId).subscribe({
        next: (timeline) => {
          this.timeline = timeline;
          this.loading = false;
          const playhead = Math.min(this.playheadMs, timeline.stableEndMs);
          const requestedPart = initial
            ? timeline.parts.find((part) => part.partId === this.initialPartId)
            : undefined;
          const part =
            requestedPart ?? this.partAt(playhead) ?? timeline.parts[0] ?? null;
          if (part) {
            const localOffset = requestedPart
              ? 0
              : Math.max(0, playhead - part.timelineStartMs);
            this.selectPart(part, localOffset);
            if (initial) {
              this.startMs = part.timelineStartMs;
              this.endMs = Math.min(
                part.timelineStartMs + 60_000,
                part.stableEndMs,
                timeline.stableEndMs
              );
              this.clipName = `高光片段 ${this.formatTime(this.startMs)}`;
            }
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.loading = false;
          this.error = this.describeError(error, '无法加载高光剪辑时间轴');
          this.changeDetector.markForCheck();
        },
      })
    );
  }

  private seekTimeline(valueMs: number): void {
    const part = this.partAt(valueMs);
    if (!part) {
      return;
    }
    this.selectPart(part, valueMs - part.timelineStartMs);
  }

  private partAt(valueMs: number): HighlightTimelinePart | null {
    return (
      this.timeline?.parts.find(
        (part) =>
          valueMs >= part.timelineStartMs &&
          valueMs <= part.timelineStartMs + part.durationMs
      ) ?? null
    );
  }

  private loadMedia(): void {
    if (!this.selectedPart) {
      return;
    }
    const part = this.selectedPart;
    this.mediaRequest?.unsubscribe();
    this.teardownPlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.mediaError = null;
    this.mediaLoading = true;
    this.mediaRequest = this.recordings.createMediaAccess(part.partId).subscribe({
      next: (access) => {
        if (this.selectedPart?.partId !== part.partId) {
          return;
        }
        this.mediaAccess = access;
        this.mediaUrl = this.recordings.mediaUrl(part.partId, access);
        this.mediaLoading = false;
        this.attachFlvPlayer();
        this.applyPendingSeek();
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.mediaLoading = false;
        this.mediaError = this.describeError(error, '无法打开本地视频');
        this.changeDetector.markForCheck();
      },
    });
  }

  private attachFlvPlayer(): void {
    if (
      this.selectedPart?.mediaKind !== 'flv' ||
      !this.videoElement ||
      !this.mediaUrl ||
      this.player
    ) {
      return;
    }
    this.player = this.playerFactory.attachFlv(
      this.videoElement,
      this.mediaUrl,
      {
        isLive: false,
        durationMs: this.mediaAccess?.durationMs ?? null,
        fileSizeBytes: this.mediaAccess?.fileSizeBytes ?? null,
      },
      (message) => {
        this.zone.run(() => {
          this.mediaError = message;
          this.teardownPlayer();
          this.changeDetector.markForCheck();
        });
      }
    );
    if (this.player === null) {
      this.mediaError = '当前浏览器不支持 FLV 播放';
    }
  }

  private applyPendingSeek(): void {
    if (!this.videoElement || this.pendingSeekSeconds === null) {
      return;
    }
    try {
      this.videoElement.currentTime = this.pendingSeekSeconds;
      this.pendingSeekSeconds = null;
    } catch (_error) {
      // Metadata may not be ready yet; loadedmetadata will retry.
    }
  }

  private teardownPlayer(): void {
    if (!this.player) {
      return;
    }
    this.player.pause();
    this.player.unload();
    this.player.detachMediaElement();
    this.player.destroy();
    this.player = null;
  }

  private handleRealtimeEvent(event: RealtimeEvent): void {
    if (event.type === 'resync') {
      this.refreshTimeline();
      return;
    }
    if (event.type !== 'highlight_progress') {
      return;
    }
    const progress = this.parseProgress(event.data);
    for (const item of progress?.clips ?? []) {
      const index = this.clips.findIndex((clip) => clip.id === item.id);
      if (index < 0) {
        continue;
      }
      const previous = this.clips[index];
      this.clips = this.clips.map((clip) =>
        clip.id === item.id ? { ...clip, ...item } : clip
      );
      this.changeDetector.markForCheck();
      if (item.state === 'ready' && previous.state !== 'ready') {
        this.subscriptions.add(
          this.highlights.getClip(item.id).subscribe((clip) => {
            this.clips = this.clips.map((value) =>
              value.id === clip.id ? clip : value
            );
            this.changeDetector.markForCheck();
          })
        );
      }
    }
  }

  private parseProgress(value: unknown): HighlightProgressEvent | null {
    if (typeof value !== 'object' || value === null || !('clips' in value)) {
      return null;
    }
    const clips = (value as { clips?: unknown }).clips;
    return Array.isArray(clips) ? (value as HighlightProgressEvent) : null;
  }

  private describeError(error: unknown, fallback: string): string {
    if (typeof error === 'object' && error !== null && 'error' in error) {
      const detail = (error as { error?: { detail?: unknown } }).error?.detail;
      if (typeof detail === 'string') {
        return detail;
      }
    }
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
