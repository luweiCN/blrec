import {
  Component,
  OnInit,
  ChangeDetectionStrategy,
  Input,
  Output,
  EventEmitter,
  OnDestroy,
} from '@angular/core';
import { Clipboard } from '@angular/cdk/clipboard';

import { Subject } from 'rxjs';
import {
  debounceTime,
  distinctUntilChanged,
  takeUntil,
  map,
  tap,
} from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  AutomaticSubmissionFilter,
  DataSelection,
  SubmissionVisibilityFilter,
} from '../shared/task.model';
import { TaskManagerService } from '../shared/services/task-manager.service';

@Component({
  selector: 'app-toolbar',
  templateUrl: './toolbar.component.html',
  styleUrls: ['./toolbar.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ToolbarComponent implements OnInit, OnDestroy {
  @Input() selection!: DataSelection;
  @Output() selectionChange = new EventEmitter<DataSelection>();

  @Input() reverse!: boolean;
  @Output() reverseChange = new EventEmitter<boolean>();

  @Output() filterChange = new EventEmitter<string>();

  @Input() automaticSubmissionFilter: AutomaticSubmissionFilter = null;
  @Output() automaticSubmissionFilterChange =
    new EventEmitter<AutomaticSubmissionFilter>();

  @Input() submissionVisibilityFilter: SubmissionVisibilityFilter = null;
  @Output() submissionVisibilityFilterChange =
    new EventEmitter<SubmissionVisibilityFilter>();

  @Input() submissionAccountFilter: number | null = null;
  @Output() submissionAccountFilterChange = new EventEmitter<number | null>();

  @Input() submissionAccountOptions: readonly {
    label: string;
    value: number;
  }[] = [];

  destroyed = new Subject<void>();

  private filterTerms = new Subject<string>();

  readonly selections = [
    { label: '全部', value: DataSelection.ALL },
    { label: '录制中', value: DataSelection.RECORDING },
    { label: '监控已开启', value: DataSelection.MONITOR_ENABLED },
    { label: '监控已关闭', value: DataSelection.MONITOR_DISABLED },
    { label: '直播', value: DataSelection.LIVING },
    { label: '轮播', value: DataSelection.ROUNDING },
    { label: '闲置', value: DataSelection.PREPARING },
  ];
  readonly automaticSubmissionOptions = [
    { label: '全部投稿状态', value: null },
    { label: '自动投稿已开启', value: 'enabled' },
    { label: '自动投稿已关闭', value: 'disabled' },
    { label: '未设置投稿', value: 'unconfigured' },
  ];
  readonly submissionVisibilityOptions = [
    { label: '全部可见性', value: null },
    { label: '公开', value: 'public' },
    { label: '仅自己可见', value: 'private' },
  ];

  get accountFilterOptions(): {
    label: string;
    value: number | null;
  }[] {
    return [
      { label: '全部投稿账号', value: null },
      ...this.submissionAccountOptions,
    ];
  }

  constructor(
    private message: NzMessageService,
    private clipboard: Clipboard,
    private taskManager: TaskManagerService,
  ) {}

  ngOnInit(): void {
    this.filterTerms
      .pipe(
        debounceTime(300),
        distinctUntilChanged(),
        takeUntil(this.destroyed),
      )
      .subscribe((term) => {
        this.filterChange.emit(term);
      });
  }

  ngOnDestroy() {
    this.destroyed.next();
    this.destroyed.complete();
  }

  onFilterInput(term: string): void {
    this.filterTerms.next(term);
  }

  toggleReverse(): void {
    this.reverse = !this.reverse;
    this.reverseChange.emit(this.reverse);
  }

  updateAllTaskInfos(): void {
    this.taskManager.updateAllTaskInfos().subscribe();
  }

  copyAllTaskRoomIds(): void {
    this.taskManager
      .getAllTaskRoomIds()
      .pipe(
        map((ids) => ids.join(' ')),
        tap((text) => {
          if (!this.clipboard.copy(text)) {
            throw Error('Failed to copy text to the clipboard');
          }
        }),
      )
      .subscribe(
        () => {
          this.message.success('全部房间号已复制到剪切板');
        },
        (error) => {
          this.message.error('复制全部房间号到剪切板出错', error);
        },
      );
  }
}
