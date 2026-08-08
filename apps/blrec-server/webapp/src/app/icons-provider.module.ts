import { NgModule } from '@angular/core';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';

import {
  BellOutline,
  CloudDownloadOutline,
  CloudUploadOutline,
  CopyOutline,
  DashboardOutline,
  DatabaseOutline,
  GlobalOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  MoreOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
  ScissorOutline,
  StarOutline,
  SwapOutline,
  TrophyOutline,
  UnorderedListOutline,
  UserOutline,
  VideoCameraOutline,
} from '@ant-design/icons-angular/icons';

const icons = [
  BellOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  MoreOutline,
  DashboardOutline,
  GlobalOutline,
  CloudDownloadOutline,
  CloudUploadOutline,
  CopyOutline,
  DatabaseOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
  ScissorOutline,
  StarOutline,
  SwapOutline,
  TrophyOutline,
  UnorderedListOutline,
  UserOutline,
  VideoCameraOutline,
];

@NgModule({
  imports: [NzIconModule],
  exports: [NzIconModule],
  providers: [{ provide: NZ_ICONS, useValue: icons }],
})
export class IconsProviderModule {}
