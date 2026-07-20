import type { Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { NoPreloading, PreloadingStrategy, Router } from '@angular/router';
import type { LoadChildrenCallback, Route } from '@angular/router';

import { AppRoutingModule } from './app-routing.module';
import { AuthGuard } from './core/services/auth.guard';
import { ClipLibraryComponent } from './upload-tasks/clip-library/clip-library.component';
import { HighlightEditorComponent } from './upload-tasks/highlight-editor/highlight-editor.component';
import { RecordingSessionRowComponent } from './upload-tasks/recording-sessions/recording-session-row.component';
import { UploadTasksModule } from './upload-tasks/upload-tasks.module';

describe('AppRoutingModule', () => {
  let router: Router;

  beforeEach(() => {
    TestBed.configureTestingModule({ imports: [AppRoutingModule] });
    router = TestBed.inject(Router);
  });

  function route(path: string): Route {
    const value = router.config.find((candidate) => candidate.path === path);
    if (!value) {
      throw new Error(`missing route: ${path}`);
    }
    return value;
  }

  async function loadModuleName(path: string): Promise<string> {
    const loader = route(path).loadChildren;
    if (typeof loader !== 'function') {
      throw new Error(`route is not lazy: ${path}`);
    }
    const loaded = await (loader as LoadChildrenCallback)();
    return (loaded as Type<unknown>).name;
  }

  it('places every concrete editor route before its generic lazy route', () => {
    const paths = router.config.map((candidate) => candidate.path);

    for (const parent of ['recordings', 'upload-tasks', 'clips']) {
      expect(paths.indexOf(`${parent}/highlights/:sessionId`)).toBeLessThan(
        paths.indexOf(parent),
      );
    }
  });

  it('loads list, clip, and editor routes from independent modules', async () => {
    await expectAsync(loadModuleName('recordings')).toBeResolvedTo(
      'UploadTasksModule',
    );
    await expectAsync(loadModuleName('upload-tasks')).toBeResolvedTo(
      'UploadTasksModule',
    );
    await expectAsync(loadModuleName('clips')).toBeResolvedTo(
      'ClipLibraryModule',
    );
    for (const path of [
      'recordings/highlights/:sessionId',
      'upload-tasks/highlights/:sessionId',
      'clips/highlights/:sessionId',
    ]) {
      await expectAsync(loadModuleName(path)).toBeResolvedTo(
        'HighlightEditorModule',
      );
    }
  });

  it('keeps all six upload surfaces behind AuthGuard', () => {
    for (const path of [
      'recordings/highlights/:sessionId',
      'upload-tasks/highlights/:sessionId',
      'clips/highlights/:sessionId',
      'recordings',
      'upload-tasks',
      'clips',
    ]) {
      expect(route(path).canActivate).toContain(AuthGuard);
    }
  });

  it('does not preload lazy feature modules while the list is idle', () => {
    expect(TestBed.inject(PreloadingStrategy)).toEqual(
      jasmine.any(NoPreloading),
    );
  });

  it('keeps the list module row while excluding editor and clip declarations', () => {
    const declarations = (
      UploadTasksModule as unknown as {
        ɵmod: { declarations: readonly Type<unknown>[] };
      }
    ).ɵmod.declarations;

    expect(declarations).toContain(RecordingSessionRowComponent);
    expect(declarations).not.toContain(HighlightEditorComponent);
    expect(declarations).not.toContain(ClipLibraryComponent);
  });
});
