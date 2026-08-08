import { TemplateRef } from '@angular/core';

import { SubPageContentDirective } from './sub-page-content.directive';

describe('SubPageContentDirective', () => {
  it('should create an instance', () => {
    const templateRef = jasmine.createSpyObj<TemplateRef<unknown>>(
      'TemplateRef',
      ['createEmbeddedView']
    );
    const directive = new SubPageContentDirective(templateRef);

    expect(directive).toBeTruthy();
  });
});
