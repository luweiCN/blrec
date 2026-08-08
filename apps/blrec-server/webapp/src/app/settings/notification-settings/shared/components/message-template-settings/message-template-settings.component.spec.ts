import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { NzMessageService } from 'ng-zorro-antd/message';

import { SettingsModule } from '../../../../settings.module';
import { MessageTemplateSettings } from '../../../../shared/setting.model';
import { SettingService } from '../../../../shared/services/setting.service';
import { MessageTemplateSettingsComponent } from './message-template-settings.component';

describe('MessageTemplateSettingsComponent', () => {
  let component: MessageTemplateSettingsComponent;
  let fixture: ComponentFixture<MessageTemplateSettingsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
            'error',
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
    fixture = TestBed.createComponent(MessageTemplateSettingsComponent);
    component = fixture.componentInstance;
    component.settings = {
      beganMessageType: 'text',
      beganMessageTitle: '',
      beganMessageContent: '',
      endedMessageType: 'text',
      endedMessageTitle: '',
      endedMessageContent: '',
      spaceMessageType: 'text',
      spaceMessageTitle: '',
      spaceMessageContent: '',
      errorMessageType: 'text',
      errorMessageTitle: '',
      errorMessageContent: '',
    } satisfies MessageTemplateSettings;
    component.keyOfSettings = 'emailNotification';
    component.ngOnChanges({});
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
