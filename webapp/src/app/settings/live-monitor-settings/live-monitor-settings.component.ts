import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { FormBuilder, FormGroup, Validators } from '@angular/forms';

import { Observable, Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import {
  LiveMonitorSettings,
  LiveMonitorSettingsView,
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
export class LiveMonitorSettingsComponent implements OnInit, OnDestroy {
  settingsLoad: LiveMonitorSettingsView = { state: 'loading' };
  status: LiveStatusView = { state: 'loading' };

  readonly settingsForm: FormGroup;
  readonly modeOptions = [
    { label: '批量模式', value: 'batch' },
    { label: '旧模式', value: 'legacy' },
  ];
  private readonly destroy$ = new Subject<void>();

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
    this.settingsForm.disable({ emitEvent: false });
  }

  ngOnInit(): void {
    this.loadSettings();
    this.loadStatus();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  retrySettings(): void {
    this.loadSettings();
  }

  resume(): void {
    this.status = { state: 'loading' };
    this.changeDetector.markForCheck();
    this.liveStatusService
      .resume()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => this.loadStatus(),
        error: (error: Error) => this.showStatusError(error),
      });
  }

  private loadSettings(): void {
    this.settingsLoad = { state: 'loading' };
    this.settingsForm.disable({ emitEvent: false });
    this.changeDetector.markForCheck();
    this.settingService
      .getSettings(['liveMonitor'])
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (settings) => {
          const liveMonitor = settings.liveMonitor;
          this.settingsForm.setValue(liveMonitor, { emitEvent: false });
          this.settingsForm.enable({ emitEvent: false });
          this.settingsLoad = { state: 'ready' };
          this.settingsSyncService
            .syncSettings(
              'liveMonitor',
              liveMonitor,
              this.settingsForm.valueChanges.pipe(
                takeUntil(this.destroy$)
              ) as Observable<LiveMonitorSettings>
            )
            .pipe(takeUntil(this.destroy$))
            .subscribe();
          this.changeDetector.markForCheck();
        },
        error: (error: Error) => {
          this.settingsLoad = { state: 'error', message: error.message };
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadStatus(): void {
    this.status = { state: 'loading' };
    this.liveStatusService
      .getMetrics()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
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
