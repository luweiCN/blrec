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

type TimelinePopover =
  | { readonly kind: 'none' }
  | {
      readonly kind: 'point';
      readonly timeMs: number;
      readonly markerId: number | null;
    }
  | { readonly kind: 'boundary'; readonly boundary: 'start' | 'end' }
  | { readonly kind: 'draft'; readonly draftId: number }
  | { readonly kind: 'clip'; readonly clipId: number };

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
  selectionActive = false;
  startBoundarySet = false;
  endBoundarySet = false;
  drafts: HighlightClipDraft[] = [];
  clips: HighlightClip[] = [];
  submissionClip: HighlightClip | null = null;
  submittingClipId: number | null = null;
  downloadingClipId: number | null = null;
  retryingClipId: number | null = null;
  draggingPlayhead = false;
  hoverTimeMs: number | null = null;
  timelinePopover: TimelinePopover = { kind: 'none' };
  isPlaying = false;
  isMuted = false;
  editingDraftId: number | null = null;
  sourceClipId: number | null = null;
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
  private editorWorkbenchElement: HTMLElement | null = null;
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

  @ViewChild('editorWorkbench')
  set editorWorkbenchRef(value: ElementRef<HTMLElement> | undefined) {
    this.editorWorkbenchElement = value?.nativeElement ?? null;
  }

  get clip(): HighlightClip | null {
    return this.clips.length > 0 ? this.clips[this.clips.length - 1] : null;
  }

  get selectionError(): string | null {
    if (
      !this.selectionActive ||
      !this.startBoundarySet ||
      !this.endBoundarySet ||
      !this.timeline ||
      !this.selectedPart
    ) {
      return null;
    }
    if (
      this.startMs < this.editorPartStartMs ||
      this.endMs > this.editorPartEndMs
    ) {
      return '片段只能位于当前分 P 内';
    }
    if (this.endMs <= this.startMs) {
      return '结束位置必须晚于开始位置';
    }
    if (this.endMs > this.editorStableEndMs) {
      return '结束位置仍在录制安全区之外，请稍后再试';
    }
    return null;
  }

  get editorPartStartMs(): number {
    return this.selectedPart?.timelineStartMs ?? 0;
  }

  get editorPartEndMs(): number {
    const part = this.selectedPart;
    return part ? part.timelineStartMs + part.durationMs : 0;
  }

  get editorStableEndMs(): number {
    const part = this.selectedPart;
    return part
      ? Math.min(part.timelineStartMs + part.durationMs, part.stableEndMs)
      : 0;
  }

  get editorDurationMs(): number {
    return this.selectedPart?.durationMs ?? 0;
  }

  get visibleMarkers(): readonly MappedHighlight[] {
    const partId = this.selectedPart?.partId;
    return (this.timeline?.markers ?? []).filter(
      (item) => item.partId === partId,
    );
  }

  get visibleClips(): readonly HighlightClip[] {
    const partId = this.selectedPart?.partId;
    return this.clips.filter((clip) =>
      clip.sources.some((source) => source.partId === partId),
    );
  }

  get selectedDraft(): HighlightClipDraft | null {
    return (
      this.drafts.find((draft) => draft.id === this.editingDraftId) ?? null
    );
  }

  get selectedTimelineClip(): HighlightClip | null {
    return this.clips.find((clip) => clip.id === this.sourceClipId) ?? null;
  }

  get selectedTimelineMarker(): MappedHighlight | null {
    const markerId =
      this.timelinePopover.kind === 'point'
        ? this.timelinePopover.markerId
        : null;
    if (markerId === null) {
      return null;
    }
    return (
      this.visibleMarkers.find((item) => item.marker.id === markerId) ?? null
    );
  }

  get hasCompleteSelection(): boolean {
    return this.selectionActive && this.startBoundarySet && this.endBoundarySet;
  }

  get selectedDraftLocked(): boolean {
    const state = this.selectedDraft?.state;
    return state === 'inspecting' || state === 'creating';
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
    if (item.partId !== this.selectedPart?.partId) {
      return;
    }
    this.prepareForTimelinePoint(item.marker.id);
    this.selectedMarkerId = item.marker.id;
    this.seekTimeline(item.timelineOffsetMs);
    this.pausePlayback();
    this.timelinePopover = {
      kind: 'point',
      timeMs: item.timelineOffsetMs,
      markerId: item.marker.id,
    };
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

  setSelectionStartFromPlayhead(): void {
    if (this.selectedDraftLocked) {
      return;
    }
    if (!this.selectionActive) {
      this.beginBoundarySelection();
    }
    const startMs = Math.round(this.playheadMs);
    if (this.endBoundarySet && startMs >= this.endMs) {
      this.actionError = '开始位置必须早于结束位置';
      return;
    }
    this.startMs = startMs;
    this.startBoundarySet = true;
    this.selectionActive = true;
    this.actionError = null;
    this.finishSelectionIfReady();
  }

  setSelectionEndFromPlayhead(): void {
    if (this.selectedDraftLocked) {
      return;
    }
    if (!this.selectionActive) {
      this.beginBoundarySelection();
    }
    const endMs = Math.round(this.playheadMs);
    if (this.startBoundarySet && endMs <= this.startMs) {
      this.actionError = '结束位置必须晚于开始位置';
      return;
    }
    this.endMs = endMs;
    this.endBoundarySet = true;
    this.selectionActive = true;
    this.actionError = null;
    this.finishSelectionIfReady();
  }

  adjustSelection(boundary: 'start' | 'end', seconds: number): void {
    if (this.selectedDraftLocked) {
      return;
    }
    const boundarySet =
      boundary === 'start' ? this.startBoundarySet : this.endBoundarySet;
    if (!boundarySet) {
      return;
    }
    this.selectionActive = true;
    const deltaMs = Math.round(seconds * 1000);
    if (boundary === 'start') {
      const candidate = Math.max(
        this.editorPartStartMs,
        Math.min(this.editorStableEndMs, this.startMs + deltaMs),
      );
      if (this.endBoundarySet && candidate >= this.endMs) {
        this.actionError = '开始位置必须早于结束位置';
        return;
      }
      this.startMs = candidate;
    } else {
      const candidate = Math.max(
        this.editorPartStartMs,
        Math.min(this.editorStableEndMs, this.endMs + deltaMs),
      );
      if (this.startBoundarySet && candidate <= this.startMs) {
        this.actionError = '结束位置必须晚于开始位置';
        return;
      }
      this.endMs = candidate;
    }
    this.actionError = null;
    this.syncSelectedDraft();
    this.timelinePopover = { kind: 'boundary', boundary };
    this.previewBoundary(boundary);
  }

  selectDraftForEditing(draft: HighlightClipDraft): void {
    if (draft.state === 'inspecting' || draft.state === 'creating') {
      return;
    }
    if (this.editingDraftId !== draft.id) {
      this.syncSelectedDraft();
    }
    this.editingDraftId = draft.id;
    this.selectionActive = true;
    this.startBoundarySet = true;
    this.endBoundarySet = true;
    this.sourceClipId = null;
    this.selectedMarkerId = draft.markerId;
    this.clipName = draft.name;
    this.startMs = draft.startMs;
    this.endMs = draft.endMs;
    this.timelinePopover = { kind: 'draft', draftId: draft.id };
    this.previewBoundary('start');
  }

  selectClipForEditing(clip: HighlightClip): void {
    if (
      !clip.sources.some(
        (source) => source.partId === this.selectedPart?.partId,
      )
    ) {
      return;
    }
    this.clearTimelineSelection();
    this.sourceClipId = clip.id;
    this.selectedMarkerId = clip.markerId;
    this.timelinePopover = { kind: 'clip', clipId: clip.id };
    this.pausePlayback();
    this.seekTimeline(clip.requestedStartMs);
  }

  copyClipToDraft(clip: HighlightClip): void {
    if (
      !clip.sources.some(
        (source) => source.partId === this.selectedPart?.partId,
      )
    ) {
      return;
    }
    const draft = this.makeDraft(
      clip.markerId,
      `${clip.name} 副本`,
      clip.requestedStartMs,
      clip.requestedEndMs,
    );
    this.drafts = [...this.drafts, draft];
    this.selectDraftForEditing(draft);
  }

  addDraft(): void {
    if (!this.hasCompleteSelection || this.selectionError !== null) {
      return;
    }
    const editing = this.drafts.find(
      (draft) => draft.id === this.editingDraftId,
    );
    if (editing) {
      editing.markerId = this.selectedMarkerId;
      editing.name = this.clipName.trim();
      editing.startMs = this.startMs;
      editing.endMs = this.endMs;
      this.updateDraft(editing);
      this.drafts = [...this.drafts];
      this.timelinePopover = { kind: 'draft', draftId: editing.id };
      return;
    }
    const draft = this.makeDraft(
      this.selectedMarkerId,
      this.clipName,
      this.startMs,
      this.endMs,
    );
    this.drafts = [...this.drafts, draft];
    this.editingDraftId = draft.id;
    this.sourceClipId = null;
    this.clipName = draft.name;
    this.timelinePopover = { kind: 'draft', draftId: draft.id };
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
    if (this.editingDraftId === draft.id) {
      this.resetWorkingSelection();
    }
    if (this.previewingDraftId === draft.id) {
      this.previewingDraftId = null;
    }
  }

  clearTimelineSelection(): void {
    this.syncSelectedDraft();
    this.resetWorkingSelection();
  }

  cancelSelectedDraft(): void {
    const draft = this.selectedDraft;
    if (draft) {
      this.removeDraft(draft);
    }
  }

  createSelectedDraft(): void {
    const draft = this.selectedDraft;
    if (!draft) {
      return;
    }
    this.syncSelectedDraft();
    this.createDraft(draft);
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
    if (this.editingDraftId === draft.id) {
      this.syncSelectedDraft();
    }
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

  startTimelineDrag(event: PointerEvent, track: HTMLElement): void {
    if (event.button !== 0 || this.isTimelineItemTarget(event.target)) {
      return;
    }
    const valueMs = this.pointerTimeMs(event.clientX, track, true);
    if (valueMs === null) {
      return;
    }
    event.preventDefault();
    this.pausePlayback();
    this.prepareForTimelinePoint(null);
    this.draggingPointerId = event.pointerId;
    this.draggingPlayhead = true;
    try {
      track.setPointerCapture(event.pointerId);
    } catch (_error) {
      // Synthetic test events and older browsers may not own pointer capture.
    }
    this.seekTimeline(valueMs);
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
    this.showPointActions(this.playheadMs, null);
  }

  handleTimelineHover(event: MouseEvent, track: HTMLElement): void {
    this.hoverTimeMs = this.pointerTimeMs(event.clientX, track, false);
  }

  clearTimelineHover(): void {
    this.hoverTimeMs = null;
  }

  showBoundaryActions(boundary: 'start' | 'end'): void {
    if (!this.hasCompleteSelection || this.selectedDraftLocked) {
      return;
    }
    this.timelinePopover = { kind: 'boundary', boundary };
    this.previewBoundary(boundary);
  }

  setPointAsBoundary(boundary: 'start' | 'end'): void {
    if (this.timelinePopover.kind !== 'point') {
      return;
    }
    const point = this.timelinePopover;
    if (!this.selectionActive) {
      this.beginBoundarySelection(point.markerId);
    }
    this.playheadMs = point.timeMs;
    if (boundary === 'start') {
      this.setSelectionStartFromPlayhead();
    } else {
      this.setSelectionEndFromPlayhead();
    }
    const boundarySet =
      boundary === 'start' ? this.startBoundarySet : this.endBoundarySet;
    if (!this.hasCompleteSelection && boundarySet) {
      this.timelinePopover = { kind: 'boundary', boundary };
    }
  }

  private seekFromPointer(clientX: number, track: HTMLElement): void {
    const valueMs = this.pointerTimeMs(clientX, track, true);
    if (valueMs === null) {
      return;
    }
    this.seekTimeline(valueMs);
  }

  private pointerTimeMs(
    clientX: number,
    track: HTMLElement,
    snap: boolean,
  ): number | null {
    if (!this.selectedPart || this.editorDurationMs <= 0) {
      return null;
    }
    const bounds = track.getBoundingClientRect();
    const ratio = Math.max(
      0,
      Math.min(1, (clientX - bounds.left) / Math.max(1, bounds.width)),
    );
    const valueMs =
      this.editorPartStartMs + Math.round(ratio * this.editorDurationMs);
    if (valueMs > this.editorStableEndMs) {
      return null;
    }
    return snap ? this.snapToMarker(valueMs, bounds.width) : valueMs;
  }

  private snapToMarker(valueMs: number, trackWidth: number): number {
    if (!this.selectedPart || this.visibleMarkers.length === 0) {
      return valueMs;
    }
    const thresholdMs = Math.max(
      500,
      Math.round((this.editorDurationMs * 10) / Math.max(1, trackWidth)),
    );
    const nearest = this.visibleMarkers.reduce((current, item) =>
      Math.abs(item.timelineOffsetMs - valueMs) <
      Math.abs(current.timelineOffsetMs - valueMs)
        ? item
        : current,
    );
    return Math.abs(nearest.timelineOffsetMs - valueMs) <= thresholdMs
      ? nearest.timelineOffsetMs
      : valueMs;
  }

  private isTimelineItemTarget(target: EventTarget | null): boolean {
    return (
      target instanceof Element &&
      target.closest(
        '.marker-pin, .draft-range, .clip-range, .selection-boundary',
      ) !== null
    );
  }

  handleTimelineKeydown(event: KeyboardEvent): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return;
    }
    event.preventDefault();
    const direction = event.key === 'ArrowLeft' ? -1 : 1;
    const target = Math.max(
      this.editorPartStartMs,
      Math.min(this.editorStableEndMs, this.playheadMs + direction * 5000),
    );
    this.seekTimeline(target);
  }

  get popoverTimeMs(): number {
    const popover = this.timelinePopover;
    if (popover.kind === 'point') {
      return popover.timeMs;
    }
    if (popover.kind === 'boundary') {
      return popover.boundary === 'start' ? this.startMs : this.endMs;
    }
    if (popover.kind === 'draft') {
      const draft = this.selectedDraft;
      return draft ? (draft.startMs + draft.endMs) / 2 : this.playheadMs;
    }
    if (popover.kind === 'clip') {
      const clip = this.selectedTimelineClip;
      return clip
        ? (clip.requestedStartMs + clip.requestedEndMs) / 2
        : this.playheadMs;
    }
    return this.playheadMs;
  }

  popoverTransform(valueMs: number): string {
    const percent = this.positionPercent(valueMs);
    if (percent < 14) {
      return 'translateX(0)';
    }
    if (percent > 86) {
      return 'translateX(-100%)';
    }
    return 'translateX(-50%)';
  }

  togglePlayback(): void {
    if (!this.videoElement) {
      return;
    }
    this.timelinePopover = { kind: 'none' };
    if (this.videoElement.paused) {
      void this.videoElement.play().catch(() => undefined);
    } else {
      this.videoElement.pause();
    }
  }

  handleMediaPlay(): void {
    this.isPlaying = true;
  }

  handleMediaPause(): void {
    this.isPlaying = false;
  }

  toggleMute(): void {
    if (!this.videoElement) {
      return;
    }
    this.videoElement.muted = !this.videoElement.muted;
    this.isMuted = this.videoElement.muted;
  }

  setVolume(value: number | string): void {
    if (!this.videoElement) {
      return;
    }
    const volume = Math.max(0, Math.min(1, Number(value)));
    if (!Number.isFinite(volume)) {
      return;
    }
    this.videoElement.volume = volume;
    this.videoElement.muted = volume === 0;
    this.isMuted = this.videoElement.muted;
  }

  toggleFullscreen(): void {
    void this.editorWorkbenchElement
      ?.requestFullscreen?.()
      .catch(() => undefined);
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

  retryClip(clip: HighlightClip): void {
    if (clip.state !== 'failed' || this.retryingClipId !== null) {
      return;
    }
    this.retryingClipId = clip.id;
    this.actionError = null;
    this.subscriptions.add(
      this.highlights.retryClip(clip.id).subscribe({
        next: (updated) => {
          this.clips = this.clips.map((item) =>
            item.id === updated.id ? updated : item,
          );
          this.retryingClipId = null;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.retryingClipId = null;
          this.actionError = this.describeError(error, '重试高光片段失败');
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
    if (!this.selectedPart || this.editorDurationMs <= 0) {
      return 0;
    }
    return Math.max(
      0,
      Math.min(
        100,
        ((valueMs - this.editorPartStartMs) / this.editorDurationMs) * 100,
      ),
    );
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

  draftError(draft: HighlightClipDraft): string | null {
    if (!draft.name.trim()) {
      return '请输入片段名称';
    }
    if (
      !this.timeline ||
      !this.selectedPart ||
      draft.startMs < this.editorPartStartMs ||
      draft.endMs > this.editorPartEndMs
    ) {
      return '裁剪范围无效';
    }
    if (draft.endMs <= draft.startMs) {
      return '结束位置必须晚于开始位置';
    }
    if (draft.endMs > this.editorStableEndMs) {
      return '结束位置仍在录制安全区之外，请稍后再试';
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

  private beginBoundarySelection(markerId: number | null = null): void {
    this.editingDraftId = null;
    this.sourceClipId = null;
    this.selectedMarkerId = markerId;
    this.selectionActive = true;
    this.startBoundarySet = false;
    this.endBoundarySet = false;
    this.clipName = '';
  }

  private finishSelectionIfReady(): void {
    if (!this.hasCompleteSelection) {
      return;
    }
    if (this.selectionError !== null) {
      this.actionError = this.selectionError;
      return;
    }
    this.addDraft();
  }

  private makeDraft(
    markerId: number | null,
    name: string,
    startMs: number,
    endMs: number,
  ): HighlightClipDraft {
    return {
      id: this.nextDraftId++,
      markerId,
      name:
        name.trim() ||
        `高光片段 ${this.formatTime(startMs - this.editorPartStartMs)}`,
      startMs,
      endMs,
      inspection: null,
      state: 'idle',
      error: null,
    };
  }

  private syncSelectedDraft(): void {
    const draft = this.selectedDraft;
    if (!draft || !this.hasCompleteSelection) {
      return;
    }
    const name = this.clipName.trim() || draft.name;
    const rangeChanged =
      draft.startMs !== this.startMs || draft.endMs !== this.endMs;
    const metadataChanged =
      draft.markerId !== this.selectedMarkerId || draft.name !== name;
    if (!rangeChanged && !metadataChanged) {
      return;
    }
    draft.markerId = this.selectedMarkerId;
    draft.name = name;
    draft.startMs = this.startMs;
    draft.endMs = this.endMs;
    if (rangeChanged) {
      this.updateDraft(draft);
    }
    this.drafts = [...this.drafts];
  }

  private resetWorkingSelection(): void {
    this.editingDraftId = null;
    this.sourceClipId = null;
    this.selectedMarkerId = null;
    this.selectionActive = false;
    this.startBoundarySet = false;
    this.endBoundarySet = false;
    this.clipName = '';
    this.timelinePopover = { kind: 'none' };
  }

  private prepareForTimelinePoint(markerId: number | null): void {
    const hasUnfinishedBoundary =
      this.selectionActive &&
      this.editingDraftId === null &&
      this.startBoundarySet !== this.endBoundarySet;
    if (hasUnfinishedBoundary) {
      if (markerId !== null) {
        this.selectedMarkerId = markerId;
      }
      this.sourceClipId = null;
      this.timelinePopover = { kind: 'none' };
      return;
    }
    this.clearTimelineSelection();
  }

  private showPointActions(timeMs: number, markerId: number | null): void {
    this.timelinePopover = { kind: 'point', timeMs, markerId };
  }

  private pausePlayback(): void {
    this.videoElement?.pause();
    this.isPlaying = false;
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
            if (this.editingDraftId === draft.id) {
              this.resetWorkingSelection();
            }
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
            if (!initial && part.recording) {
              this.loadMedia();
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
    const selected = this.selectedPart;
    if (
      selected &&
      valueMs >= selected.timelineStartMs &&
      valueMs <= selected.timelineStartMs + selected.durationMs
    ) {
      this.selectPart(
        selected,
        Math.min(selected.durationMs, valueMs - selected.timelineStartMs),
      );
      return;
    }
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
          (valueMs < endMs || (index === parts.length - 1 && valueMs === endMs))
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

  private previewBoundary(boundary: 'start' | 'end'): void {
    this.videoElement?.pause();
    this.previewingDraftId = null;
    this.seekTimeline(boundary === 'start' ? this.startMs : this.endMs);
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
