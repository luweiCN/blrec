import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  Input,
  OnDestroy,
  OnInit,
  Output,
} from '@angular/core';

import type { NzCascaderOption } from 'ng-zorro-antd/cascader';
import { NzMessageService } from 'ng-zorro-antd/message';
import { Observable, Subject, forkJoin, of, throwError } from 'rxjs';
import { catchError, finalize, map, takeUntil } from 'rxjs/operators';

import { BiliAccount } from '../../uploads/shared/bili-account.model';
import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  BiliCollection,
  BiliCollectionCatalog,
  CoverAsset,
  RoomUploadPolicyDraft,
  RoomUploadPolicyRequest,
  UploadAccountMode,
  UploadCategoryCatalog,
  UploadCategoryNode,
  UploadCoverMode,
  UploadCreationStatement,
  UploadRetentionMode,
} from './room-upload-policy.model';
import { RoomUploadPolicyService } from './room-upload-policy.service';
import { RecordingSubmissionService } from './recording-submission.service';

const DEFAULT_DRAFT: RoomUploadPolicyDraft = {
  accountMode: 'primary',
  accountId: null,
  enabled: true,
  titleTemplate:
    '【直播回放】【{{ anchor_name }}】{{ title }} {{ live_start_time | date: "%Y年%m月%d日%H点%M分" }}',
  descriptionTemplate:
    '直播录像\n{{ anchor_name }}直播间：https://live.bilibili.com/{{ room_id }}',
  partTitleTemplate:
    'P{{ part_index }}-{{ area_name }}-{{ live_start_time | date: "%m月%d日%H点%M分" }}',
  dynamicTemplate:
    '直播录像\n{{ anchor_name }}直播间：https://live.bilibili.com/{{ room_id }}',
  tid: 21,
  tags: '直播回放,{{ anchor_name }},{{ area_name }}',
  creationStatementId: -2,
  originalAuthorization: false,
  source: 'https://live.bilibili.com/{{ room_id }}',
  isOnlySelf: false,
  publishDynamic: true,
  upSelectionReply: false,
  upCloseReply: false,
  upCloseDanmu: false,
  autoComment: true,
  danmakuBackfill: true,
  filters: {},
  collectionSeasonId: null,
  collectionSectionId: null,
  coverMode: 'live',
  coverAssetId: null,
  publishDelaySeconds: 0,
  retentionMode: 'submitted',
  retentionDays: 5,
};

const LIVE_CATEGORY_ALIASES: Readonly<Record<string, readonly string[]>> = {
  教育学习: ['校园学习'],
  其他单机: ['单机游戏'],
  主机游戏: ['单机游戏'],
  单机游戏: ['单机游戏'],
  网游: ['网络游戏'],
  手游: ['手机游戏'],
  无畏契约: ['电子竞技', '网络游戏'],
  英雄联盟: ['电子竞技', '网络游戏'],
  王者荣耀: ['电子竞技', '手机游戏'],
};

interface CategoryRecommendation {
  readonly path: readonly [number, number];
  readonly label: string;
}

type PublishMode = 'immediate' | 'scheduled';

type PolicyValidationErrors = Partial<
  Record<
    | 'account'
    | 'title'
    | 'partTitle'
    | 'category'
    | 'tags'
    | 'creationStatement'
    | 'source'
    | 'cover'
    | 'collection'
    | 'schedule'
    | 'retention',
    string
  >
>;

