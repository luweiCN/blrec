import { NgModule } from '@angular/core';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';

import {
  CloudUploadOutline,
  CopyOutline,
  DashboardOutline,
  MenuFoldOutline,
  MenuUnfoldOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
  SwapOutline,
  UserOutline,
} from '@ant-design/icons-angular/icons';

const icons = [
  MenuFoldOutline,
  MenuUnfoldOutline,
  DashboardOutline,
  CloudUploadOutline,
  CopyOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
  SwapOutline,
  UserOutline,
];

@NgModule({
  imports: [NzIconModule],
  exports: [NzIconModule],
  providers: [{ provide: NZ_ICONS, useValue: icons }],
})
export class IconsProviderModule {}
