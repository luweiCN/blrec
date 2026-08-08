import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';

import { of } from 'rxjs';

import { AuthGuard } from './auth.guard';
import { AuthService } from './auth.service';

describe('AuthGuard', () => {
  it('redirects an unauthenticated browser to the login page', (done) => {
    const auth = jasmine.createSpyObj<AuthService>('AuthService', [
      'ensureSession',
    ]);
    auth.ensureSession.and.returnValue(of(false));
    const loginTree = {};
    const router = jasmine.createSpyObj<Router>('Router', ['parseUrl']);
    router.parseUrl.and.returnValue(loginTree as never);
    TestBed.configureTestingModule({
      providers: [
        AuthGuard,
        { provide: AuthService, useValue: auth },
        { provide: Router, useValue: router },
      ],
    });

    TestBed.inject(AuthGuard)
      .canActivate()
      .subscribe((result) => {
        expect(result).toBe(loginTree as never);
        expect(router.parseUrl).toHaveBeenCalledOnceWith('/auth');
        done();
      });
  });
});
