import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { of, throwError } from 'rxjs';

import { AuthService } from '../core/services/auth.service';
import { AuthComponent } from './auth.component';
import { AuthModule } from './auth.module';

describe('AuthComponent', () => {
  let fixture: ComponentFixture<AuthComponent>;
  let component: AuthComponent;
  let auth: jasmine.SpyObj<AuthService>;
  let router: Router;

  beforeEach(async () => {
    auth = jasmine.createSpyObj<AuthService>('AuthService', [
      'getStatus',
      'ensureSession',
      'setup',
      'login',
      'recover',
    ]);
    auth.getStatus.and.returnValue(
      of({ setupRequired: true, authenticated: false })
    );
    await TestBed.configureTestingModule({
      imports: [AuthModule, RouterTestingModule],
      providers: [{ provide: AuthService, useValue: auth }],
    }).compileComponents();
    router = TestBed.inject(Router);
    fixture = TestBed.createComponent(AuthComponent);
    component = fixture.componentInstance;
  });

  it('shows username, initialization key and a new password during setup', () => {
    fixture.detectChanges();

    expect(component.mode).toBe('setup');
    const username = fixture.nativeElement.querySelector('[name="username"]');
    const password = fixture.nativeElement.querySelector('[name="password"]');
    expect(username).not.toBeNull();
    expect(username.autocomplete).toBe('username');
    expect(fixture.nativeElement.querySelector('[name="apiKey"]')).not.toBeNull();
    expect(password.autocomplete).toBe('new-password');
    expect(fixture.nativeElement.textContent).toContain('设置管理员密码');
  });

  it('logs in with username and password without showing the API key', () => {
    auth.getStatus.and.returnValue(
      of({ setupRequired: false, authenticated: false })
    );
    auth.login.and.returnValue(
      of({ authenticated: true, csrfToken: 'csrf', expiresAt: 123 })
    );
    spyOn(router, 'navigateByUrl');
    fixture.detectChanges();
    component.username = 'owner';
    component.password = 'correct horse battery staple';

    component.submit();

    expect(auth.login).toHaveBeenCalledOnceWith(
      'owner',
      'correct horse battery staple'
    );
    expect(fixture.nativeElement.querySelector('[name="apiKey"]')).toBeNull();
    expect(router.navigateByUrl).toHaveBeenCalledOnceWith('/tasks');
  });

  it('shows all initialization credentials when recovering the password', () => {
    auth.getStatus.and.returnValue(
      of({ setupRequired: false, authenticated: false })
    );
    fixture.detectChanges();

    const recoveryButton = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    ).find((button) => button.textContent?.includes('重置密码'));
    recoveryButton?.click();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[name="username"]')).not.toBeNull();
    expect(fixture.nativeElement.querySelector('[name="apiKey"]')).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('[name="password"]').autocomplete
    ).toBe('new-password');
  });

  it('keeps login errors on the page without a browser prompt', () => {
    auth.getStatus.and.returnValue(
      of({ setupRequired: false, authenticated: false })
    );
    auth.login.and.returnValue(
      throwError(() => ({ error: { detail: 'Password is invalid' } }))
    );
    spyOn(window, 'prompt');
    fixture.detectChanges();
    component.username = 'owner';
    component.password = 'wrong password';

    component.submit();

    expect(component.errorMessage).toBe('Password is invalid');
    expect(window.prompt).not.toHaveBeenCalled();
  });

  it('shows the retry time after too many failed attempts', () => {
    auth.getStatus.and.returnValue(
      of({ setupRequired: false, authenticated: false })
    );
    auth.login.and.returnValue(
      throwError(() => ({
        status: 429,
        headers: { get: () => '900' },
      }))
    );
    fixture.detectChanges();
    component.username = 'owner';
    component.password = 'wrong password';

    component.submit();

    expect(component.errorMessage).toContain('15 分钟');
  });
});
