import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NGXLogger } from 'ngx-logger';

import { RouterScrollService } from '../core/services/router-scroll.service';
import { SettingsComponent } from './settings.component';

describe('SettingsComponent', () => {
  let component: SettingsComponent;
  let fixture: ComponentFixture<SettingsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [SettingsComponent],
      imports: [NoopAnimationsModule],
      providers: [
        {
          provide: ActivatedRoute,
          useValue: {
            data: of({
              settings: {
                output: {},
                recorder: {},
                danmaku: {},
                postprocessing: {},
                space: {},
                biliApi: {},
                header: {},
                logging: {},
              },
            }),
          },
        },
        {
          provide: NGXLogger,
          useValue: jasmine.createSpyObj<NGXLogger>('NGXLogger', ['error']),
        },
        {
          provide: RouterScrollService,
          useValue: jasmine.createSpyObj<RouterScrollService>(
            'RouterScrollService',
            ['setCustomViewportToScroll']
          ),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  beforeEach(() => {
    fixture = TestBed.createComponent(SettingsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('renders only system settings without a tab bar', () => {
    const text = fixture.nativeElement.textContent;

    expect(fixture.nativeElement.querySelector('nz-tabset')).toBeNull();
    expect(text).not.toContain('通知设置');
    expect(
      fixture.nativeElement.querySelector('app-notification-settings')
    ).toBeNull();
    expect(fixture.nativeElement.querySelectorAll('.primary-page').length).toBe(
      1
    );
  });
});
