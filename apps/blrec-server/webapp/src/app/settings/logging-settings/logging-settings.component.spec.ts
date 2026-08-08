import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { ValidationService } from 'src/app/core/services/validation.service';
import { SettingsModule } from '../settings.module';
import { LoggingSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { LoggingSettingsComponent } from './logging-settings.component';

describe('LoggingSettingsComponent', () => {
  let component: LoggingSettingsComponent;
  let fixture: ComponentFixture<LoggingSettingsComponent>;

  beforeEach(async () => {
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    settingsSyncService.syncSettings.and.returnValue(of());
    const validationService = jasmine.createSpyObj<ValidationService>(
      'ValidationService',
      ['validateDir']
    );
    validationService.validateDir.and.returnValue(
      of({ code: 200, message: '' })
    );

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: SettingsSyncService, useValue: settingsSyncService },
        { provide: ValidationService, useValue: validationService },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(LoggingSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      logDir: '',
      consoleLogLevel: 'INFO',
      backupCount: 30,
    } satisfies LoggingSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
    expect(fixture.nativeElement.textContent).toContain('日志保留天数');
  });
});
