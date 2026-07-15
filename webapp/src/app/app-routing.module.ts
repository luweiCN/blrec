import { NgModule } from '@angular/core';
import { Routes, RouterModule, PreloadAllModules } from '@angular/router';

import { PageNotFoundComponent } from './page-not-found/page-not-found.component';
import { RouteScrollBehaviour } from './core/services/router-scroll.service.intf';
import { AuthGuard } from './core/services/auth.guard';

const routes: Routes = [
  {
    path: 'auth',
    loadChildren: () => import('./auth/auth.module').then((m) => m.AuthModule),
  },
  {
    path: 'tasks',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./tasks/tasks.module').then((m) => m.TasksModule),
  },
  {
    path: 'network',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./network/network.module').then((m) => m.NetworkModule),
  },
  {
    path: 'settings',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./settings/settings.module').then((m) => m.SettingsModule),
    data: {
      scrollBehavior: RouteScrollBehaviour.KEEP_POSITION,
    },
  },
  {
    path: 'notifications',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./notifications/notifications.module').then(
        (m) => m.NotificationsModule
      ),
  },
  {
    path: 'upload-tasks',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./upload-tasks/upload-tasks.module').then(
        (m) => m.UploadTasksModule,
      ),
  },
  {
    path: 'upload-policies',
    pathMatch: 'full',
    redirectTo: 'tasks',
  },
  {
    path: 'uploads',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./uploads/uploads.module').then((m) => m.UploadsModule),
  },
  {
    path: 'about',
    canActivate: [AuthGuard],
    loadChildren: () =>
      import('./about/about.module').then((m) => m.AboutModule),
  },
  { path: '', pathMatch: 'full', redirectTo: '/tasks' },
  { path: '**', component: PageNotFoundComponent },
];

@NgModule({
  imports: [
    RouterModule.forRoot(routes, {
      preloadingStrategy: PreloadAllModules,
    }),
  ],
  exports: [RouterModule],
})
export class AppRoutingModule {}
