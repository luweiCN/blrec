import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
} from '@angular/core';

import { forkJoin } from 'rxjs';
import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import { BiliAccount } from '../../uploads/shared/bili-account.model';
import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import { RecordingSessionService } from '../shared/recording-session.service';

interface TaskDraft {
  accountId: number | null;
  title: string;
  description: string;
  dynamic: string;
  tid: number | null;
  tags: string;
  publishDynamic: boolean;
  isOnlySelf: boolean;
  autoComment: boolean;
  danmakuBackfill: boolean;
  publishDelaySeconds: number;
}

@Component({
  selector: 'app-task-edit-dialog',
  templateUrl: './task-edit-dialog.component.html',
  styleUrls: ['./task-edit-dialog.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TaskEditDialogComponent implements OnChanges {
  @Input() visible = false;
  @Input() jobIds: readonly number[] = [];
  @Output() readonly closed = new EventEmitter<void>();
  @Output() readonly saved = new EventEmitter<void>();

  loading = false;
  submitting = false;
  errorMessage = '';
  accounts: readonly BiliAccount[] = [];
  draft: TaskDraft = this.emptyDraft();

  constructor(
    private sessions: RecordingSessionService,
    private biliAccounts: BiliAccountService,
    private message: NzMessageService,
    private changeDetector: ChangeDetectorRef
  ) {}

  ngOnChanges(changes: SimpleChanges): void {
    if (
      (changes['visible'] || changes['jobIds']) &&
      this.visible &&
      this.jobIds.length > 0
    ) {
      this.load();
    }
  }

  close(): void {
    if (!this.submitting) {
      this.closed.emit();
    }
  }

  save(): void {
    if (
      this.submitting ||
      this.draft.accountId === null ||
      !this.draft.title.trim() ||
      this.draft.tid === null ||
      !this.draft.tags.trim()
    ) {
      return;
    }
    this.submitting = true;
    this.errorMessage = '';
    const changes = {
      title: this.draft.title.trim(),
      description: this.draft.description,
      dynamic: this.draft.dynamic,
      tid: this.draft.tid,
      tags: this.draft.tags.trim(),
      publish_dynamic: this.draft.publishDynamic,
      is_only_self: this.draft.isOnlySelf,
      auto_comment: this.draft.autoComment,
      danmaku_backfill: this.draft.danmakuBackfill,
      publish_delay_seconds: this.draft.publishDelaySeconds,
    };
    forkJoin(
      this.jobIds.map((jobId) =>
        this.sessions.updateTaskSettings(
          jobId,
          this.draft.accountId as number,
          changes
        )
      )
    )
      .pipe(
        finalize(() => {
          this.submitting = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (results) => {
          if (results.some((result) => result.collectionCleared)) {
            this.message.warning('切换投稿账号后，原账号的合集选择已清空');
          } else {
            this.message.success(
              this.jobIds.length > 1 ? '任务设置已批量更新' : '任务设置已更新'
            );
          }
          this.saved.emit();
        },
        error: (error: unknown) => {
          this.errorMessage = this.describeError(error);
        },
      });
  }

  private load(): void {
    this.loading = true;
    this.errorMessage = '';
    forkJoin({
      task: this.sessions.getTaskSettings(this.jobIds[0]),
      accounts: this.biliAccounts.listAccounts(),
    })
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: ({ task, accounts }) => {
          if (!task.editable) {
            this.errorMessage = task.blockedReason ?? '任务已经开始上传';
            return;
          }
          const settings = task.settings;
          this.accounts = accounts.filter((account) => account.state === 'active');
          this.draft = {
            accountId: task.accountId,
            title: this.text(settings['title']),
            description: this.text(settings['description']),
            dynamic: this.text(settings['dynamic']),
            tid: this.number(settings['tid']),
            tags: this.text(settings['tags']),
            publishDynamic: settings['publish_dynamic'] === true,
            isOnlySelf: settings['is_only_self'] === true,
            autoComment: settings['auto_comment'] === true,
            danmakuBackfill: settings['danmaku_backfill'] === true,
            publishDelaySeconds: this.number(settings['publish_delay_seconds']) ?? 0,
          };
        },
        error: (error: unknown) => {
          this.errorMessage = this.describeError(error);
        },
      });
  }

  private emptyDraft(): TaskDraft {
    return {
      accountId: null,
      title: '',
      description: '',
      dynamic: '',
      tid: null,
      tags: '',
      publishDynamic: false,
      isOnlySelf: false,
      autoComment: false,
      danmakuBackfill: false,
      publishDelaySeconds: 0,
    };
  }

  private text(value: unknown): string {
    return typeof value === 'string' ? value : '';
  }

  private number(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  private describeError(error: unknown): string {
    const value = error as { error?: { detail?: unknown }; message?: unknown };
    if (typeof value?.error?.detail === 'string') {
      return value.error.detail;
    }
    return typeof value?.message === 'string' ? value.message : '保存任务设置失败';
  }
}
