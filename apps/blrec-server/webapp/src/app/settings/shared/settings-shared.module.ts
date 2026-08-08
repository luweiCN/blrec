import { NgModule } from '@angular/core';

import { SwitchActionableDirective } from './directives/switch-actionable.directive';

@NgModule({
  declarations: [SwitchActionableDirective],
  exports: [SwitchActionableDirective],
})
export class SettingsSharedModule {}
