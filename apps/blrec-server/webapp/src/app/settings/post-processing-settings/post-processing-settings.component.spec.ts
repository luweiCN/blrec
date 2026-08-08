import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';

import { SettingsModule } from '../settings.module';
import {
  DeleteStrategy,
  PostprocessingSettings,
} from '../shared/setting.model';
import { SettingsSyncService } from '../shared/services/settings-sync.service';
import { PostProcessingSettingsComponent } from './post-processing-settings.component';

describe('PostProcessingSettingsComponent', () => {
  let component: PostProcessingSettingsComponent;
  let fixture: ComponentFixture<PostProcessingSettingsComponent>;

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
    fixture = TestBed.createComponent(PostProcessingSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      injectExtraMetadata: false,
      remuxToMp4: false,
      deleteSource: DeleteStrategy.NEVER,
    } satisfies PostprocessingSettings;
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