@Component({
  selector: 'app-upload-policy-dialog',
  templateUrl: './upload-policy-dialog.component.html',
  styleUrls: ['./upload-policy-dialog.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UploadPolicyDialogComponent implements OnInit, OnDestroy {
  @Input() roomId!: number;
  @Input() roomIds: readonly number[] = [];
  @Input() sessionId: number | null = null;
  @Input() roomName = '';
  @Input() liveAreaName = '';
  @Input() liveParentAreaName = '';
  @Input() allowRestoreInherited = true;
  @Input() deferredSave = false;
  @Output() closed = new EventEmitter<void>();
  @Output() saved = new EventEmitter<void>();
  @Output() settingsConfirmed = new EventEmitter<RoomUploadPolicyRequest>();

  visible = true;
  loading = true;
  categoryLoading = false;
  saving = false;
  existingPolicy = false;
  error: string | null = null;
  categoryError: string | null = null;
  coverError: string | null = null;
  collectionError: string | null = null;
  saveAttempted = false;
  accounts: readonly BiliAccount[] = [];
  catalog: UploadCategoryCatalog | null = null;
  categoryOptions: NzCascaderOption[] = [];
  coverAssets: readonly CoverAsset[] = [];
  selectedCoverPreviewUrl: string | null = null;
  collectionCatalog: BiliCollectionCatalog | null = null;
  categoryPath: number[] = [];
  collectionSelection: string | null = null;
  publishMode: PublishMode = 'immediate';
  publishDelayHours = 2;
  readonly retentionModeOptions: {
    label: string;
    value: UploadRetentionMode;
  }[] = [
    { label: '投稿成功后删除', value: 'submitted' },
    { label: '审核通过后删除', value: 'approved' },
    { label: '上传完成后删除', value: 'upload_completed' },
    { label: '容量超限时清理', value: 'capacity' },
    { label: '从不删除', value: 'never' },
  ];
  coverLoading = false;
  coverUploading = false;
  collectionLoading = false;
  newCollectionVisible = false;
  creatingCollection = false;
  newCollectionTitle = '';
  newCollectionDescription = '';
  newCollectionCoverAssetId: number | null = null;
  newCollectionError: string | null = null;
  draft: RoomUploadPolicyDraft = this.newDraft();

  readonly templateTip =
    '可用变量：{{ title }}、{{ anchor_name }}、{{ area_name }}、{{ parent_area_name }}、{{ room_id }}、{{ live_start_time }}、{{ live_end_time }}、{{ part_count }}。分 P 标题还可使用 {{ part_index }}。';
  readonly partTemplateTip =
    '每个录制分段分别渲染。{{ part_index }} 是从 1 开始的分 P 序号，例如 P1、P2。';

  private readonly destroy$ = new Subject<void>();
  private coverPreviewObjectUrl: string | null = null;
  private coverPreviewGeneration = 0;
  private collectionLoadGeneration = 0;

  constructor(
    private policyService: RoomUploadPolicyService,
    private submissionService: RecordingSubmissionService,
    private accountService: BiliAccountService,
    private message: NzMessageService,
    private changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    forkJoin({
      policy: this.loadPolicy(),
      accounts: this.accountService.listAccounts(),
    })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: ({ policy, accounts }) => {
          this.accounts = accounts;
          if (this.sessionId === null) {
            this.existingPolicy = policy !== null;
          }
          const draft = policy ? this.fromPolicy(policy) : this.newDraft();
          this.draft = this.deferredSave ? this.highlightDraft(draft) : draft;
          this.syncDependentControls();
          this.loading = false;
          this.loadCategories();
          this.loadCovers();
          this.loadCollections();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.loading = false;
          this.error = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.replaceCoverPreview(null);
  }

  get activeAccounts(): readonly BiliAccount[] {
    return this.accounts.filter((account) => account.state === 'active');
  }

  get targetRoomIds(): readonly number[] {
    const values = this.roomIds.length > 0 ? this.roomIds : [this.roomId];
    return [...new Set(values.filter((roomId) => roomId > 0))];
  }

  get modalTitle(): string {
    if (this.deferredSave) {
      return `片段投稿设置 · ${this.roomName}`;
    }
    if (this.sessionId !== null) {
      return `本场投稿设置 · ${this.roomName} · ${this.roomId}`;
    }
    return this.targetRoomIds.length > 1
      ? `批量投稿设置 · ${this.targetRoomIds.length} 个房间`
      : `投稿设置 · ${this.roomName} · ${this.roomId}`;
  }

  get categories(): UploadCategoryNode[] {
    return this.catalog?.categories ?? [];
  }

  get creationStatements(): readonly UploadCreationStatement[] {
    return this.catalog?.creationStatements ?? [];
  }

  get collections(): readonly BiliCollection[] {
    return this.collectionCatalog?.collections ?? [];
  }

  get selectableCollectionSections(): readonly {
    value: string;
    label: string;
  }[] {
    return this.collections.flatMap((collection) =>
      collection.selectable
        ? collection.sections.map((section) => ({
            value: `${collection.id}:${section.id}`,
            label: `${collection.title} / ${section.title}`,
          }))
        : [],
    );
  }

  get selectedCover(): CoverAsset | null {
    return (
      this.coverAssets.find((asset) => asset.id === this.draft.coverAssetId) ??
      null
    );
  }

  get isRepost(): boolean {
    return this.draft.creationStatementId === -2;
  }

  get selectedCategoryDescription(): string {
    const tid = this.draft.tid;
    if (tid === null) {
      return '';
    }
    for (const parent of this.categories) {
      const child = parent.children.find((item) => item.id === tid);
      if (child) {
        return child.description;
      }
    }
    return '';
  }

  get categoryRecommendation(): CategoryRecommendation | null {
    const candidates = [
      this.liveAreaName,
      ...(LIVE_CATEGORY_ALIASES[this.liveAreaName] ?? []),
      ...(LIVE_CATEGORY_ALIASES[this.liveParentAreaName] ?? []),
      this.liveParentAreaName,
    ]
      .map((value) => value.trim())
      .filter(Boolean);
    for (const candidate of candidates) {
      for (const parent of this.categories) {
        const child = parent.children.find((item) => item.name === candidate);
        if (child) {
          return {
            path: [parent.id, child.id],
            label: `${parent.name} / ${child.name}`,
          };
        }
      }
    }
    return null;
  }

  get allowReplies(): boolean {
    return !this.draft.upCloseReply;
  }

  get allowDanmaku(): boolean {
    return !this.draft.upCloseDanmu;
  }

  get retentionUsesDays(): boolean {
    return (
      this.draft.retentionMode === 'upload_completed' ||
      this.draft.retentionMode === 'submitted' ||
      this.draft.retentionMode === 'approved'
    );
  }

  get validationErrors(): PolicyValidationErrors {
    return this.saveAttempted ? this.validateDraft() : {};
  }

  get saveDisabled(): boolean {
    return (
      this.loading ||
      this.saving ||
      this.coverUploading ||
      this.creatingCollection
    );
  }

  accountModeChanged(mode: UploadAccountMode): void {
    this.draft.accountMode = mode;
    this.draft.accountId =
      mode === 'primary' ? null : (this.activeAccounts[0]?.id ?? null);
    this.clearCategorySelectionForAccountChange();
    this.loadCategories();
    this.clearCollectionSelectionForAccountChange();
    this.loadCollections();
  }

  fixedAccountChanged(accountId: number | null): void {
    this.draft.accountId = accountId;
    this.clearCategorySelectionForAccountChange();
    this.loadCategories();
    this.clearCollectionSelectionForAccountChange();
    this.loadCollections();
  }

  categoryChanged(path: number[] | null): void {
    this.categoryPath = path ?? [];
    this.draft.tid =
      this.categoryPath.length === 2 ? this.categoryPath[1] : null;
  }

  applyCategoryRecommendation(): void {
    const recommendation = this.categoryRecommendation;
    if (!recommendation) {
      return;
    }
    this.categoryPath = [...recommendation.path];
    this.draft.tid = recommendation.path[1];
    this.changeDetector.markForCheck();
  }

  creationStatementChanged(statementId: number): void {
    this.draft.creationStatementId = statementId;
    if (statementId === -2) {
      this.draft.originalAuthorization = false;
    }
  }

  coverModeChanged(mode: UploadCoverMode): void {
    this.draft.coverMode = mode;
    this.draft.coverAssetId =
      mode === 'custom'
        ? (this.draft.coverAssetId ?? this.coverAssets[0]?.id ?? null)
        : null;
    this.loadSelectedCoverPreview();
  }

  customCoverChanged(assetId: number | null): void {
    this.draft.coverAssetId = assetId;
    this.loadSelectedCoverPreview();
  }

  coverFileSelected(event: Event): void {
    this.uploadCoverFile(event, 'manuscript');
  }

  collectionCoverFileSelected(event: Event): void {
    this.uploadCoverFile(event, 'collection');
  }

  private uploadCoverFile(
    event: Event,
    target: 'manuscript' | 'collection',
  ): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.item(0) ?? null;
    input.value = '';
    if (file === null || this.coverUploading) {
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      const message = '封面不能超过 2 MiB。';
      if (target === 'collection') {
        this.newCollectionError = message;
      } else {
        this.coverError = message;
      }
      this.changeDetector.markForCheck();
      return;
    }
    this.coverUploading = true;
    if (target === 'collection') {
      this.newCollectionError = null;
    } else {
      this.coverError = null;
    }
    this.policyService
      .uploadCover(file)
      .pipe(
        finalize(() => {
          this.coverUploading = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (asset) => {
          this.coverAssets = [
            asset,
            ...this.coverAssets.filter((item) => item.id !== asset.id),
          ];
          if (target === 'collection') {
            this.newCollectionCoverAssetId = asset.id;
          } else {
            this.draft.coverMode = 'custom';
            this.draft.coverAssetId = asset.id;
            this.loadSelectedCoverPreview();
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (target === 'collection') {
            this.newCollectionError = this.errorMessage(error);
          } else {
            this.coverError = this.errorMessage(error);
          }
          this.changeDetector.markForCheck();
        },
      });
  }

  collectionChanged(selection: string | null): void {
    this.collectionSelection = selection;
    if (selection === null) {
      this.draft.collectionSeasonId = null;
      this.draft.collectionSectionId = null;
      return;
    }
    const match = /^(\d+):(\d+)$/.exec(selection);
    this.draft.collectionSeasonId = match ? Number(match[1]) : null;
    this.draft.collectionSectionId = match ? Number(match[2]) : null;
  }

  publishModeChanged(mode: PublishMode): void {
    this.publishMode = mode;
    if (mode === 'immediate') {
      this.draft.publishDelaySeconds = 0;
    } else if (this.publishDelayHours < 2) {
      this.publishDelayHours = 2;
    }
  }

  openCreateCollection(): void {
    this.newCollectionTitle = '';
    this.newCollectionDescription = '';
    this.newCollectionCoverAssetId = this.coverAssets[0]?.id ?? null;
    this.newCollectionError = null;
    this.newCollectionVisible = true;
  }

  closeCreateCollection(): void {
    if (!this.creatingCollection && !this.coverUploading) {
      this.newCollectionVisible = false;
    }
  }

  createCollection(): void {
    const title = this.newCollectionTitle.trim();
    const coverAssetId = this.newCollectionCoverAssetId;
    if (
      !title ||
      coverAssetId === null ||
      this.creatingCollection ||
      this.coverUploading
    ) {
      if (this.coverUploading) {
        return;
      }
      this.newCollectionError = !title
        ? '请填写合集名称。'
        : '请选择一张手动上传的合集封面。';
      this.changeDetector.markForCheck();
      return;
    }
    this.creatingCollection = true;
    this.newCollectionError = null;
    this.policyService
      .createCollection({
        accountMode: this.draft.accountMode,
        accountId:
          this.draft.accountMode === 'fixed' ? this.draft.accountId : null,
        title,
        description: this.newCollectionDescription.trim(),
        coverAssetId,
      })
      .pipe(
        finalize(() => {
          this.creatingCollection = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (result) => {
          this.newCollectionVisible = false;
          this.message.success(
            result.collection.selectable
              ? '合集已创建'
              : '合集已提交 B 站审核，通过后即可选择',
          );
          this.loadCollections();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.newCollectionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  allowRepliesChanged(allowed: boolean): void {
    this.draft.upCloseReply = !allowed;
    if (!allowed) {
      this.draft.upSelectionReply = false;
      this.draft.autoComment = false;
    }
  }

  allowDanmakuChanged(allowed: boolean): void {
    this.draft.upCloseDanmu = !allowed;
    if (!allowed) {
      this.draft.danmakuBackfill = false;
    }
  }

  refreshCategories(): void {
    this.loadCategories(true);
  }

  refreshCollections(): void {
    this.loadCollections();
  }

  save(): void {
    if (this.loading || this.saving) {
      return;
    }
    this.saveAttempted = true;
    this.error = null;
    const validationErrors = this.validationErrors;
    const tid = this.draft.tid;
    if (Object.keys(validationErrors).length > 0 || tid === null) {
      this.changeDetector.markForCheck();
      return;
    }
    const request: RoomUploadPolicyRequest = {
      accountMode: this.draft.accountMode,
      accountId:
        this.draft.accountMode === 'fixed' ? this.draft.accountId : null,
      enabled:
        this.sessionId === null && !this.deferredSave
          ? this.draft.enabled
          : true,
      titleTemplate: this.draft.titleTemplate.trim(),
      descriptionTemplate: this.draft.descriptionTemplate.trim(),
      partTitleTemplate: this.deferredSave
        ? this.draft.titleTemplate.trim()
        : this.draft.partTitleTemplate.trim(),
      dynamicTemplate: this.draft.dynamicTemplate.trim(),
      tid,
      tags: this.draft.tags.trim(),
      creationStatementId: this.draft.creationStatementId,
      originalAuthorization: this.draft.originalAuthorization,
      source: this.draft.source.trim(),
      isOnlySelf: this.draft.isOnlySelf,
      publishDynamic: this.draft.publishDynamic,
      upSelectionReply: this.draft.upSelectionReply,
      upCloseReply: this.draft.upCloseReply,
      upCloseDanmu: this.draft.upCloseDanmu,
      autoComment: this.draft.autoComment,
      danmakuBackfill: this.draft.danmakuBackfill,
      filters: { ...this.draft.filters },
      collectionSeasonId: this.draft.collectionSeasonId,
      collectionSectionId: this.draft.collectionSectionId,
      coverMode: this.draft.coverMode,
      coverAssetId:
        this.draft.coverMode === 'custom' ? this.draft.coverAssetId : null,
      publishDelaySeconds:
        this.publishMode === 'scheduled'
          ? Math.round(this.publishDelayHours * 3600)
          : 0,
      retentionMode: this.deferredSave ? 'never' : this.draft.retentionMode,
      retentionDays: this.deferredSave ? 0 : this.draft.retentionDays,
    };
    if (this.deferredSave) {
      this.settingsConfirmed.emit(request);
      this.visible = false;
      this.changeDetector.markForCheck();
      return;
    }
    this.saving = true;
    this.error = null;
    const targetRoomIds = this.targetRoomIds;
    const operation: Observable<unknown> =
      this.sessionId !== null
        ? this.submissionService.save(this.sessionId, request)
        : targetRoomIds.length === 1
          ? this.policyService.save(targetRoomIds[0], request)
          : this.policyService.saveMany(targetRoomIds, request);
    operation
      .pipe(
        finalize(() => {
          this.saving = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.existingPolicy = true;
          this.message.success(
            this.sessionId !== null
              ? '本场投稿设置已保存'
              : targetRoomIds.length > 1
                ? `已保存 ${targetRoomIds.length} 个房间的投稿设置`
                : `房间 ${this.roomId} 的投稿设置已保存`,
          );
          this.saved.emit();
          this.visible = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.error = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  close(): void {
    if (!this.saving) {
      this.visible = false;
    }
  }

  restoreInherited(): void {
    if (this.sessionId === null || !this.existingPolicy || this.saving) {
      return;
    }
    this.saving = true;
    this.error = null;
    this.submissionService
      .clear(this.sessionId)
      .pipe(
        finalize(() => {
          this.saving = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: () => {
          this.message.success('已恢复跟随房间投稿设置');
          this.saved.emit();
          this.visible = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.error = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadPolicy(): Observable<RoomUploadPolicyRequest | null> {
    if (this.sessionId !== null) {
      return this.submissionService.get(this.sessionId).pipe(
        map((value) => {
          this.existingPolicy = !value.inherited;
          return value.settings;
        }),
      );
    }
    return this.policyService.get(this.roomId).pipe(
      catchError((error: unknown) => {
        if (error instanceof HttpErrorResponse && error.status === 404) {
          return of(null);
        }
        return throwError(() => error);
      }),
    );
  }

  private loadCategories(forceRefresh = false): void {
    if (this.draft.accountMode === 'fixed' && this.draft.accountId === null) {
      this.setCategoryCatalog(null);
      this.categoryError = '请选择一个可用的固定投稿账号。';
      this.loading = false;
      this.changeDetector.markForCheck();
      return;
    }
    this.categoryLoading = true;
    this.categoryError = null;
    this.policyService
      .categories(this.draft.accountMode, this.draft.accountId, forceRefresh)
      .pipe(
        finalize(() => {
          this.categoryLoading = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (catalog) => {
          this.setCategoryCatalog(catalog);
          this.syncCategoryPath();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.setCategoryCatalog(null);
          this.categoryError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadCovers(): void {
    this.coverLoading = true;
    this.coverError = null;
    this.policyService
      .covers()
      .pipe(
        finalize(() => {
          this.coverLoading = false;
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (assets) => {
          this.coverAssets = assets;
          this.loadSelectedCoverPreview();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.coverAssets = [];
          this.coverError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadCollections(): void {
    const generation = ++this.collectionLoadGeneration;
    if (this.draft.accountMode === 'fixed' && this.draft.accountId === null) {
      this.collectionCatalog = null;
      this.collectionError = '请选择一个可用的固定投稿账号。';
      this.collectionLoading = false;
      this.changeDetector.markForCheck();
      return;
    }
    this.collectionLoading = true;
    this.collectionError = null;
    this.policyService
      .collections(this.draft.accountMode, this.draft.accountId)
      .pipe(
        finalize(() => {
          if (generation === this.collectionLoadGeneration) {
            this.collectionLoading = false;
            this.changeDetector.markForCheck();
          }
        }),
        takeUntil(this.destroy$),
      )
      .subscribe({
        next: (catalog) => {
          if (generation !== this.collectionLoadGeneration) {
            return;
          }
          this.collectionCatalog = catalog;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (generation !== this.collectionLoadGeneration) {
            return;
          }
          this.collectionCatalog = null;
          this.collectionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadSelectedCoverPreview(): void {
    const assetId =
      this.draft.coverMode === 'custom' ? this.draft.coverAssetId : null;
    const generation = ++this.coverPreviewGeneration;
    if (assetId === null) {
      this.replaceCoverPreview(null);
      return;
    }
    this.policyService
      .coverContent(assetId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (blob) => {
          if (generation !== this.coverPreviewGeneration) {
            return;
          }
          this.replaceCoverPreview(URL.createObjectURL(blob));
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (generation !== this.coverPreviewGeneration) {
            return;
          }
          this.replaceCoverPreview(null);
          this.coverError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private replaceCoverPreview(url: string | null): void {
    if (this.coverPreviewObjectUrl !== null) {
      URL.revokeObjectURL(this.coverPreviewObjectUrl);
    }
    this.coverPreviewObjectUrl = url;
    this.selectedCoverPreviewUrl = url;
  }

  private syncCategoryPath(): void {
    const tid = this.draft.tid;
    this.categoryPath = [];
    if (tid === null) {
      return;
    }
    for (const parent of this.categories) {
      if (parent.children.some((child) => child.id === tid)) {
        this.categoryPath = [parent.id, tid];
        return;
      }
    }
  }

  private clearCategorySelectionForAccountChange(): void {
    this.setCategoryCatalog(null);
    this.categoryPath = [];
    this.draft.tid = null;
  }

  private setCategoryCatalog(catalog: UploadCategoryCatalog | null): void {
    this.catalog = catalog;
    this.categoryOptions = (catalog?.categories ?? []).map((parent) => ({
      value: parent.id,
      label: parent.name,
      isLeaf: false,
      children: parent.children.map((child) => ({
        value: child.id,
        label: child.name,
        isLeaf: true,
      })),
    }));
  }

  private clearCollectionSelectionForAccountChange(): void {
    this.collectionCatalog = null;
    this.collectionSelection = null;
    this.draft.collectionSeasonId = null;
    this.draft.collectionSectionId = null;
  }

  private validateDraft(): PolicyValidationErrors {
    const errors: PolicyValidationErrors = {};
    if (this.draft.accountMode === 'fixed' && this.draft.accountId === null) {
      errors.account = '请选择投稿账号';
    }
    if (!this.draft.titleTemplate.trim()) {
      errors.title = this.deferredSave ? '请填写稿件标题' : '请填写标题模板';
    }
    if (!this.deferredSave && !this.draft.partTitleTemplate.trim()) {
      errors.partTitle = '请填写分 P 标题模板';
    }
    if (
      this.draft.tid === null ||
      this.categoryPath.length !== 2 ||
      this.categoryPath[1] !== this.draft.tid
    ) {
      errors.category = '请选择投稿分区';
    }
    if (!this.draft.tags.trim()) {
      errors.tags = '请填写至少一个标签';
    }
    if (
      !this.creationStatements.some(
        (statement) => statement.id === this.draft.creationStatementId,
      )
    ) {
      errors.creationStatement = '请选择创作声明';
    }
    if (this.isRepost && !this.draft.source.trim()) {
      errors.source = '转载稿件必须填写来源';
    }
    if (
      (this.draft.collectionSeasonId === null) !==
      (this.draft.collectionSectionId === null)
    ) {
      errors.collection = '请重新选择合集';
    }
    if (this.draft.coverMode === 'custom' && this.draft.coverAssetId === null) {
      errors.cover = '请选择或上传一张封面';
    }
    if (
      this.publishMode === 'scheduled' &&
      (!Number.isInteger(this.publishDelayHours) ||
        this.publishDelayHours < 2 ||
        this.publishDelayHours > 360)
    ) {
      errors.schedule = '定时发布需设置为 2～360 个整小时';
    }
    if (
      !this.deferredSave &&
      (!Number.isInteger(this.draft.retentionDays) ||
        this.draft.retentionDays < 0 ||
        this.draft.retentionDays > 3650)
    ) {
      errors.retention = '保留天数需填写 0～3650 的整数';
    }
    return errors;
  }

  private newDraft(): RoomUploadPolicyDraft {
    return { ...DEFAULT_DRAFT, filters: {} };
  }

  private highlightDraft(draft: RoomUploadPolicyDraft): RoomUploadPolicyDraft {
    const title = this.roomName.trim();
    return {
      ...draft,
      enabled: true,
      titleTemplate: title,
      partTitleTemplate: title,
      retentionMode: 'never',
      retentionDays: 0,
    };
  }

  private fromPolicy(policy: RoomUploadPolicyRequest): RoomUploadPolicyDraft {
    return {
      accountMode: policy.accountMode,
      accountId: policy.accountId,
      enabled: policy.enabled,
      titleTemplate: policy.titleTemplate,
      descriptionTemplate: policy.descriptionTemplate,
      partTitleTemplate: policy.partTitleTemplate,
      dynamicTemplate: policy.dynamicTemplate,
      tid: policy.tid,
      tags: policy.tags,
      creationStatementId: policy.creationStatementId,
      originalAuthorization: policy.originalAuthorization,
      source: policy.source,
      isOnlySelf: policy.isOnlySelf,
      publishDynamic: policy.publishDynamic,
      upSelectionReply: policy.upSelectionReply,
      upCloseReply: policy.upCloseReply,
      upCloseDanmu: policy.upCloseDanmu,
      autoComment: policy.autoComment,
      danmakuBackfill: policy.danmakuBackfill,
      filters: { ...policy.filters },
      collectionSeasonId: policy.collectionSeasonId,
      collectionSectionId: policy.collectionSectionId,
      coverMode: policy.coverMode,
      coverAssetId: policy.coverAssetId,
      publishDelaySeconds: policy.publishDelaySeconds,
      retentionMode: policy.retentionMode,
      retentionDays: policy.retentionDays,
    };
  }

  private syncDependentControls(): void {
    const seasonId = this.draft.collectionSeasonId;
    const sectionId = this.draft.collectionSectionId;
    this.collectionSelection =
      seasonId !== null && sectionId !== null
        ? `${seasonId}:${sectionId}`
        : null;
    this.publishMode =
      this.draft.publishDelaySeconds > 0 ? 'scheduled' : 'immediate';
    this.publishDelayHours =
      this.draft.publishDelaySeconds > 0
        ? this.draft.publishDelaySeconds / 3600
        : 2;
  }

  private errorMessage(error: unknown): string {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail;
      if (typeof detail === 'string' && detail) {
        return detail;
      }
      return error.message || '请求失败，请稍后重试';
    }
    return error instanceof Error ? error.message : '请求失败，请稍后重试';
  }
}
