import { HttpErrorResponse } from '@angular/common/http';
import { Injectable } from '@angular/core';
import {
  ActivatedRouteSnapshot,
  Resolve,
  RouterStateSnapshot,
} from '@angular/router';

import { Observable } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { NGXLogger } from 'ngx-logger';
import { NzNotificationService } from 'ng-zorro-antd/notification';

import { retry } from '../../shared/rx-operators';
import { SettingService } from '../../settings/shared/services/setting.service';
import { Settings } from '../../settings/shared/setting.model';

export type NotificationPageSettings = Pick<
  Settings,
  | 'emailNotification'
  | 'serverchanNotification'
  | 'pushdeerNotification'
  | 'pushplusNotification'
  | 'telegramNotification'
  | 'barkNotification'
  | 'operationalNotifications'
>;

@Injectable()
export class NotificationsResolver
  implements Resolve<NotificationPageSettings>
{
  constructor(
    private logger: NGXLogger,
    private notification: NzNotificationService,
    private settingService: SettingService
  ) {}

  resolve(
    route: ActivatedRouteSnapshot,
    state: RouterStateSnapshot
  ): Observable<NotificationPageSettings> {
    return this.settingService
      .getSettings([
        'emailNotification',
        'serverchanNotification',
        'pushdeerNotification',
        'pushplusNotification',
        'telegramNotification',
        'barkNotification',
        'operationalNotifications',
      ])
      .pipe(
        retry(3, 300),
        catchError((error: HttpErrorResponse) => {
          this.logger.error('Failed to get notification settings:', error);
          this.notification.error('获取通知设置出错', error.message, {
            nzDuration: 0,
          });
          throw error;
        })
      );
  }
}
