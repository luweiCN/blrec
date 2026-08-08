import { fakeAsync, TestBed, tick } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { of, Subject } from 'rxjs';

import { UrlService } from './url.service';
import {
  ControlOperation,
  ControlOperationService,
} from './control-operation.service';

describe('ControlOperationService', () => {
  let service: ControlOperationService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        { provide: UrlService, useValue: { makeApiUrl: (path: string) => path } },
      ],
    });
    service = TestBed.inject(ControlOperationService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('polls an accepted operation until its terminal result', fakeAsync(() => {
    const statuses: string[] = [];
    service.poll('operation-1', 10).subscribe((value) => {
      statuses.push(value.status);
    });

    tick();
    http.expectOne('/api/v1/control-operations/operation-1').flush({
      id: 'operation-1',
      status: 'running',
      steps: [],
    });
    tick(10);
    http.expectOne('/api/v1/control-operations/operation-1').flush({
      id: 'operation-1',
      status: 'succeeded',
      steps: [],
    });
    tick();

    expect(statuses).toEqual(['running', 'succeeded']);
  }));

  it('stops polling terminal failures', fakeAsync(() => {
    let completed = false;
    service.poll('operation-1', 10).subscribe({
      complete: () => {
        completed = true;
      },
    });
    tick();
    http.expectOne('/api/v1/control-operations/operation-1').flush({
      id: 'operation-1',
      status: 'failed',
      errorCode: 'TASK_LIFECYCLE_FAILED',
      steps: [],
    });
    tick(30);

    http.expectNone('/api/v1/control-operations/operation-1');
    expect(completed).toBeTrue();
  }));

  it('stops polling when the consumer unsubscribes before the deadline', fakeAsync(() => {
    const subscription = service.poll('operation-1', 10).subscribe();
    tick();
    http.expectOne('/api/v1/control-operations/operation-1').flush({
      id: 'operation-1',
      status: 'running',
      steps: [],
    });
    subscription.unsubscribe();
    tick(60_100);

    http.expectNone('/api/v1/control-operations/operation-1');
    expect(subscription.closed).toBeTrue();
  }));

  it('emits a stable failure and stops polling at the fixed deadline', fakeAsync(() => {
    const get = spyOn(service, 'get').and.returnValue(
      of<ControlOperation>(operation('running'))
    );
    const results: ControlOperation[] = [];
    let completed = false;
    service.poll('operation-1').subscribe({
      next: (result) => results.push(result),
      complete: () => {
        completed = true;
      },
    });

    tick(59_999);
    expect(results.at(-1)?.status).toBe('running');
    expect(completed).toBeFalse();

    tick(1);
    const callsAtDeadline = get.calls.count();

    expect(results.at(-1)?.status).toBe('failed');
    expect(results.at(-1)?.errorCode).toBe('CONTROL_OPERATION_POLL_TIMEOUT');
    expect(completed).toBeTrue();

    tick(1_000);
    expect(get.calls.count()).toBe(callsAtDeadline);
  }));

  it('waits for a slow response before scheduling the next request', fakeAsync(() => {
    const first = new Subject<ControlOperation>();
    const get = spyOn(service, 'get').and.returnValues(
      first,
      of<ControlOperation>(operation('succeeded'))
    );

    service.poll('operation-1', 10).subscribe();
    tick(50);
    expect(get).toHaveBeenCalledTimes(1);

    first.next(operation('running'));
    first.complete();
    tick(10);

    expect(get).toHaveBeenCalledTimes(2);
  }));
});

function operation(status: 'running' | 'succeeded'): ControlOperation {
  return {
    id: 'operation-1',
    lane: 'task-state',
    kind: 'start',
    targetKey: '100',
    attempt: 1,
    generation: 1,
    status,
    result: null,
    errorCode: null,
    createdAt: 1,
    updatedAt: 1,
    steps: [],
  };
}
