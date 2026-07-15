import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../../../settings.module';
import { BarkSettings } from '../../../shared/setting.model';
import { SettingsSyncService } from '../../../shared/services/settings-sync.service';
import { BarkSettingsComponent } from './bark-settings.component';

describe('BarkSettingsComponent', () => {
  let component: BarkSettingsComponent;
  let fixture: ComponentFixture<BarkSettingsComponent>;

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
    fixture = TestBed.createComponent(BarkSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      server: 'https://example.com',
      pushkey: 'push-key',
    } satisfies BarkSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
