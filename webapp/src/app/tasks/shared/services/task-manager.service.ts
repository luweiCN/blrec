import { HttpErrorResponse } from '@angular/common/http';
import { Injectable } from '@angular/core';

import {
  catchError,
  concatMap,
  filter,
  map,
  switchMap,
  tap,
} from 'rxjs/operators';
import { concat, Observable, of, Subject } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import { TaskService } from './task.service';
import { ResponseMessage } from 'src/app/shared/api.models';
import {
  TaskBatchAction,
  TaskBatchActionResponse,
  TaskData,
} from '../task.model';
import { ControlOperationService } from 'src/app/core/services/control-operation.service';
import type { ControlOperation } from 'src/app/core/services/control-operation.service';

export interface AddTaskResultMessage {
  type: 'success' | 'info' | 'warning' | 'error';
  message: string;
}

@Injectable({
  providedIn: 'root',
})
export class TaskManagerService {
  private readonly taskDataRefreshSubject = new Subject<TaskData[]>();
  readonly taskDataRefresh$ = this.taskDataRefreshSubject.asObservable();

  constructor(
    private message: NzMessageService,
    private taskService: TaskService,
    private controlOperations: ControlOperationService
  ) {}

  getAllTaskRoomIds(): Observable<number[]> {
    return this.taskService
      .getAllTaskData()
      .pipe(map((taskData) => taskData.map((data) => data.room_info.room_id)));
  }

