import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  NetworkInterfaceResponse,
  NetworkInterfaceUpdate,
} from './network.model';

@Injectable({ providedIn: 'root' })
export class NetworkService {
  constructor(private http: HttpClient, private url: UrlService) {}

  getInterfaces(): Observable<NetworkInterfaceResponse> {
    return this.http.get<NetworkInterfaceResponse>(
      this.url.makeApiUrl('/api/v1/network/interfaces'),
    );
  }

  probe(interfaceName: string | null = null): Observable<NetworkInterfaceResponse> {
    return this.http.post<NetworkInterfaceResponse>(
      this.url.makeApiUrl('/api/v1/network/probe'),
      { interfaceName },
    );
  }

  updateInterface(
    interfaceName: string,
    update: NetworkInterfaceUpdate
  ): Observable<NetworkInterfaceResponse> {
    return this.http.patch<NetworkInterfaceResponse>(
      this.url.makeApiUrl(`/api/v1/network/interfaces/${interfaceName}`),
      update
    );
  }
}
