import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from '../core/services/url.service';
import { MediaLibraryService } from './media-library.service';

describe('MediaLibraryService', () => {
  let service: MediaLibraryService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        MediaLibraryService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(MediaLibraryService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists and favorites permanent broadcasts', () => {
    service.list('broadcast', 20, 40, ' 精选 ').subscribe();
    const list = http.expectOne(
      (request) =>
        request.url === '/api/v1/media-library' &&
        request.params.get('kind') === 'broadcast' &&
        request.params.get('limit') === '20' &&
        request.params.get('offset') === '40' &&
        request.params.get('q') === '精选',
    );
    expect(list.request.method).toBe('GET');
    list.flush({ total: 0, items: [] });

    service.favorite(7).subscribe();
    const favorite = http.expectOne('/api/v1/media-library/favorites/7');
    expect(favorite.request.method).toBe('POST');
    expect(favorite.request.body).toBeNull();
    favorite.flush({});
  });

  it('creates, streams, completes and updates a multipart import', () => {
    const request = {
      kind: 'broadcast' as const,
      displayName: '外部直播',
      note: '',
      tags: ['精选'],
      roomId: 0,
      anchorName: '',
      parts: [
        { filename: 'one.mp4', sizeBytes: 3 },
        { filename: 'two.mp4', sizeBytes: 3 },
      ],
    };
    service.createImport(request).subscribe();
    const create = http.expectOne('/api/v1/media-library/imports');
    expect(create.request.method).toBe('POST');
    expect(create.request.body).toEqual(request);
    create.flush({ id: 9 });

    const file = new File(['one'], 'one.mp4', { type: 'video/mp4' });
    service.uploadPart(9, 1, file).subscribe();
    const upload = http.expectOne('/api/v1/media-library/9/parts/1/content');
    expect(upload.request.method).toBe('PUT');
    expect(upload.request.body).toBe(file);
    expect(upload.request.reportProgress).toBeTrue();
    upload.flush({});

    service.completeImport(9).subscribe();
    const complete = http.expectOne('/api/v1/media-library/9/complete');
    expect(complete.request.method).toBe('POST');
    complete.flush({});

    service
      .update(9, {
        displayName: '新名称',
        note: '备注',
        tags: ['保留'],
      })
      .subscribe();
    const update = http.expectOne('/api/v1/media-library/9');
    expect(update.request.method).toBe('PATCH');
    expect(update.request.body).toEqual({
      displayName: '新名称',
      note: '备注',
      tags: ['保留'],
    });
    update.flush({});

    service.delete(9).subscribe();
    const remove = http.expectOne('/api/v1/media-library/9');
    expect(remove.request.method).toBe('DELETE');
    remove.flush({ state: 'requested', generation: 1 });
  });
});
