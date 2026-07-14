import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';

import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import { ValidationService } from 'src/app/core/services/validation.service';
import { SettingsModule } from '../settings.module';
import { HeaderSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { HeaderSettingsComponent } from './header-settings.component';

describe('HeaderSettingsComponent', () => {
  let component: HeaderSettingsComponent;
  let fixture: ComponentFixture<HeaderSettingsComponent>;

  beforeEach(async () => {
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    settingsSyncService.syncSettings.and.returnValue(of());
    const validationService = jasmine.createSpyObj<ValidationService>(
      'ValidationService',
      ['validateCookie']
    );
    validationService.validateCookie.and.returnValue(
      of({ code: 200, message: '' })
    );

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, RouterTestingModule, SettingsModule],
      providers: [
        { provide: SettingsSyncService, useValue: settingsSyncService },
        { provide: ValidationService, useValue: validationService },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'error',
          ]),
        },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(HeaderSettingsComponent);
    component = fixture.componentInstance;
    component.settings = { userAgent: '', cookie: '' } satisfies HeaderSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('moves cookie editing to managed posting accounts', () => {
    const text = fixture.nativeElement.textContent;

    expect(text).toContain('Cookie 已由投稿账号管理');
    expect(text).toContain('前往投稿账号管理');
    expect(
      fixture.nativeElement.querySelector('app-cookie-edit-dialog')
    ).toBeNull();
  });
});
