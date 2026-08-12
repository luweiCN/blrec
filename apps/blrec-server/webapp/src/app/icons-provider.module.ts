import { NgModule } from '@angular/core';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';

import {
  BellOutline,
  BarChartOutline,
  CloudDownloadOutline,
  CloudUploadOutline,
  CopyOutline,
  DashboardOutline,
  DatabaseOutline,
  DollarCircleOutline,
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
  BarChartOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  MoreOutline,
  DashboardOutline,
  GlobalOutline,
  CloudDownloadOutline,
  CloudUploadOutline,
  CopyOutline,
  DatabaseOutline,
  DollarCircleOutline,
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
