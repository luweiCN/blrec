# Notification Navigation and Page Width Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将通知设置迁移为侧边主导航一级页面，并把七个一级页面统一到居中的 `1180px` 内容容器。

**Architecture:** 新 `NotificationsModule` 独占通知首页、六个渠道详情和通知解析器；系统设置模块恢复为无 Tab 的系统表单。全局 `.primary-page` 类提供唯一页面宽度，表格只在自身区域滚动。

**Tech Stack:** Angular 15、TypeScript、SCSS、ng-zorro、Jasmine/Karma。

## Global Constraints

- 侧边顺序固定为：录制任务、上传任务、投稿账号、网络管理、设置、通知设置、关于。
- 通知首页为 `/notifications`；原 `/settings/*-notification` 必须重定向。
- 所有一级页面最大宽度固定为 `1180px`，窄屏宽度为 `100%`。
- 抽屉、弹窗和播放器宽度不受一级页面容器影响。
- 不移动现有通知组件目录，不重写各页面内部视觉样式，不使用 worktree。

---

### Task 1: 独立通知模块与字段解析器

**Files:**
- Create: `webapp/src/app/notifications/notifications.component.ts`
- Create: `webapp/src/app/notifications/notifications.component.html`
- Create: `webapp/src/app/notifications/notifications.component.scss`
- Create: `webapp/src/app/notifications/notifications.component.spec.ts`
- Create: `webapp/src/app/notifications/notifications-routing.module.ts`
- Create: `webapp/src/app/notifications/notifications.module.ts`
- Create: `webapp/src/app/notifications/shared/notifications.resolver.ts`
- Create: `webapp/src/app/notifications/shared/notifications.resolver.spec.ts`
- Modify: `webapp/src/app/settings/settings.module.ts`

**Interfaces:**
- Produces: lazy module `NotificationsModule`；`NotificationsResolver` 返回六个渠道设置和 `operationalNotifications`。

- [ ] **Step 1: 写通知首页与解析字段失败测试**

```typescript
expect(settingService.getSettings).toHaveBeenCalledOnceWith([
  'emailNotification',
  'serverchanNotification',
  'pushdeerNotification',
  'pushplusNotification',
  'telegramNotification',
  'barkNotification',
  'operationalNotifications',
]);
expect(fixture.nativeElement.textContent).toContain('通知设置');
```

- [ ] **Step 2: 运行测试确认缺少模块**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/notifications/**/*.spec.ts'`
Expected: FAIL，目录和模块尚不存在。

- [ ] **Step 3: 实现通知模块**

```typescript
const routes: Routes = [
  { path: 'email-notification', component: EmailNotificationSettingsComponent,
    resolve: { settings: EmailNotificationSettingsResolver } },
  { path: 'serverchan-notification', component: ServerchanNotificationSettingsComponent,
    resolve: { settings: ServerchanNotificationSettingsResolver } },
  { path: 'pushdeer-notification', component: PushdeerNotificationSettingsComponent,
    resolve: { settings: PushdeerNotificationSettingsResolver } },
  { path: 'pushplus-notification', component: PushplusNotificationSettingsComponent,
    resolve: { settings: PushplusNotificationSettingsResolver } },
  { path: 'telegram-notification', component: TelegramNotificationSettingsComponent,
    resolve: { settings: TelegramNotificationSettingsResolver } },
  { path: 'bark-notification', component: BarkNotificationSettingsComponent,
    resolve: { settings: BarkNotificationSettingsResolver } },
  { path: '', pathMatch: 'full', component: NotificationsComponent,
    resolve: { settings: NotificationsResolver } },
];
```

`NotificationsComponent` 从 `ActivatedRoute.data.settings` 取数据，模板使用标准 `nz-page-header` 和 `<app-notification-settings>`。将全部通知专用 declarations/providers 从 `SettingsModule` 移到 `NotificationsModule`，同一组件不得由两个模块声明。

- [ ] **Step 4: 运行聚焦测试并提交**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/notifications/**/*.spec.ts' --include='src/app/settings/notification-settings/notification-settings.component.spec.ts'`
Expected: PASS。

```bash
git add webapp/src/app/notifications webapp/src/app/settings/settings.module.ts
git commit -m "feat: add notification settings page"
```

