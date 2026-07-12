import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { FormBuilder, FormGroup, Validators } from '@angular/forms';

import { Observable } from 'rxjs';

import {
  LiveMonitorSettings,
  LiveStatusView,
} from '../shared/setting.model';
import { LiveStatusService } from '../shared/services/live-status.service';
import { SettingService } from '../shared/services/setting.service';
import { SettingsSyncService } from '../shared/services/settings-sync.service';

@Component({
  selector: 'app-live-monitor-settings',
  templateUrl: './live-monitor-settings.component.html',
  styleUrls: ['./live-monitor-settings.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LiveMonitorSettingsComponent implements OnInit {
  status: LiveStatusView = { state: 'loading' };

  readonly settingsForm: FormGroup;
  readonly modeOptions = [
    { label: '批量模式', value: 'batch' },
    { label: '旧模式', value: 'legacy' },
  ];

  constructor(
    formBuilder: FormBuilder,
    private changeDetector: ChangeDetectorRef,
    private liveStatusService: LiveStatusService,
    private settingService: SettingService,
    private settingsSyncService: SettingsSyncService
  ) {
    this.settingsForm = formBuilder.nonNullable.group({
      mode: ['batch', Validators.required],
      intervalSeconds: [
        30,
        [Validators.required, Validators.min(30), Validators.max(60)],
      ],
      batchSize: [
        29,
        [Validators.required, Validators.min(1), Validators.max(29)],
      ],
      fallbackCooldownSeconds: [
        600,
        [Validators.required, Validators.min(600), Validators.max(3600)],
      ],
    });
  }

  ngOnInit(): void {
    this.loadSettings();
    this.loadStatus();
  }

  resume(): void {
    this.status = { state: 'loading' };
    this.changeDetector.markForCheck();
    this.liveStatusService.resume().subscribe({
      next: () => this.loadStatus(),
      error: (error: Error) => this.showStatusError(error),
    });
  }

  private loadSettings(): void {
    this.settingService.getSettings(['liveMonitor']).subscribe((settings) => {
      const liveMonitor = settings.liveMonitor;
      this.settingsForm.setValue(liveMonitor, { emitEvent: false });
      this.settingsSyncService
        .syncSettings(
          'liveMonitor',
          liveMonitor,
          this.settingsForm.valueChanges as Observable<LiveMonitorSettings>
        )
        .subscribe();
      this.changeDetector.markForCheck();
    });
  }

  private loadStatus(): void {
    this.status = { state: 'loading' };
    this.liveStatusService.getMetrics().subscribe({
      next: (data) => {
        this.status = { state: 'ready', data };
        this.changeDetector.markForCheck();
      },
      error: (error: Error) => this.showStatusError(error),
    });
  }

  private showStatusError(error: Error): void {
    this.status = { state: 'error', message: error.message };
    this.changeDetector.markForCheck();
  }
}
