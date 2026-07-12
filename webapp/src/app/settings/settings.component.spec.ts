import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';

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
});
