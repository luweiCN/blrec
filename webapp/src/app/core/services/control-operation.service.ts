import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { EMPTY, Observable, timer } from 'rxjs';
import { concatMap, expand, takeWhile } from 'rxjs/operators';

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
    return this.get(operationId).pipe(
      expand((operation) =>
        operation.status === 'succeeded' || operation.status === 'failed'
          ? EMPTY
          : timer(intervalMilliseconds).pipe(
              concatMap(() => this.get(operationId))
            )
      ),
      takeWhile(
        (operation) =>
          operation.status !== 'succeeded' && operation.status !== 'failed',
        true
      )
    );
  }
}
