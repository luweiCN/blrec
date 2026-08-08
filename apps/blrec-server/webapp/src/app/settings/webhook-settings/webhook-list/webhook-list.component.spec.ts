import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import {
  ClearOutline,
  MoreOutline,
  PlusOutline,
} from '@ant-design/icons-angular/icons';
import { NZ_ICONS } from 'ng-zorro-antd/icon';

import { SettingsModule } from '../../settings.module';
import { WebhookListComponent } from './webhook-list.component';

describe('WebhookListComponent', () => {
  let component: WebhookListComponent;
  let fixture: ComponentFixture<WebhookListComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule, SettingsModule],
      providers: [
        {
          provide: NZ_ICONS,
          useValue: [ClearOutline, MoreOutline, PlusOutline],
        },
      ],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(WebhookListComponent);
    component = fixture.componentInstance;
    component.data = [];
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
