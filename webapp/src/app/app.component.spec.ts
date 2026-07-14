import { TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  CloudUploadOutline,
  FormOutline,
  GithubOutline,
  InfoCircleOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  SettingOutline,
  UnorderedListOutline,
  UserOutline,
} from '@ant-design/icons-angular/icons';
import { NZ_ICONS } from 'ng-zorro-antd/icon';

import { AppComponent } from './app.component';
import { AppModule } from './app.module';

describe('AppComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppModule, RouterTestingModule],
      providers: [
        {
          provide: NZ_ICONS,
          useValue: [
            CloudUploadOutline,
            FormOutline,
            GithubOutline,
            InfoCircleOutline,
            MenuFoldOutline,
            MenuUnfoldOutline,
            SettingOutline,
            UnorderedListOutline,
            UserOutline,
          ],
        },
      ],
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(AppComponent);
    const app = fixture.componentInstance;
    expect(app).toBeTruthy();
  });

  it(`should have as title 'B 站直播录制'`, () => {
    const fixture = TestBed.createComponent(AppComponent);
    const app = fixture.componentInstance;
    expect(app.title).toEqual('B 站直播录制');
  });

  it('should render title', () => {
    const fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();
    const compiled = fixture.nativeElement;
    expect(compiled.querySelector('.app-title').textContent).toContain(
      'B 站直播录制',
    );
  });

  it('shows separate upload-task and Bilibili-account navigation', () => {
    const fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();

    const uploadTasks = fixture.nativeElement.querySelector(
      'a[href="/upload-tasks"]',
    ) as HTMLAnchorElement;
    const accounts = fixture.nativeElement.querySelector(
      'a[href="/uploads"]',
    ) as HTMLAnchorElement;
    const policies = fixture.nativeElement.querySelector(
      'a[href="/upload-policies"]',
    ) as HTMLAnchorElement;

    expect(uploadTasks).not.toBeNull();
    expect(uploadTasks?.textContent?.trim()).toBe('上传任务');
    expect(policies).not.toBeNull();
    expect(policies?.textContent?.trim()).toBe('投稿规则');
    expect(accounts.textContent?.trim()).toBe('投稿账号');
  });
});
