import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import {
  BellOutline,
  CloudUploadOutline,
  GithubOutline,
  InfoCircleOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  GlobalOutline,
  SettingOutline,
  UnorderedListOutline,
  UserOutline,
  VideoCameraOutline,
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
            BellOutline,
            CloudUploadOutline,
            GithubOutline,
            InfoCircleOutline,
            MenuFoldOutline,
            MenuUnfoldOutline,
            GlobalOutline,
            SettingOutline,
            UnorderedListOutline,
            UserOutline,
            VideoCameraOutline,
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

  it('shows recording, upload-task, and Bilibili-account navigation', () => {
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
    const recordingTasks = fixture.nativeElement.querySelector(
      'a[href="/tasks"]',
    ) as HTMLAnchorElement;
    const recordings = fixture.nativeElement.querySelector(
      'a[href="/recordings"]',
    ) as HTMLAnchorElement;
    const network = fixture.nativeElement.querySelector(
      'a[href="/network"]',
    ) as HTMLAnchorElement;

    expect(recordingTasks?.textContent?.trim()).toBe('房间管理');
    expect(recordings?.textContent?.trim()).toBe('录制任务');
    expect(uploadTasks).not.toBeNull();
    expect(uploadTasks?.textContent?.trim()).toBe('上传任务');
    expect(policies).toBeNull();
    expect(accounts.textContent?.trim()).toBe('投稿账号');
    expect(network.textContent?.trim()).toBe('网络管理');
  });

  it('uses distinct icons for room management and recording tasks', () => {
    const fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();

    const roomIcon = fixture.nativeElement
      .querySelector('a[href="/tasks"]')
      ?.closest('li')
      ?.querySelector('i');
    const recordingIcon = fixture.nativeElement
      .querySelector('a[href="/recordings"]')
      ?.closest('li')
      ?.querySelector('i');

    expect(roomIcon?.classList).toContain('anticon-unordered-list');
    expect(recordingIcon?.classList).toContain('anticon-video-camera');
  });

  it('shows primary navigation in the expected order', () => {
    const fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();

    const links = fixture.nativeElement.querySelectorAll(
      '.sidebar-menu a',
    ) as NodeListOf<HTMLAnchorElement>;
    const labels = Array.from(links).map((link) => link.textContent?.trim());

    expect(labels).toEqual([
      '房间管理',
      '录制任务',
      '上传任务',
      '投稿账号',
      '网络管理',
      '设置',
      '通知设置',
      '关于',
    ]);
  });

  it('lazy loads recording and upload lists as separate scopes', () => {
    const router = TestBed.inject(Router);
    const recordings = router.config.find(
      (candidate) => candidate.path === 'recordings',
    );
    const uploads = router.config.find(
      (candidate) => candidate.path === 'upload-tasks',
    );

    expect(recordings?.data?.sessionScope).toBe('recordings');
    expect(uploads?.data?.sessionScope).toBe('uploads');
  });

  it('lazy loads the independent notification settings page', () => {
    const router = TestBed.inject(Router);
    const route = router.config.find(
      (candidate) => candidate.path === 'notifications',
    );

    expect(route?.loadChildren).toBeDefined();
  });

  it('redirects the retired upload-policy page to recording tasks', () => {
    const router = TestBed.inject(Router);
    const route = router.config.find(
      (candidate) => candidate.path === 'upload-policies',
    );

    expect(route?.redirectTo).toBe('tasks');
    expect(route?.pathMatch).toBe('full');
  });
});
