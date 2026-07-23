import { NgModule } from '@angular/core';

import { ClipLibraryContentModule } from './clip-library-content.module';
import { ClipLibraryRoutingModule } from './clip-library-routing.module';

@NgModule({
  imports: [ClipLibraryContentModule, ClipLibraryRoutingModule],
})
export class ClipLibraryModule {}
