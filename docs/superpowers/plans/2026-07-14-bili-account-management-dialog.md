# 投稿账号管理弹窗 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将侧边栏“投稿”入口改为“投稿账号”，并把常驻扫码卡片改成右上角按钮打开的账号添加弹窗。

**Architecture:** 保留 `/uploads` 路由与现有 `UploadsComponent`，使用 ng-zorro `nz-modal` 包裹已有二维码状态机。弹窗显示状态由组件本地布尔值控制，关闭时停止前端轮询并取消服务端会话；登录成功后关闭弹窗、刷新列表并显示成功提示。

**Tech Stack:** Angular 15、TypeScript 4.9、RxJS 7、ng-zorro、Jasmine/Karma。

## Global Constraints

- 不改变 `/uploads` 路由、后端登录接口或账号数据库。
- 侧边栏使用“投稿账号”，页面标题使用“投稿账号管理”，主操作使用“添加账号”。
- 二维码只在用户点击“生成登录二维码”后申请。
- 登录成功自动关闭弹窗；关闭进行中的弹窗必须停止轮询并取消服务端会话。
- 保留头像 `no-referrer`、账号状态、时间、版本说明和按需续期行为。

---

### Task 1: 重命名独立导航入口

**Files:**
- Modify: `webapp/src/app/app.component.html`
- Test: `webapp/src/app/app.component.spec.ts`

**Interfaces:**
- Consumes: 现有 `/uploads` 懒加载路由。
- Produces: 侧边栏文字和折叠提示“投稿账号”。

- [ ] **Step 1: 写导航失败测试**

```typescript
it('labels the uploads navigation as Bilibili accounts', () => {
  const fixture = TestBed.createComponent(AppComponent);
  fixture.detectChanges();

  const link = fixture.nativeElement.querySelector(
    'a[href="/uploads"]'
  ) as HTMLAnchorElement;
  expect(link.textContent?.trim()).toBe('投稿账号');
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts'`

Workdir: `webapp`

Expected: FAIL，现有链接文字仍为“投稿”。

- [ ] **Step 3: 修改导航文字**

```html
<li
  nz-menu-item
  nzMatchRouter="true"
  nz-tooltip
  nzTooltipPlacement="right"
  [nzTooltipTitle]="collapsed ? '投稿账号' : ''"
>
  <i nz-icon nzType="cloud-upload" nzTheme="outline"></i>
  <span><a routerLink="/uploads">投稿账号</a></span>
</li>
```

- [ ] **Step 4: 运行导航测试并提交**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts'`

Expected: PASS。

```bash
git add webapp/src/app/app.component.html webapp/src/app/app.component.spec.ts
git commit -m "feat: rename Bilibili account navigation"
```

### Task 2: 将扫码登录改为添加账号弹窗

**Files:**
- Modify: `webapp/src/app/uploads/uploads.component.html`
- Modify: `webapp/src/app/uploads/uploads.component.ts`
- Modify: `webapp/src/app/uploads/uploads.component.scss`
- Modify: `webapp/src/app/uploads/uploads.module.ts`
- Test: `webapp/src/app/uploads/uploads.component.spec.ts`

**Interfaces:**
- Consumes: `BiliAccountService.createQrSession/getQrSession/cancelQrSession` 与现有 `LoginView`。
- Produces: `loginDialogVisible: boolean`、`openLoginDialog()` 和 `closeLoginDialog()`。

- [ ] **Step 1: 写弹窗交互失败测试**

```typescript
it('opens account login without creating a QR code automatically', () => {
  fixture.detectChanges();

  const addButton = fixture.nativeElement.querySelector(
    '[data-testid="add-account"]'
  ) as HTMLButtonElement;
  addButton.click();
  fixture.detectChanges();

  expect(component.loginDialogVisible).toBeTrue();
  expect(accountService.createQrSession).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain('生成登录二维码');
});

it('cancels a pending login when the dialog closes', fakeAsync(() => {
  fixture.detectChanges();
  component.openLoginDialog();
  component.startLogin();
  tick();

  component.closeLoginDialog();
  tick();

  expect(accountService.cancelQrSession).toHaveBeenCalledOnceWith('session-1');
  expect(component.loginDialogVisible).toBeFalse();
}));

it('closes the dialog and reloads accounts after confirmation', fakeAsync(() => {
  accountService.getQrSession.and.returnValue(
    of({ ...pending, state: 'confirmed', qrUrl: null, accountId: 7 })
  );
  fixture.detectChanges();
  component.openLoginDialog();
  component.startLogin();
  tick(1000);

  expect(component.loginDialogVisible).toBeFalse();
  expect(component.actionMessage).toBe('账号添加成功');
  expect(accountService.listAccounts).toHaveBeenCalledTimes(2);
}));
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/uploads.component.spec.ts'`

Workdir: `webapp`

Expected: FAIL，现有扫码区域仍为常驻卡片，且没有弹窗状态。

- [ ] **Step 3: 实现最小弹窗状态**

```typescript
loginDialogVisible = false;

openLoginDialog(): void {
  this.stopQrPolling$.next();
  this.loginView = { state: 'idle' };
  this.loginDialogVisible = true;
  this.changeDetector.markForCheck();
}

closeLoginDialog(): void {
  const display = this.visibleQr;
  this.stopQrPolling$.next();
  this.loginDialogVisible = false;
  if (display && this.canCancelLogin) {
    this.accountService.cancelQrSession(display.session.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe();
  }
  this.loginView = { state: 'idle' };
  this.changeDetector.markForCheck();
}
```

`startLogin()` 的请求链增加 `takeUntil(this.stopQrPolling$)`，避免用户在二维码创建期间关闭弹窗后仍启动隐藏轮询。确认状态设置 `loginDialogVisible = false` 和 `actionMessage = '账号添加成功'`。

- [ ] **Step 4: 更新模板、样式与模块**

页面使用 `nz-page-header-extra` 放置“添加账号”按钮；账号卡片改为全宽；原登录状态内容移入无 footer 的 `nz-modal`。在 `UploadsModule` 导入 `NzModalModule`，删除不再使用的双栏样式与成功后“继续添加账号”按钮。

- [ ] **Step 5: 运行上传测试和目录 lint**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Run: `npx eslint 'src/app/uploads/**/*.ts' 'src/app/uploads/**/*.html'`

Workdir: `webapp`

Expected: 10 项测试 PASS，lint 退出码为 0。

- [ ] **Step 6: 生产构建并提交源码和生成包**

Run: `npm run build`

Workdir: `webapp`

Expected: production build 成功，生成新的 uploads/main/runtime 哈希文件和 `ngsw.json`。

```bash
git add webapp/src/app/uploads src/blrec/data/webapp
git commit -m "feat: add Bilibili account login dialog"
```

### Task 3: 本机交互验证

**Files:**
- Test only; no new production files.

**Interfaces:**
- Consumes: 当前 `localhost:4200` Angular 开发服务和 `localhost:2233` 后端。
- Produces: 可由用户直接检查的投稿账号页面。

- [ ] **Step 1: 确认开发服务热更新成功**

检查 Angular 输出包含 `Compiled successfully`，并确认端口 4200、2233 各只有一个监听进程。

- [ ] **Step 2: 检查工作区和最终测试**

Run: `git diff --check`

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Expected: 无差异格式错误，上传模块测试全部通过；用户自己的 `AGENTS.md` 保持未跟踪且未提交。