### Task 2: 系统设置去 Tab 与新旧路由

**Files:**
- Modify: `webapp/src/app/app-routing.module.ts`
- Modify: `webapp/src/app/settings/settings-routing.module.ts`
- Modify: `webapp/src/app/settings/settings.component.html`
- Modify: `webapp/src/app/settings/settings.component.scss`
- Modify: `webapp/src/app/settings/settings.component.spec.ts`
- Modify: `webapp/src/app/settings/shared/services/settings.resolver.ts`
- Modify: `webapp/src/app/settings/shared/services/settings.resolver.spec.ts`

**Interfaces:**
- Consumes: `NotificationsModule`。
- Produces: `/notifications` lazy route；旧 `/settings/<channel>-notification` 绝对重定向。

- [ ] **Step 1: 写失败路由和设置页测试**

```typescript
expect(fixture.nativeElement.querySelector('nz-tabset')).toBeNull();
expect(fixture.nativeElement.textContent).not.toContain('通知设置');
expect(router.config.find((route) => route.path === 'notifications')).toBeDefined();
```

解析器测试必须断言系统设置请求不再包含任何 `*Notification` 或 `operationalNotifications` 字段。

- [ ] **Step 2: 运行聚焦测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/settings/settings.component.spec.ts' --include='src/app/settings/shared/services/settings.resolver.spec.ts' --include='src/app/app.component.spec.ts'`
Expected: FAIL，现有设置页仍使用 Tab。

- [ ] **Step 3: 实现路由与模板拆分**

```typescript
{
  path: 'notifications',
  canActivate: [AuthGuard],
  loadChildren: () =>
    import('./notifications/notifications.module').then((m) => m.NotificationsModule),
}
```

设置模板只保留 `.settings-page-content` 和十个系统 section。六个旧渠道路径使用 `redirectTo: '/notifications/<same-path>'` 和 `pathMatch: 'full'`。

- [ ] **Step 4: 运行聚焦测试并提交**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/settings/**/*.spec.ts' --include='src/app/notifications/**/*.spec.ts'`
Expected: PASS。

```bash
git add webapp/src/app/app-routing.module.ts webapp/src/app/settings webapp/src/app/notifications
git commit -m "refactor: separate notification settings routes"
```

### Task 3: 侧边导航与图标

**Files:**
- Modify: `webapp/src/app/app.component.html`
- Modify: `webapp/src/app/app.component.spec.ts`
- Modify: `webapp/src/app/icons-provider.module.ts`

**Interfaces:**
- Produces: `/notifications` 侧边链接和 `BellOutline` 图标注册。

- [ ] **Step 1: 写失败导航顺序测试**

```typescript
expect(labels).toEqual([
  '录制任务', '上传任务', '投稿账号', '网络管理',
  '设置', '通知设置', '关于',
]);
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts'`
Expected: FAIL，缺少通知设置链接。

- [ ] **Step 3: 增加真实路由链接和折叠提示**

```html
<li nz-menu-item nzMatchRouter="true" nz-tooltip
    [nzTooltipTitle]="collapsed ? '通知设置' : ''">
  <i nz-icon nzType="bell" nzTheme="outline"></i>
  <span><a routerLink="/notifications">通知设置</a></span>
</li>
```

- [ ] **Step 4: 运行测试并提交**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts'`
Expected: PASS。

```bash
git add webapp/src/app/app.component.html webapp/src/app/app.component.spec.ts webapp/src/app/icons-provider.module.ts
git commit -m "feat: add notification navigation"
```

### Task 4: 七个一级页面共享 `1180px` 容器

**Files:**
- Modify: `webapp/src/app/shared/styles/_layout.scss`
- Modify: `webapp/src/styles.scss`
- Modify: `webapp/src/app/tasks/tasks.component.html`
- Modify: `webapp/src/app/tasks/tasks.component.scss`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.html`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.scss`
- Modify: `webapp/src/app/uploads/uploads.component.html`
- Modify: `webapp/src/app/uploads/uploads.component.scss`
- Modify: `webapp/src/app/network/network.component.html`
- Modify: `webapp/src/app/network/network.component.scss`
- Modify: `webapp/src/app/settings/settings.component.html`
- Modify: `webapp/src/app/settings/settings.component.scss`
- Modify: `webapp/src/app/notifications/notifications.component.scss`
- Modify: `webapp/src/app/about/about.component.html`
- Modify: `webapp/src/app/about/about.component.scss`
- Test: `webapp/src/app/tasks/tasks.component.spec.ts`
- Test: `webapp/src/app/upload-tasks/upload-tasks.component.spec.ts`
- Test: `webapp/src/app/uploads/uploads.component.spec.ts`
- Test: `webapp/src/app/notifications/notifications.component.spec.ts`
- Test: `webapp/src/app/about/about.component.spec.ts`

