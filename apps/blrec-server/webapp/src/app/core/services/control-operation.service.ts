import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { defer, EMPTY, merge, Observable, timer } from 'rxjs';
import { concatMap, expand, map, takeWhile, tap } from 'rxjs/operators';

import { UrlService } from './url.service';

export type ControlOperationStatus =
  | 'accepted'
  | 'running'
  | 'succeeded'
  | 'failed';

export type ControlStepStatus =
  | 'queued'
  | 'rejected'
  | 'running'
  | 'succeeded'
  | 'failed';

export interface ControlOperationStep {
  readonly key: string;
  readonly generation: number;
  readonly status: ControlStepStatus;
  readonly result: Readonly<Record<string, unknown>> | null;
  readonly errorCode: string | null;
}

export interface ControlOperation {
  readonly id: string;
  readonly lane: string;
  readonly kind: string;
  readonly targetKey: string;
  readonly attempt: number;
  readonly generation: number;
  readonly status: ControlOperationStatus;
  readonly result: Readonly<Record<string, unknown>> | null;
  readonly errorCode: string | null;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly steps: readonly ControlOperationStep[];
}

const POLL_TIMEOUT_MILLISECONDS = 60_000;
const POLL_TIMEOUT_ERROR_CODE = 'CONTROL_OPERATION_POLL_TIMEOUT';

@Injectable({ providedIn: 'root' })
export class ControlOperationService {
  constructor(private http: HttpClient, private url: UrlService) {}

  get(operationId: string): Observable<ControlOperation> {
    const url = this.url.makeApiUrl(
      `/api/v1/control-operations/${operationId}`
    );
    return this.http.get<ControlOperation>(url);
  }

  poll(
    operationId: string,
    intervalMilliseconds: number = 500
  ): Observable<ControlOperation> {
    return defer(() => {
      let latest: ControlOperation | null = null;
      const polling = this.get(operationId).pipe(
        expand((operation) =>
          operation.status === 'succeeded' || operation.status === 'failed'
            ? EMPTY
            : timer(intervalMilliseconds).pipe(
                concatMap(() => this.get(operationId))
              )
        ),
        tap((operation) => (latest = operation))
      );
      const deadline = timer(POLL_TIMEOUT_MILLISECONDS).pipe(
        map(() => this.timeoutResult(operationId, latest))
      );
      return merge(polling, deadline).pipe(
        takeWhile(
          (operation) =>
            operation.status !== 'succeeded' && operation.status !== 'failed',
          true
        )
      );
    });
  }

  private timeoutResult(
    operationId: string,
    latest: ControlOperation | null
  ): ControlOperation {
    const now = Date.now() / 1000;
    return {
      id: latest?.id ?? operationId,
      lane: latest?.lane ?? '',
      kind: latest?.kind ?? '',
      targetKey: latest?.targetKey ?? '',
      attempt: latest?.attempt ?? 0,
      generation: latest?.generation ?? 0,
      status: 'failed',
      result: latest?.result ?? null,
      errorCode: POLL_TIMEOUT_ERROR_CODE,
      createdAt: latest?.createdAt ?? now,
      updatedAt: now,
      steps: (latest?.steps ?? []).map((step) =>
        step.status === 'queued' || step.status === 'running'
          ? { ...step, status: 'failed', errorCode: POLL_TIMEOUT_ERROR_CODE }
          : step
      ),
    };
  }
}
