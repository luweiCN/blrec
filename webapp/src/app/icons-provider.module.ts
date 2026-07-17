import { NgModule } from '@angular/core';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';

import {
  BellOutline,
  CloudUploadOutline,
  CopyOutline,
  DashboardOutline,
  GlobalOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  MoreOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
  SwapOutline,
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
  CloudUploadOutline,
  CopyOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
  SwapOutline,
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
