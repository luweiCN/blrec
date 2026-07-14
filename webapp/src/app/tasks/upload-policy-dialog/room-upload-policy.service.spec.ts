import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { RoomUploadPolicyRequest } from './room-upload-policy.model';
import { RoomUploadPolicyService } from './room-upload-policy.service';

describe('RoomUploadPolicyService', () => {
  let service: RoomUploadPolicyService;
  let http: HttpTestingController;

  const payload: RoomUploadPolicyRequest = {
    accountMode: 'primary',
    accountId: null,
    enabled: true,
    titleTemplate: '{{ title }} 录播',
    descriptionTemplate: '主播：{{ anchor_name }}',
    partTitleTemplate: 'P{{ part_index }}',
    dynamicTemplate: '{{ title }} 录播',
    tid: 17,
    tags: '直播,录播',
    copyright: 1,
    source: '',
    isOnlySelf: false,
    publishDynamic: true,
    noReprint: true,
    upSelectionReply: false,
    upCloseReply: false,
    upCloseDanmu: false,
    autoComment: true,
    danmakuBackfill: true,
    filters: {},
  };

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

  it('loads only the requested room policy', () => {
    service.get(100).subscribe();

    const request = http.expectOne('/api/v1/room-upload-policies/100');
    expect(request.request.method).toBe('GET');
    request.flush({ roomId: 100 });
  });

  it('loads categories for the resolved account selection', () => {
    service.categories('fixed', 7, true).subscribe();

    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/room-upload-policies/categories' &&
        candidate.params.get('accountMode') === 'fixed' &&
        candidate.params.get('accountId') === '7' &&
        candidate.params.get('refresh') === 'true',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ categories: [] });
  });

  it('saves and deletes one room policy', () => {
    service.save(100, payload).subscribe();
    let request = http.expectOne('/api/v1/room-upload-policies/100');
    expect(request.request.method).toBe('PUT');
    expect(request.request.body).toEqual(payload);
    request.flush({ roomId: 100, ...payload });

    service.delete(100).subscribe();
    request = http.expectOne('/api/v1/room-upload-policies/100');
    expect(request.request.method).toBe('DELETE');
    request.flush(null);
  });
});
