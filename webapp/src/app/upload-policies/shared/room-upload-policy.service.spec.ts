import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { RoomUploadPolicyService } from './room-upload-policy.service';

describe('RoomUploadPolicyService', () => {
  let service: RoomUploadPolicyService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        RoomUploadPolicyService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(RoomUploadPolicyService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists room upload policies', () => {
    service.list().subscribe();

    const request = http.expectOne('/api/v1/room-upload-policies');
    expect(request.request.method).toBe('GET');
    request.flush([]);
  });

  it('saves one room policy without changing existing jobs', () => {
    const payload = {
      accountMode: 'primary' as const,
      accountId: null,
      enabled: true,
      titleTemplate: '{{ title }} 录播',
      descriptionTemplate: '',
      tid: 17,
      tags: '直播,录播',
      copyright: 1 as const,
      source: '',
      autoComment: false,
      danmakuBackfill: false,
      filters: {},
    };

    service.save(100, payload).subscribe();

    const request = http.expectOne('/api/v1/room-upload-policies/100');
    expect(request.request.method).toBe('PUT');
    expect(request.request.body).toEqual(payload);
    request.flush({ roomId: 100, ...payload });
  });

  it('deletes only the future policy for one room', () => {
    service.delete(100).subscribe();

    const request = http.expectOne('/api/v1/room-upload-policies/100');
    expect(request.request.method).toBe('DELETE');
    request.flush(null);
  });
});
