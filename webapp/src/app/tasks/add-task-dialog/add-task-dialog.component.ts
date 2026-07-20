import {
  Component,
  ChangeDetectionStrategy,
  EventEmitter,
  Input,
  Output,
  ChangeDetectorRef,
  OnDestroy,
} from '@angular/core';
import {
  FormBuilder,
  FormControl,
  FormGroup,
  Validators,
} from '@angular/forms';

import { Subject } from 'rxjs';
import { takeUntil, tap } from 'rxjs/operators';

import {
  TaskManagerService,
  AddTaskResultMessage,
} from '../shared/services/task-manager.service';

const ROOM_URL_PATTERN = /^https?:\/\/live\.bilibili\.com\/(\d+).*$/;
const INPUT_PATTERN =
  /^\s*(?:\d+(?:[ ]+\d+)*|https?:\/\/live\.bilibili\.com\/\d+.*)\s*$/;

@Component({
  selector: 'app-add-task-dialog',
  templateUrl: './add-task-dialog.component.html',
  styleUrls: ['./add-task-dialog.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AddTaskDialogComponent implements OnDestroy {
  @Input() visible = false;
  @Output() visibleChange = new EventEmitter<boolean>();

  pending = false;
  resultMessages: AddTaskResultMessage[] = [];

  readonly formGroup: FormGroup;
  readonly pattern = INPUT_PATTERN;
  private readonly destroy$ = new Subject<void>();

  constructor(
    formBuilder: FormBuilder,
    private changeDetector: ChangeDetectorRef,
    private taskManager: TaskManagerService
  ) {
    this.formGroup = formBuilder.group({
      input: ['', [Validators.required, Validators.pattern(this.pattern)]],
    });
  }

  get inputControl() {
    return this.formGroup.get('input') as FormControl;
  }

  open(): void {
    this.setVisible(true);
  }

  close(): void {
    this.resultMessages = [];
    this.reset();
    this.setVisible(false);
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.visibleChange.emit(visible);
    this.changeDetector.markForCheck();
  }

  reset(): void {
    this.pending = false;
    this.formGroup.reset();
    this.changeDetector.markForCheck();
  }

  handleCancel(): void {
    this.close();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  handleConfirm(): void {
    this.pending = true;
    const inputValue = this.inputControl.value.trim() as string;

    let roomIds: readonly number[];
    if (inputValue.startsWith('http')) {
      roomIds = [parseInt(ROOM_URL_PATTERN.exec(inputValue)![1])];
    } else {
      roomIds = [...new Set(inputValue.split(/\s+/).map((s) => parseInt(s)))];
    }

    this.taskManager
      .addTasks(roomIds)
      .pipe(
        tap((resultMessage) => {
          this.resultMessages.push(resultMessage);
          if (resultMessage.type === 'info') {
            this.pending = false;
          }
          this.changeDetector.markForCheck();
        }),
        takeUntil(this.destroy$)
      )
      .subscribe({
        complete: () => {
          if (
            this.resultMessages.length > 0 &&
            this.resultMessages.every(
              (message) =>
                message.type === 'success' || message.type === 'info'
            )
          ) {
            this.close();
          } else {
            this.reset();
          }
        },
      });
  }
}
