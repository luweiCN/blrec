import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import {
  CoverSaveStrategy,
  DeleteStrategy,
  GlobalTaskSettings,
  TaskOptions,
} from 'src/app/settings/shared/setting.model';
import { TaskSettingsDialogComponent } from './task-settings-dialog.component';

const globalSettings: GlobalTaskSettings = {
  output: {
    outDir: '',
    pathTemplate: '',
    filesizeLimit: 0,
    durationLimit: 0,
  },
  biliApi: {
    baseApiUrls: [],
    baseLiveApiUrls: [],
    basePlayInfoApiUrls: [],
  },
  header: { userAgent: '', cookie: '' },
  danmaku: {
    danmuUname: false,
    recordGiftSend: false,
    recordFreeGifts: false,
    recordGuardBuy: false,
    recordSuperChat: false,
    saveRawDanmaku: false,
  },
  recorder: {
    streamFormat: 'flv',
    recordingMode: 'standard',
    qualityNumber: 10000,
    fmp4StreamTimeout: 0,
    readTimeout: 0,
    disconnectionTimeout: 0,
    bufferSize: 0,
    saveCover: false,
    coverSaveStrategy: CoverSaveStrategy.DEFAULT,
  },
  postprocessing: {
    injectExtraMetadata: false,
    remuxToMp4: false,
    deleteSource: DeleteStrategy.AUTO,
  },
};

const taskOptions: TaskOptions = {
  ...globalSettings,
  output: {
    pathTemplate: null,
    filesizeLimit: null,
    durationLimit: null,
  },
};

describe('TaskSettingsDialogComponent', () => {
  let component: TaskSettingsDialogComponent;
  let fixture: ComponentFixture<TaskSettingsDialogComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [TaskSettingsDialogComponent],
      schemas: [NO_ERRORS_SCHEMA],
    })
      .compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(TaskSettingsDialogComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('taskOptions', taskOptions);
    fixture.componentRef.setInput('globalSettings', globalSettings);
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
    expect(component.options).toEqual(taskOptions);
    expect(component.model).toBeDefined();
  });
});
