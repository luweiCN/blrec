import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { ValidationService } from 'src/app/core/services/validation.service';
import { SettingsModule } from '../settings.module';
import { OutputSettings } from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { OutputSettingsComponent } from './output-settings.component';

describe('OutputSettingsComponent', () => {
  let component: OutputSettingsComponent;
  let fixture: ComponentFixture<OutputSettingsComponent>;

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
    fixture = TestBed.createComponent(OutputSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      outDir: '',
      pathTemplate: '',
      filesizeLimit: 0,
      durationLimit: 0,
    } satisfies OutputSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
