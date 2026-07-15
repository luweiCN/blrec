import {
  Component,
  OnInit,
  ChangeDetectionStrategy,
  Input,
  OnChanges,
  ChangeDetectorRef,
} from '@angular/core';
import { FormBuilder, FormControl, FormGroup } from '@angular/forms';

import { Observable } from 'rxjs';
import { finalize } from 'rxjs/operators';
import mapValues from 'lodash-es/mapValues';

import { SpaceSettings } from '../shared/setting.model';
import {
  SettingsSyncService,
  SyncStatus,
  calcSyncStatus,
} from '../shared/services/settings-sync.service';
import { SYNC_FAILED_WARNING_TIP } from '../shared/constants/form';
import {
  RecordingRetentionService,
  RecordingRetentionStatus,
} from './recording-retention.service';

@Component({
  selector: 'app-disk-space-settings',
  templateUrl: './disk-space-settings.component.html',
  styleUrls: ['./disk-space-settings.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DiskSpaceSettingsComponent implements OnInit, OnChanges {
  @Input() settings!: SpaceSettings;
  syncStatus!: SyncStatus<SpaceSettings>;
  retentionStatus: RecordingRetentionStatus | null = null;
  retentionStatusLoading = false;

  readonly settingsForm: FormGroup;
  readonly syncFailedWarningTip = SYNC_FAILED_WARNING_TIP;

  readonly intervalOptions = [
    { label: '不检测', value: 0 },
    { label: '10 秒', value: 10 },
    { label: '30 秒', value: 30 },
    { label: '1 分钟', value: 60 },
    { label: '3 分钟', value: 180 },
    { label: '5 分钟', value: 300 },
    { label: '10 分钟', value: 600 },
  ];

  readonly thresholdOptions = [
    { label: '1 GB', value: 1024 ** 3 },
    { label: '3 GB', value: 1024 ** 3 * 3 },
    { label: '5 GB', value: 1024 ** 3 * 5 },
    { label: '10 GB', value: 1024 ** 3 * 10 },
    { label: '20 GB', value: 1024 ** 3 * 20 },
  ];

  readonly capacityOptions = [
    { label: '不限制', value: 0 },
    { label: '100 GB', value: 1024 ** 3 * 100 },
    { label: '200 GB', value: 1024 ** 3 * 200 },
    { label: '500 GB', value: 1024 ** 3 * 500 },
    { label: '1 TB', value: 1024 ** 4 },
    { label: '2 TB', value: 1024 ** 4 * 2 },
  ];

  readonly capacityWarningOptions = [
    { label: '不预警', value: 0 },
    { label: '5 GB', value: 1024 ** 3 * 5 },
    { label: '10 GB', value: 1024 ** 3 * 10 },
    { label: '20 GB', value: 1024 ** 3 * 20 },
    { label: '50 GB', value: 1024 ** 3 * 50 },
    { label: '100 GB', value: 1024 ** 3 * 100 },
  ];

  constructor(
    formBuilder: FormBuilder,
    private changeDetector: ChangeDetectorRef,
    private settingsSyncService: SettingsSyncService,
    private recordingRetentionService: RecordingRetentionService
  ) {
    this.settingsForm = formBuilder.group({
      recycleRecords: [''],
      checkInterval: [''],
      spaceThreshold: [''],
      recordingCapacity: [''],
      capacityWarningThreshold: [''],
    });
  }

  get recycleRecordsControl() {
    return this.settingsForm.get('recycleRecords') as FormControl;
  }

  get checkIntervalControl() {
    return this.settingsForm.get('checkInterval') as FormControl;
  }

  get spaceThresholdControl() {
    return this.settingsForm.get('spaceThreshold') as FormControl;
  }

  get recordingCapacityControl() {
    return this.settingsForm.get('recordingCapacity') as FormControl;
  }

  get capacityWarningThresholdControl() {
    return this.settingsForm.get('capacityWarningThreshold') as FormControl;
  }

  ngOnChanges(): void {
    this.syncStatus = mapValues(this.settings, () => true);
    this.settingsForm.setValue(this.settings);
  }

  ngOnInit(): void {
    this.refreshRetentionStatus();
    this.settingsSyncService
      .syncSettings(
        'space',
        this.settings,
        this.settingsForm.valueChanges as Observable<SpaceSettings>
      )
      .subscribe((detail) => {
        this.syncStatus = { ...this.syncStatus, ...calcSyncStatus(detail) };
        this.changeDetector.markForCheck();
      });
  }

  refreshRetentionStatus(): void {
    if (this.retentionStatusLoading) {
      return;
    }
    this.retentionStatusLoading = true;
    this.recordingRetentionService
      .status()
      .pipe(
        finalize(() => {
          this.retentionStatusLoading = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (status) => {
          this.retentionStatus = status;
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.retentionStatus = null;
        },
      });
  }

  formatBytes(value: number): string {
    if (value >= 1024 ** 4) {
      return `${(value / 1024 ** 4).toFixed(2)} TB`;
    }
    return `${(value / 1024 ** 3).toFixed(2)} GB`;
  }

  get capacityPercent(): number {
    const status = this.retentionStatus;
    if (!status || status.capacityBytes <= 0) {
      return 0;
    }
    return Math.min(
      100,
      Math.max(0, (status.managedVideoBytes / status.capacityBytes) * 100)
    );
  }
}
