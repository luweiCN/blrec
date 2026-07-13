import { TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  CloudUploadOutline,
  GithubOutline,
  InfoCircleOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  SettingOutline,
  UnorderedListOutline,
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
            GithubOutline,
            InfoCircleOutline,
            MenuFoldOutline,
            MenuUnfoldOutline,
            SettingOutline,
            UnorderedListOutline,
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
      'B 站直播录制'
    );
  });

  it('labels the uploads navigation as Bilibili accounts', () => {
    const fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();

    const link = fixture.nativeElement.querySelector(
      'a[href="/uploads"]'
    ) as HTMLAnchorElement;
    expect(link.textContent?.trim()).toBe('投稿账号');
  });
});
