# 产品目录

本仓库按可独立构建、版本化和部署的产品组织。共享仓库不代表共享发布周期。

| 产品 | 目录 | 产物 | 发布标签 |
|---|---|---|---|
| BLREC Server | `apps/blrec-server/` + `src/blrec/` | 服务端镜像（内含管理端） | `server-v*` |
| Browser Extension | `apps/browser-extension/` | Chromium 扩展 ZIP | `extension-v*` |
| Vision Lab | `apps/vision-lab/` | Python wheel / sdist | `vision-lab-v*` |
| Analysis Worker | `apps/analysis-worker/` | 含模型的 Mac Worker wheel / sdist | `worker-v*` |
| Public Dashboard | `apps/public-dashboard/` | 静态站点 ZIP、Publisher wheel 与镜像 | `dashboard-v*` |

每个产品拥有自己的依赖清单、构建入口、测试工作流和发布工作流。跨产品协作应通过
API、数据格式或版本化模型产物完成，不得通过读取另一个产品的源码资源来完成构建。

## 产品关系

- Vision Lab 冻结数据集并产出不可变模型包；Analysis Worker 显式安装模型包。
- Analysis Worker 通过版本化分析协议从 BLREC Server 领任务、回传结果，不直连生产库。
- Browser Extension 只通过受限 HTTP API 访问 BLREC Server，构建时不读取管理端资源。
- Public Dashboard Publisher 只读生产 SQLite，并把规范化对局增量写入独立 API；公开站点从 API 读取榜单与分页对局，OSS JSON 只保留为故障回退。

`src/blrec/vainglory/` 保留 Server 与 Worker 都要理解的分析领域对象、算法核心和
HTTP 协议编解码；产品运行入口、依赖、模型、静态资源和发布配置归各自 `apps/`
目录。Server 的最终镜像不会安装或携带 Worker 的推理运行时与模型。

边界决策和分阶段迁移计划见
[`docs/adr/0002-independent-product-artifacts.md`](../docs/adr/0002-independent-product-artifacts.md)。
