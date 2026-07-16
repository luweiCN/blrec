import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { of } from 'rxjs';
import { NzModalService } from 'ng-zorro-antd/modal';

import { BrowserExtensionTokenService } from './browser-extension-token.service';
import { BrowserExtensionTokensComponent } from './browser-extension-tokens.component';

describe('BrowserExtensionTokensComponent', () => {
  let fixture: ComponentFixture<BrowserExtensionTokensComponent>;
  let component: BrowserExtensionTokensComponent;
  let service: jasmine.SpyObj<BrowserExtensionTokenService>;
  let modal: jasmine.SpyObj<NzModalService>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<BrowserExtensionTokenService>(
      'BrowserExtensionTokenService',
      ['list', 'revoke']
    );
    modal = jasmine.createSpyObj<NzModalService>('NzModalService', ['confirm']);
    service.list.and.returnValue(
      of([
        {
          id: 7,
          createdAt: 1_700_000_000,
          lastUsedAt: 1_700_000_100,
          revokedAt: null,
        },
      ])
    );
    service.revoke.and.returnValue(of(void 0));

    await TestBed.configureTestingModule({
      declarations: [BrowserExtensionTokensComponent],
      imports: [CommonModule],
      providers: [
        { provide: BrowserExtensionTokenService, useValue: service },
        { provide: NzModalService, useValue: modal },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BrowserExtensionTokensComponent);
    component = fixture.componentInstance;
  });

  it('loads authorization times without displaying a token', () => {
    fixture.detectChanges();

    expect(service.list).toHaveBeenCalledTimes(1);
    expect(fixture.nativeElement.textContent).toContain('授权 #7');
    expect(fixture.nativeElement.textContent).not.toContain('blrec_ext_');
  });

  it('shows a concise empty state', () => {
    service.list.and.returnValue(of([]));

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('暂无浏览器插件授权');
  });

  it('revokes only after modal confirmation', async () => {
    fixture.detectChanges();

    component.confirmRevoke(component.tokens[0]);

    expect(service.revoke).not.toHaveBeenCalled();
    const options = modal.confirm.calls.mostRecent().args[0];
    const onOk = options?.nzOnOk as (() => void | Promise<void>) | undefined;
    expect(onOk).toBeDefined();
    await Promise.resolve(onOk?.());
    expect(service.revoke).toHaveBeenCalledOnceWith(7);
    expect(component.tokens[0].revokedAt).not.toBeNull();
  });
});
