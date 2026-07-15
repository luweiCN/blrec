import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { NetworkService } from './network.service';

describe('NetworkService', () => {
  let service: NetworkService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        NetworkService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(NetworkService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads host interfaces', () => {
    service.getInterfaces().subscribe((value) => {
      expect(value.interfaces[0].name).toBe('eth0');
    });

    const request = http.expectOne('/api/v1/network/interfaces');
    expect(request.request.method).toBe('GET');
    request.flush({ interfaces: [{ name: 'eth0' }] });
  });

  it('can probe all interfaces', () => {
    service.probe().subscribe();

    const request = http.expectOne('/api/v1/network/probe');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ interfaceName: null });
    request.flush({ interfaces: [] });
  });
});