**Interfaces:**
- Produces: `.primary-page`，宽度 `100%`、`max-width: 1180px`、水平居中。

- [ ] **Step 1: 写共享容器失败断言**

录制任务、上传任务、投稿账号、设置、通知设置和关于的组件测试断言根滚动层内存在且只存在一个 `.primary-page`；任务和上传列表仍只渲染一次，避免为布局复制组件。网络页由最终浏览器检查覆盖，因为当前没有页面组件测试基架。

- [ ] **Step 2: 运行一级页面聚焦组件测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/tasks.component.spec.ts' --include='src/app/upload-tasks/upload-tasks.component.spec.ts' --include='src/app/uploads/uploads.component.spec.ts' --include='src/app/settings/settings.component.spec.ts' --include='src/app/notifications/notifications.component.spec.ts' --include='src/app/about/about.component.spec.ts'`
Expected: FAIL，尚无统一容器。

- [ ] **Step 3: 增加全局页面容器并应用到页面**

```scss
.primary-page {
  width: 100%;
  max-width: 1180px;
  margin-right: auto;
  margin-left: auto;
}
```

该类放在 `webapp/src/styles.scss`，七个一级页面的现有语义 wrapper 增加此类。删除网络页重复 `max-width: 1180px`，并把 `%inner-page` 的旧 `680px` 改为 `1180px`，使关于页和通知渠道子页面保持相同边界。任务表格、上传任务表格和通知矩阵在自身 wrapper 使用 `overflow-x: auto`，页面滚动层不得出现水平溢出。

- [ ] **Step 4: 运行聚焦测试并提交**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/**/*.spec.ts' --include='src/app/upload-tasks/**/*.spec.ts' --include='src/app/uploads/uploads.component.spec.ts' --include='src/app/network/**/*.spec.ts' --include='src/app/settings/settings.component.spec.ts' --include='src/app/notifications/**/*.spec.ts'`
Expected: PASS。

```bash
git add webapp/src/styles.scss webapp/src/app/shared/styles/_layout.scss webapp/src/app/tasks webapp/src/app/upload-tasks webapp/src/app/uploads webapp/src/app/network webapp/src/app/settings/settings.component.html webapp/src/app/settings/settings.component.scss webapp/src/app/notifications webapp/src/app/about
git commit -m "style: unify primary page widths"
```

### Task 5: 完整验证与浏览器宽度检查

**Files:**
- Modify generated assets: `src/blrec/data/webapp/`

- [ ] **Step 1: 运行完整 Angular 验证**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless`
Expected: 全部 PASS。

Run: `cd webapp && git diff --name-only --diff-filter=ACMR -- . | rg '\.ts$' | sed 's#^webapp/##' | xargs npx eslint`
Expected: 本轮修改文件 0 error。

Run: `cd webapp && npm run build`
Expected: PASS，仅允许已记录的体积预算和 CommonJS 警告。

- [ ] **Step 2: 实际浏览器冒烟**

在同一桌面窗口依次打开 `/tasks`、`/upload-tasks`、`/uploads`、`/network`、`/settings`、`/notifications`、`/about`，读取各 `.primary-page` 的 `getBoundingClientRect()`；宽屏时宽度均不超过且等于可用的 `1180px`，左右边界一致。打开通知渠道详情和旧设置渠道 URL，确认详情可编辑且旧 URL 正确跳转。

- [ ] **Step 3: 检查差异并提交生成包**

Run: `git diff --check`
Expected: 无空白错误、无凭据、无测试管理员残留。

```bash
git add src/blrec/data/webapp webapp
git commit -m "build: update notification navigation webapp"
```
