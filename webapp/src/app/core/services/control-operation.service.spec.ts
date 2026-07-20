import { fakeAsync, TestBed, tick } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';

import { UrlService } from './url.service';
import { ControlOperationService } from './control-operation.service';

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
});
