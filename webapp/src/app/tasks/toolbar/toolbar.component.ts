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
import { NzModalService } from 'ng-zorro-antd/modal';
import { NzMessageService } from 'ng-zorro-antd/message';

import { DataSelection } from '../shared/task.model';
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

  @Input() dateRange: Date[] | null = null;
  @Output() dateRangeChange = new EventEmitter<Date[] | null>();

  destroyed = new Subject<void>();

  private filterTerms = new Subject<string>();

  readonly selections = [
    { label: '全部', value: DataSelection.ALL },
    { label: '录制中', value: DataSelection.RECORDING },
    { label: '录制开', value: DataSelection.RECORDER_ENABLED },
    { label: '录制关', value: DataSelection.RECORDER_DISABLED },
    { label: '运行', value: DataSelection.MONITOR_ENABLED },
    { label: '停止', value: DataSelection.MONITOR_DISABLED },
    { label: '直播', value: DataSelection.LIVING },
    { label: '轮播', value: DataSelection.ROUNDING },
    { label: '闲置', value: DataSelection.PREPARING },
  ];

  constructor(
    private message: NzMessageService,
    private modal: NzModalService,
    private clipboard: Clipboard,
    private taskManager: TaskManagerService
  ) {}

  ngOnInit(): void {
    this.filterTerms
      .pipe(
        debounceTime(300),
        distinctUntilChanged(),
        takeUntil(this.destroyed)
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

  onDateRangeChanged(value: Date[] | null): void {
    this.dateRange = value;
    this.dateRangeChange.emit(value);
  }

  toggleReverse(): void {
    this.reverse = !this.reverse;
    this.reverseChange.emit(this.reverse);
  }

  removeAllTasks(): void {
    this.modal.confirm({
      nzTitle: '确定要删除全部任务？',
      nzContent: '正在录制的任务将被强制停止！任务删除后将不可恢复！',
      nzOnOk: () =>
        new Promise((resolve, reject) => {
          this.taskManager.removeAllTasks().subscribe(resolve, reject);
        }),
    });
  }

  startAllTasks(): void {
    this.taskManager.startAllTasks().subscribe();
  }

  stopAllTasks(force: boolean = false): void {
    if (force) {
      this.modal.confirm({
        nzTitle: '确定要强制停止全部任务？',
        nzContent: '正在录制的文件会被强行中断！确定要放弃正在录制的文件？',
        nzOnOk: () =>
          new Promise((resolve, reject) => {
            this.taskManager.stopAllTasks(force).subscribe(resolve, reject);
          }),
      });
    } else {
      this.taskManager.stopAllTasks().subscribe();
    }
  }

  disableAllRecorders(force: boolean = false): void {
    if (force) {
      this.modal.confirm({
        nzTitle: '确定要强制关闭全部任务的录制？',
        nzContent: '正在录制的文件会被强行中断！确定要放弃正在录制的文件？',
        nzOnOk: () =>
          new Promise((resolve, reject) => {
            this.taskManager
              .disableAllRecorders(force)
              .subscribe(resolve, reject);
          }),
      });
    } else {
      this.taskManager.disableAllRecorders().subscribe();
    }
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
        })
      )
      .subscribe(
        () => {
          this.message.success('全部房间号已复制到剪切板');
        },
        (error) => {
          this.message.error('复制全部房间号到剪切板出错', error);
        }
      );
  }
}
