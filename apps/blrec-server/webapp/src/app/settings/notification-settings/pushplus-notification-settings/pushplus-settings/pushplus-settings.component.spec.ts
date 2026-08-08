import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../../../settings.module';
import { PushplusSettings } from '../../../shared/setting.model';
import { SettingsSyncService } from '../../../shared/services/settings-sync.service';
import { PushplusSettingsComponent } from './pushplus-settings.component';

describe('PushplusSettingsComponent', () => {
  let component: PushplusSettingsComponent;
  let fixture: ComponentFixture<PushplusSettingsComponent>;

  beforeEach(async () => {
    const settingsSyncService = jasmine.createSpyObj<SettingsSyncService>(
      'SettingsSyncService',
      ['syncSettings']
    );
    settingsSyncService.syncSettings.and.returnValue(of());

    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        { provide: SettingsSyncService, useValue: settingsSyncService },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(PushplusSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      token: 'token',
      topic: 'topic',
    } satisfies PushplusSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