  runBatchAction(
    action: TaskBatchAction,
    roomIds: readonly number[]
  ): Observable<TaskBatchActionResponse> {
    return this.taskService.runBatchAction(action, roomIds).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          if (response.status === 'running') {
            return;
          }
          if (response.status === 'accepted') {
            const acceptedCount = response.results.filter(
              (result) => result.accepted
            ).length;
            this.message.success(`已提交 ${acceptedCount} 个任务`);
            return;
          }
          const succeededCount = response.results.filter(
            (result) => result.status === 'succeeded'
          ).length;
          const failed = response.results.filter(
            (result) =>
              result.status === 'rejected' || result.status === 'failed'
          );
          if (failed.length > 0) {
            this.message.warning(
              `成功 ${succeededCount} 个，失败 ${failed.length} 个：${this.controlErrorMessage(
                failed[0].errorCode
              )}`
            );
          } else {
            this.message.success(`已完成 ${succeededCount} 个任务`);
          }
        },
        (error: HttpErrorResponse) => {
          this.message.error(`批量操作失败：${error.message}`);
        }
      )
    );
  }

  updateTaskInfo(roomId: number): Observable<ResponseMessage> {
    return this.taskService.updateTaskInfo(roomId).pipe(
      tap(
        () => {
          this.message.success(`[${roomId}] 成功刷新任务的数据`);
        },
        (error: HttpErrorResponse) => {
          this.message.error(
            `[${roomId}] 刷新任务的数据出错: ${error.message}`
          );
        }
      )
    );
  }

  updateAllTaskInfos(): Observable<ResponseMessage> {
    return this.taskService.updateAllTaskInfos().pipe(
      tap(
        () => {
          this.message.success('成功刷新全部任务的数据');
        },
        (error: HttpErrorResponse) => {
          this.message.error(`刷新全部任务的数据出错: ${error.message}`);
        }
      )
    );
  }

  addTask(roomId: number): Observable<AddTaskResultMessage> {
    return this.taskService.addTask(roomId).pipe(
      map((result) => {
        return {
          type: 'success',
          message: '成功添加任务',
        } as AddTaskResultMessage;
      }),
      catchError((error: HttpErrorResponse) => {
        let result: AddTaskResultMessage;
        if (error.status == 409) {
          result = {
            type: 'error',
            message: '任务已存在，不能重复添加。',
          };
        } else if (error.status == 403) {
          result = {
            type: 'warning',
            message: '任务数量超过限制，不能添加任务。',
          };
        } else if (error.status == 404) {
          result = {
            type: 'error',
            message: '直播间不存在',
          };
        } else {
          result = {
            type: 'error',
            message: `添加任务出错: ${error.message}`,
          };
        }
        return of(result);
      }),
      map((resultMessage) => {
        resultMessage.message = `${roomId}: ${resultMessage.message}`;
        return resultMessage;
      }),
      tap((resultMessage) => {
        this.message[resultMessage.type](resultMessage.message);
      })
    );
  }

  removeTask(roomId: number): Observable<ResponseMessage> {
    return this.taskService.removeTask(roomId).pipe(
      tap(
        () => {
          this.message.success(`[${roomId}] 任务已删除`);
        },
        (error: HttpErrorResponse) => {
          this.message.error(`[${roomId}] 删除任务出错: ${error.message}`);
        }
      )
    );
  }

  removeAllTasks(): Observable<ResponseMessage> {
    const messageId = this.message.loading('正在删除全部任务...', {
      nzDuration: 0,
    }).messageId;
    return this.taskService.removeAllTasks().pipe(
      tap(
        () => {
          this.message.remove(messageId);
          this.message.success('成功删除全部任务');
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`删除全部任务出错: ${error.message}`);
        }
      )
    );
  }

  startTask(roomId: number): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading(`[${roomId}] 正在运行任务...`, {
      nzDuration: 0,
    }).messageId;
    return this.taskService.startTask(roomId).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            `[${roomId}] 任务已提交`,
            `[${roomId}] 成功运行任务`
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`[${roomId}] 运行任务出错: ${error.message}`);
        }
      )
    );
  }

  startAllTasks(): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading('正在运行全部任务...', {
      nzDuration: 0,
    }).messageId;
    return this.taskService.startAllTasks().pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            '全部任务已提交',
            '成功运行全部任务'
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`运行全部任务出错: ${error.message}`);
        }
      )
    );
  }

  stopTask(
    roomId: number,
    force: boolean = false
  ): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading(`[${roomId}] 正在停止任务...`, {
      nzDuration: 0,
    }).messageId;
    return this.taskService.stopTask(roomId, force).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            `[${roomId}] 停止操作已提交`,
            `[${roomId}] 成功停止任务`
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`[${roomId}] 停止任务出错: ${error.message}`);
        }
      )
    );
  }

  stopAllTasks(force: boolean = false): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading('正在停止全部任务...', {
      nzDuration: 0,
    }).messageId;
    return this.taskService.stopAllTasks(force).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            '全部停止操作已提交',
            '成功停止全部任务'
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`停止全部任务出错: ${error.message}`);
        }
      )
    );
  }

  enableRecorder(roomId: number): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading(`[${roomId}] 正在开启录制...`, {
      nzDuration: 0,
    }).messageId;
    return this.taskService.enableTaskRecorder(roomId).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            `[${roomId}] 开启录制已提交`,
            `[${roomId}] 成功开启录制`
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`[${roomId}] 开启录制出错: ${error.message}`);
        }
      )
    );
  }

  /**
   * Deprecated!
   * Enable all tasks' recorder will cause some problems.
   * Tasks those monitor are disabled won't work as expected!
   */
  enableAllRecorders(): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading('正在开启全部任务的录制...', {
      nzDuration: 0,
    }).messageId;
    return this.taskService.enableAllRecorders().pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            '全部开启录制操作已提交',
            '成功开启全部任务的录制'
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`开启全部任务的录制出错: ${error.message}`);
        }
      )
    );
  }

  disableRecorder(
    roomId: number,
    force: boolean = false
  ): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading(`[${roomId}] 正在关闭录制...`, {
      nzDuration: 0,
    }).messageId;
    return this.taskService.disableTaskRecorder(roomId, force).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            `[${roomId}] 关闭录制已提交`,
            `[${roomId}] 成功关闭录制`
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`[${roomId}] 关闭录制出错: ${error.message}`);
        }
      )
    );
  }

  disableAllRecorders(
    force: boolean = false
  ): Observable<TaskBatchActionResponse> {
    const messageId = this.message.loading('正在关闭全部任务的录制...', {
      nzDuration: 0,
    }).messageId;
    return this.taskService.disableAllRecorders(force).pipe(
      switchMap((admission) => this.observeControl(admission)),
      tap(
        (response) => {
          this.notifyControl(
            response,
            messageId,
            '全部关闭录制操作已提交',
            '成功关闭全部任务的录制'
          );
        },
        (error: HttpErrorResponse) => {
          this.message.remove(messageId);
          this.message.error(`关闭全部任务的录制出错: ${error.message}`);
        }
      )
    );
  }

  canCutStream(roomId: number) {
    return this.taskService.canCutStream(roomId).pipe(
      tap((ableToCutStream) => {
        if (!ableToCutStream) {
          this.message.warning(`[${roomId}] 不支持文件切割~`);
        }
      })
    );
  }

  cutStream(roomId: number) {
    return this.taskService.cutStream(roomId).pipe(
      tap(
        () => {
          this.message.success(`[${roomId}] 文件切割已触发`);
        },
        (error: HttpErrorResponse) => {
          if (error.status == 403) {
            this.message.warning(`[${roomId}] 时长太短不能切割，请稍后再试。`);
          } else {
            this.message.error(`[${roomId}] 切割文件出错: ${error.message}`);
          }
        }
      )
    );
  }

  private observeControl(
    admission: TaskBatchActionResponse
  ): Observable<TaskBatchActionResponse> {
    if (
      !admission.operationId ||
      admission.status === 'succeeded' ||
      admission.status === 'failed'
    ) {
      return of(admission);
    }
    return concat(
      of(admission),
      this.controlOperations.poll(admission.operationId).pipe(
        map((operation) => this.operationResult(admission, operation)),
        filter(
          (result) =>
            result.status === 'succeeded' || result.status === 'failed'
        ),
        concatMap((result) => {
          return this.taskService.getAllTaskData().pipe(
            tap((tasks) => this.taskDataRefreshSubject.next(tasks)),
            map(() => result)
          );
        })
      )
    );
  }

  private notifyControl(
    response: TaskBatchActionResponse,
    loadingMessageId: string,
    submittedMessage: string,
    succeededMessage: string
  ): void {
    if (response.status === 'running') {
      return;
    }
    this.message.remove(loadingMessageId);
    if (response.status === 'accepted') {
      this.message.success(submittedMessage);
      return;
    }
    if (response.status === 'failed') {
      const failed = response.results.find(
        (result) => result.status === 'failed' || result.status === 'rejected'
      );
      this.message.error(this.controlErrorMessage(failed?.errorCode));
      return;
    }
    this.message.success(succeededMessage);
  }

  private controlErrorMessage(errorCode: string | null | undefined): string {
    if (errorCode === 'TASK_NOT_FOUND') {
      return '录制任务不存在';
    }
    if (errorCode === 'TASK_LIFECYCLE_FAILED') {
      return '任务状态切换失败';
    }
    return errorCode ? `任务操作失败（${errorCode}）` : '任务操作失败';
  }

  private operationResult(
    admission: TaskBatchActionResponse,
    operation: ControlOperation
  ): TaskBatchActionResponse {
    const admittedByRoom = new Map(
      admission.results.map((result) => [result.roomId, result])
    );
    return {
      operationId: operation.id,
      status: operation.status,
      results: operation.steps.map((step) => {
        const roomId = Number(step.key);
        const admitted = admittedByRoom.get(roomId);
        return {
          roomId,
          accepted: admitted?.accepted ?? step.status !== 'rejected',
          status: step.status,
          operationId: operation.id,
          errorCode: step.errorCode,
          message:
            step.status === 'succeeded'
              ? '操作已完成'
              : step.errorCode ?? admitted?.message ?? '操作处理中',
        };
      }),
    };
  }
}
