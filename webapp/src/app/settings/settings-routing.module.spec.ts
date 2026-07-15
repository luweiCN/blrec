import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { SettingsRoutingModule } from './settings-routing.module';

describe('SettingsRoutingModule', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [RouterTestingModule, SettingsRoutingModule],
    });
  });

  it('redirects retired notification routes to the independent page', () => {
    const routes = TestBed.inject(Router).config;
    const channels = [
      'email',
      'serverchan',
      'pushdeer',
      'pushplus',
      'telegram',
      'bark',
    ];

    for (const channel of channels) {
      const path = `${channel}-notification`;
      const route = routes.find((candidate) => candidate.path === path);
      expect(route?.redirectTo).toBe(`/notifications/${path}`);
      expect(route?.pathMatch).toBe('full');
    }
  });
});
