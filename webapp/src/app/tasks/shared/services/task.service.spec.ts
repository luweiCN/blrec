import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { TaskService } from './task.service';

describe('TaskService', () => {
  let service: TaskService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        { provide: UrlService, useValue: { makeApiUrl: (path: string) => path } },
      ],
    });
    service = TestBed.inject(TaskService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('submits one selected-room batch action', () => {
    service.runBatchAction('recorder_disable', [100, 200]).subscribe();

    const request = http.expectOne('/api/v1/tasks/actions');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      action: 'recorder_disable',
      roomIds: [100, 200],
    });
    request.flush({ results: [] });
  });

  it('keeps the operation identity returned by a 202 lifecycle admission', () => {
    let operationId: string | null | undefined;
    service.startTask(100).subscribe((response) => {
      operationId = response.operationId;
    });

    const request = http.expectOne('/api/v1/tasks/100/start');
    expect(request.request.method).toBe('POST');
    request.flush(
      {
        operationId: 'operation-1',
        status: 'accepted',
        results: [],
      },
      { status: 202, statusText: 'Accepted' }
    );

    expect(operationId).toBe('operation-1');
  });
});
