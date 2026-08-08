import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute } from '@angular/router';

import {
  ArrowLeftOutline,
  ClearOutline,
  MoreOutline,
  PlusOutline,
} from '@ant-design/icons-angular/icons';
import { of } from 'rxjs';
import { NZ_ICONS } from 'ng-zorro-antd/icon';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';

import { SettingsModule } from '../../settings.module';
import { SettingService } from '../../shared/services/setting.service';
import { WebhookManagerComponent } from './webhook-manager.component';

describe('WebhookManagerComponent', () => {
  let component: WebhookManagerComponent;
  let fixture: ComponentFixture<WebhookManagerComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        {
          provide: NZ_ICONS,
          useValue: [
            ArrowLeftOutline,
            ClearOutline,
            MoreOutline,
            PlusOutline,
          ],
        },
        { provide: ActivatedRoute, useValue: { data: of({ settings: [] }) } },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'error',
          ]),
        },
        {
          provide: NzModalService,
          useValue: jasmine.createSpyObj<NzModalService>('NzModalService', [
            'confirm',
          ]),
        },
        {
          provide: SettingService,
          useValue: jasmine.createSpyObj<SettingService>('SettingService', [
            'changeSettings',
          ]),
        },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(WebhookManagerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
