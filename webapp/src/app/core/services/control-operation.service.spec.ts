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

  it('stops polling when the consumer unsubscribes', fakeAsync(() => {
    const subscription = service.poll('operation-1', 10).subscribe();
    tick();
    http.expectOne('/api/v1/control-operations/operation-1').flush({
      id: 'operation-1',
      status: 'running',
      steps: [],
    });
    subscription.unsubscribe();
    tick(30);

    http.expectNone('/api/v1/control-operations/operation-1');
    expect(subscription.closed).toBeTrue();
  }));

  it('does not silently complete while an operation is still running', fakeAsync(() => {
    const get = spyOn(service, 'get').and.returnValue(
      of<ControlOperation>(operation('running'))
    );
    let completed = false;
    const subscription = service.poll('operation-1', 10).subscribe({
      complete: () => {
        completed = true;
      },
    });

    tick(1_300);

    expect(get.calls.count()).toBeGreaterThan(120);
    expect(completed).toBeFalse();
    subscription.unsubscribe();
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
