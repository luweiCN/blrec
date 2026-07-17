import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  Inject,
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
import { RoomUploadPolicyRequest } from '../../tasks/upload-policy-dialog/room-upload-policy.model';
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

interface HighlightClipDraft {
  readonly id: number;
  markerId: number | null;
  name: string;
  startMs: number;
  endMs: number;
  inspection: HighlightClipInspection | null;
  state: 'idle' | 'inspecting' | 'confirmation' | 'creating';
  error: string | null;
}

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
  drafts: HighlightClipDraft[] = [];
  clips: HighlightClip[] = [];
  submissionClip: HighlightClip | null = null;
  submittingClipId: number | null = null;
  downloadingClipId: number | null = null;
  draggingPlayhead = false;
  clipPreviewId: number | null = null;
  clipPreviewUrl: string | null = null;
  clipPreviewLoading = false;

  loading = true;
  clipsLoading = true;
  mediaLoading = false;
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
  private clipsRequest?: Subscription;
  private nextDraftId = 1;
  private previewingDraftId: number | null = null;
  private draggingPointerId: number | null = null;
  private readonly subscriptions = new Subscription();

  constructor(
    @Inject(DOCUMENT) private document: Document,
    route: ActivatedRoute,
    private highlights: HighlightService,
    private recordings: RecordingSessionService,
    private playerFactory: PartPlayerFactory,
    private changeDetector: ChangeDetectorRef,
    private zone: NgZone,
    realtime: RealtimeService,
  ) {
    this.sessionId = Number(route.snapshot.paramMap.get('sessionId'));
    const partId = Number(route.snapshot.queryParamMap?.get('partId'));
    this.initialPartId = Number.isInteger(partId) && partId > 0 ? partId : null;
    this.subscriptions.add(
      realtime.events$.subscribe((event) => this.handleRealtimeEvent(event)),
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
    if (!this.partAt(this.startMs) || !this.partAt(this.endMs)) {
      return '开始或结束位置位于录像断档中';
    }
    return null;
  }

  ngOnInit(): void {
    if (!Number.isInteger(this.sessionId) || this.sessionId <= 0) {
      this.loading = false;
      this.error = '录像场次编号无效';
      return;
    }
    this.loadTimeline(true);
    this.loadClips();
  }

  ngOnDestroy(): void {
    this.mediaRequest?.unsubscribe();
    this.clipsRequest?.unsubscribe();
    this.subscriptions.unsubscribe();
    this.teardownPlayer();
  }

  refreshTimeline(): void {
    this.loadTimeline(false);
    this.loadClips();
  }

  selectMarker(item: MappedHighlight): void {
    this.selectedMarkerId = item.marker.id;
    this.clipName = item.marker.name;
    this.startMs = Math.max(0, item.timelineOffsetMs - 30_000);
    this.endMs = Math.min(
      this.timeline?.stableEndMs ?? item.timelineOffsetMs + 60_000,
      item.timelineOffsetMs + 60_000,
    );
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
    this.actionError = null;
    if (this.selectedMarkerId === null) {
      this.clipName = `高光片段 ${this.formatTime(this.startMs)}`;
    }
  }

  setSelectionStartFromPlayhead(): void {
    this.startMs = Math.round(this.playheadMs);
    if (this.endMs <= this.startMs) {
      this.endMs = Math.min(
        this.timeline?.stableEndMs ?? this.startMs + 60_000,
        this.startMs + 60_000,
      );
    }
    this.selectionChanged();
  }

  setSelectionEndFromPlayhead(): void {
    this.endMs = Math.round(this.playheadMs);
    this.selectionChanged();
  }

  adjustSelection(boundary: 'start' | 'end', seconds: number): void {
    const deltaMs = Math.round(seconds * 1000);
    if (boundary === 'start') {
      this.startMs = Math.max(
        0,
        Math.min(this.endMs - 1000, this.startMs + deltaMs),
      );
    } else {
      this.endMs = Math.min(
        this.timeline?.stableEndMs ?? this.endMs,
        Math.max(this.startMs + 1000, this.endMs + deltaMs),
      );
    }
    this.selectionChanged();
  }

  addDraft(): void {
    if (this.selectionError !== null) {
      return;
    }
    this.drafts = [
      ...this.drafts,
      {
        id: this.nextDraftId++,
        markerId: this.selectedMarkerId,
        name:
          this.clipName.trim() || `高光片段 ${this.formatTime(this.startMs)}`,
        startMs: this.startMs,
        endMs: this.endMs,
        inspection: null,
        state: 'idle',
        error: null,
      },
    ];
    this.selectedMarkerId = null;
    this.startMs = this.endMs;
    this.endMs = Math.min(
      this.timeline?.stableEndMs ?? this.startMs + 60_000,
      this.startMs + 60_000,
    );
    this.clipName = `高光片段 ${this.formatTime(this.startMs)}`;
  }

  updateDraft(draft: HighlightClipDraft): void {
    draft.inspection = null;
    draft.state = 'idle';
    draft.error = null;
  }

  removeDraft(draft: HighlightClipDraft): void {
    if (draft.state === 'inspecting' || draft.state === 'creating') {
      return;
    }
    this.drafts = this.drafts.filter((item) => item.id !== draft.id);
    if (this.previewingDraftId === draft.id) {
      this.previewingDraftId = null;
    }
  }

  createDraft(draft: HighlightClipDraft): void {
    if (draft.state !== 'idle' || this.draftError(draft) !== null) {
      return;
    }
    draft.state = 'inspecting';
    draft.error = null;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights
        .inspectClip(this.sessionId, draft.startMs, draft.endMs)
        .subscribe({
          next: (inspection) => {
            draft.inspection = inspection;
            if (!inspection.compatible) {
              draft.state = 'idle';
              draft.error = '所选分段编码不兼容，无法无损合并';
            } else if (inspection.confirmationRequired) {
              draft.state = 'confirmation';
            } else {
              this.persistDraft(draft, false);
            }
            this.changeDetector.markForCheck();
          },
          error: (error: unknown) => {
            draft.state = 'idle';
            draft.error = this.describeError(error, '无法创建这个片段');
            this.changeDetector.markForCheck();
          },
        }),
    );
  }

  confirmDraft(draft: HighlightClipDraft): void {
    if (draft.state !== 'confirmation') {
      return;
    }
    this.persistDraft(draft, true);
  }

  previewDraft(draft: HighlightClipDraft): void {
    if (this.draftError(draft) !== null) {
      return;
    }
    const targetPart = this.partAt(draft.startMs);
    const partChanged = targetPart?.partId !== this.selectedPart?.partId;
    this.previewingDraftId = draft.id;
    this.seekTimeline(draft.startMs);
    if (!partChanged && !this.mediaLoading) {
      void this.videoElement?.play().catch(() => undefined);
    }
  }

  seekFromTrack(event: MouseEvent, track: HTMLElement): void {
    if (this.draggingPlayhead) {
      return;
    }
    this.seekFromPointer(event.clientX, track);
  }

  startTimelineDrag(event: PointerEvent, track: HTMLElement): void {
    if (event.button !== 0 || this.isMarkerTarget(event.target)) {
      return;
    }
    event.preventDefault();
    this.draggingPointerId = event.pointerId;
    this.draggingPlayhead = true;
    try {
      track.setPointerCapture(event.pointerId);
    } catch (_error) {
      // Synthetic test events and older browsers may not own pointer capture.
    }
    this.seekFromPointer(event.clientX, track);
  }

  moveTimelineDrag(event: PointerEvent, track: HTMLElement): void {
    if (event.pointerId !== this.draggingPointerId) {
      return;
    }
    this.seekFromPointer(event.clientX, track);
  }

  endTimelineDrag(event: PointerEvent, track: HTMLElement): void {
    if (event.pointerId !== this.draggingPointerId) {
      return;
    }
    try {
      track.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // Pointer capture may already have been released by the browser.
    }
    this.draggingPointerId = null;
    this.draggingPlayhead = false;
  }

  private seekFromPointer(clientX: number, track: HTMLElement): void {
    if (!this.timeline || this.timeline.durationMs <= 0) {
      return;
    }
    const bounds = track.getBoundingClientRect();
    const ratio = Math.max(
      0,
      Math.min(1, (clientX - bounds.left) / Math.max(1, bounds.width)),
    );
    const valueMs = Math.round(ratio * this.timeline.durationMs);
    this.seekTimeline(this.snapToMarker(valueMs, bounds.width));
  }

  private snapToMarker(valueMs: number, trackWidth: number): number {
    if (!this.timeline || this.timeline.markers.length === 0) {
      return valueMs;
    }
    const thresholdMs = Math.max(
      500,
      Math.round((this.timeline.durationMs * 10) / Math.max(1, trackWidth)),
    );
    const nearest = this.timeline.markers.reduce((current, item) =>
      Math.abs(item.timelineOffsetMs - valueMs) <
      Math.abs(current.timelineOffsetMs - valueMs)
        ? item
        : current,
    );
    return Math.abs(nearest.timelineOffsetMs - valueMs) <= thresholdMs
      ? nearest.timelineOffsetMs
      : valueMs;
  }

  private isMarkerTarget(target: EventTarget | null): boolean {
    return target instanceof Element && target.closest('.marker-pin') !== null;
  }

  handleTimelineKeydown(event: KeyboardEvent): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return;
    }
    event.preventDefault();
    const direction = event.key === 'ArrowLeft' ? -1 : 1;
    const target = Math.max(
      0,
      Math.min(
        this.timeline?.stableEndMs ?? 0,
        this.playheadMs + direction * 5000,
      ),
    );
    this.seekTimeline(target);
  }

  openClipSubmission(clip: HighlightClip): void {
    if (clip.uploadJobId || this.submittingClipId !== null) {
      return;
    }
    this.actionError = null;
    this.submissionClip = clip;
    this.changeDetector.markForCheck();
  }

  closeClipSubmission(): void {
    this.submissionClip = null;
  }

  clipSubmissionSaved(settings: RoomUploadPolicyRequest): void {
    const clip = this.submissionClip;
    if (!clip || clip.uploadJobId || this.submittingClipId !== null) {
      return;
    }
    this.submittingClipId = clip.id;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights.createUploadTask(clip.id, settings).subscribe({
        next: ({ jobId }) => {
          this.clips = this.clips.map((item) =>
            item.id === clip.id
              ? { ...item, uploadJobId: jobId, uploadState: 'ready' }
              : item,
          );
          this.submittingClipId = null;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.submittingClipId = null;
          this.actionError = this.describeError(error, '创建上传任务失败');
          this.changeDetector.markForCheck();
        },
      }),
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
      }),
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

  downloadClip(clip: HighlightClip): void {
    if (clip.state !== 'ready' || this.downloadingClipId !== null) {
      return;
    }
    this.downloadingClipId = clip.id;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights.createMediaAccess(clip.id).subscribe({
        next: (access) => {
          const link = this.document.createElement('a');
          link.href = this.highlights.downloadUrl(clip.id, access);
          link.download = '';
          link.rel = 'noopener noreferrer';
          link.style.display = 'none';
          this.document.body.appendChild(link);
          link.click();
          link.remove();
          this.downloadingClipId = null;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.downloadingClipId = null;
          this.actionError = this.describeError(error, '下载高光片段失败');
          this.changeDetector.markForCheck();
        },
      }),
    );
  }

  deleteClip(clip: HighlightClip): void {
    this.subscriptions.add(
      this.highlights.deleteClip(clip.id).subscribe({
        next: () => {
          this.clips = this.clips.filter((item) => item.id !== clip.id);
          if (this.clipPreviewId === clip.id) {
            this.closeClipPreview();
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.actionError = this.describeError(error, '删除高光片段失败');
          this.changeDetector.markForCheck();
        },
      }),
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
                  item.marker.id === markerId ? { ...item, marker } : item,
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
        }),
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
                (value) => value.marker.id !== markerId,
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
      }),
    );
  }

  handleTimeUpdate(): void {
    if (!this.videoElement || !this.selectedPart) {
      return;
    }
    this.playheadMs =
      this.selectedPart.timelineStartMs + this.videoElement.currentTime * 1000;
    if (this.previewingDraftId === null) {
      return;
    }
    const draft = this.drafts.find(
      (item) => item.id === this.previewingDraftId,
    );
    if (!draft || this.playheadMs < draft.endMs) {
      return;
    }
    this.videoElement.pause();
    this.previewingDraftId = null;
  }

  handleMediaCanPlay(): void {
    if (this.previewingDraftId !== null) {
      void this.videoElement?.play().catch(() => undefined);
    }
  }

  handleMediaEnded(): void {
    if (
      this.previewingDraftId === null ||
      !this.timeline ||
      !this.selectedPart
    ) {
      return;
    }
    const draft = this.drafts.find(
      (item) => item.id === this.previewingDraftId,
    );
    const currentPart = this.selectedPart;
    const nextPart = this.timeline.parts.find(
      (part) => part.timelineStartMs > currentPart.timelineStartMs,
    );
    if (!draft || !nextPart || nextPart.timelineStartMs >= draft.endMs) {
      this.previewingDraftId = null;
      return;
    }
    this.selectPart(nextPart);
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
    const base = `${String(minutes).padStart(2, '0')}:${String(
      seconds,
    ).padStart(2, '0')}`;
    return hours > 0 ? `${String(hours).padStart(2, '0')}:${base}` : base;
  }

  positionPercent(valueMs: number): number {
    if (!this.timeline || this.timeline.durationMs <= 0) {
      return 0;
    }
    return Math.max(
      0,
      Math.min(100, (valueMs / this.timeline.durationMs) * 100),
    );
  }

  partWidthPercent(part: HighlightTimelinePart): number {
    return this.positionPercent(part.durationMs);
  }

  trackClip(_index: number, clip: HighlightClip): number {
    return clip.id;
  }

  trackDraft(_index: number, draft: HighlightClipDraft): number {
    return draft.id;
  }

  trackMarker(_index: number, item: MappedHighlight): number {
    return item.marker.id;
  }

  trackPart(_index: number, part: HighlightTimelinePart): number {
    return part.partId;
  }

  draftError(draft: HighlightClipDraft): string | null {
    if (!draft.name.trim()) {
      return '请输入片段名称';
    }
    if (!this.timeline || draft.startMs < 0) {
      return '裁剪范围无效';
    }
    if (draft.endMs <= draft.startMs) {
      return '结束位置必须晚于开始位置';
    }
    if (draft.endMs > this.timeline.stableEndMs) {
      return '结束位置仍在录制安全区之外，请稍后刷新';
    }
    if (!this.partAt(draft.startMs) || !this.partAt(draft.endMs)) {
      return '开始或结束位置位于录像断档中';
    }
    return null;
  }

  uploadStatus(clip: HighlightClip): string {
    const labels: Record<string, string> = {
      waiting_artifacts: '等待文件',
      ready: '等待上传',
      uploading: '正在上传',
      submitting: '正在投稿',
      waiting_review: '等待审核',
      approved: '审核通过',
      rejected: '审核未通过',
      paused: '已暂停',
      completed: '已完成',
    };
    return clip.uploadState
      ? (labels[clip.uploadState] ?? clip.uploadState)
      : '';
  }

  private persistDraft(
    draft: HighlightClipDraft,
    confirmKeyframe: boolean,
  ): void {
    this.cancelClipLoad();
    draft.state = 'creating';
    draft.error = null;
    this.subscriptions.add(
      this.highlights
        .createClip(this.sessionId, {
          markerId: draft.markerId,
          name: draft.name.trim(),
          startMs: draft.startMs,
          endMs: draft.endMs,
          confirmKeyframe,
        })
        .subscribe({
          next: (clip) => {
            this.clips = [...this.clips, clip];
            this.drafts = this.drafts.filter((item) => item.id !== draft.id);
            this.changeDetector.markForCheck();
          },
          error: (error: unknown) => {
            draft.state = draft.inspection?.confirmationRequired
              ? 'confirmation'
              : 'idle';
            draft.error = this.describeError(error, '创建高光片段失败');
            this.changeDetector.markForCheck();
          },
        }),
    );
  }

  private loadClips(): void {
    this.clipsRequest?.unsubscribe();
    this.clipsLoading = true;
    this.clipsRequest = this.highlights.listClips(this.sessionId).subscribe({
      next: (clips) => {
        this.clips = [...clips];
        this.clipsLoading = false;
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.clipsLoading = false;
        this.actionError = this.describeError(error, '无法加载已创建片段');
        this.changeDetector.markForCheck();
      },
    });
    this.subscriptions.add(this.clipsRequest);
  }

  private cancelClipLoad(): void {
    this.clipsRequest?.unsubscribe();
    this.clipsRequest = undefined;
    this.clipsLoading = false;
  }

  private loadTimeline(initial: boolean): void {
    if (initial && this.initialPartId === null) {
      this.loading = false;
      this.selectedPart = null;
      this.mediaUrl = null;
      this.error = '请从录制任务详情中的具体分段进入剪辑';
      this.changeDetector.markForCheck();
      return;
    }
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
          if (initial && this.initialPartId !== null && !requestedPart) {
            this.mediaRequest?.unsubscribe();
            this.teardownPlayer();
            this.selectedPart = null;
            this.mediaUrl = null;
            this.error = '所选分段的本地录像已不存在，无法剪辑';
            this.changeDetector.markForCheck();
            return;
          }
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
                timeline.stableEndMs,
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
      }),
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
    const parts = this.timeline?.parts ?? [];
    return (
      parts.find((part, index) => {
        const endMs = part.timelineStartMs + part.durationMs;
        return (
          valueMs >= part.timelineStartMs &&
          (valueMs < endMs ||
            (index === parts.length - 1 && valueMs === endMs))
        );
      }) ?? null
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
    this.mediaRequest = this.recordings
      .createMediaAccess(part.partId)
      .subscribe({
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
        playbackMode: this.mediaAccess?.playbackMode ?? 'sequential',
        durationMs: this.mediaAccess?.durationMs ?? null,
        fileSizeBytes: this.mediaAccess?.fileSizeBytes ?? null,
      },
      (event) => {
        this.zone.run(() => {
          if (event.type === 'error') {
            this.mediaError = event.message;
            this.teardownPlayer();
            this.changeDetector.markForCheck();
          } else if (event.type === 'stalled') {
            this.handleMediaStalled();
            this.changeDetector.markForCheck();
          }
        });
      },
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
    if (event.type === 'upload_progress') {
      this.handleUploadProgress(event.data);
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
        clip.id === item.id ? { ...clip, ...item } : clip,
      );
      this.changeDetector.markForCheck();
      if (item.state === 'ready' && previous.state !== 'ready') {
        this.subscriptions.add(
          this.highlights.getClip(item.id).subscribe((clip) => {
            this.clips = this.clips.map((value) =>
              value.id === clip.id ? clip : value,
            );
            this.changeDetector.markForCheck();
          }),
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

  private handleUploadProgress(value: unknown): void {
    if (typeof value !== 'object' || value === null || !('jobs' in value)) {
      return;
    }
    const jobs = (value as { jobs?: unknown }).jobs;
    if (!Array.isArray(jobs)) {
      return;
    }
    let changed = false;
    this.clips = this.clips.map((clip) => {
      const job = jobs.find(
        (item): item is Record<string, unknown> =>
          typeof item === 'object' &&
          item !== null &&
          Number((item as Record<string, unknown>)['jobId']) ===
            clip.uploadJobId,
      );
      if (!job) {
        return clip;
      }
      changed = true;
      return {
        ...clip,
        uploadState: typeof job['state'] === 'string' ? job['state'] : null,
        uploadPercent:
          typeof job['percent'] === 'number' ? job['percent'] : null,
        uploadBvid: typeof job['bvid'] === 'string' ? job['bvid'] : null,
      };
    });
    if (changed) {
      this.changeDetector.markForCheck();
    }
  }

  private describeError(error: unknown, fallback: string): string {
    if (typeof error === 'object' && error !== null && 'error' in error) {
      const detail = (error as { error?: { detail?: unknown } }).error?.detail;
      if (typeof detail === 'string') {
        return detail;
      }
      if (
        typeof detail === 'object' &&
        detail !== null &&
        'message' in detail &&
        typeof (detail as { message?: unknown }).message === 'string'
      ) {
        return (detail as { message: string }).message;
      }
    }
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
