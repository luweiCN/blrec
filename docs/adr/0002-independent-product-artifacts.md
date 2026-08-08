# 五个产品使用独立交付物与部署生命周期

Status: accepted

BLREC 单仓库保留五个产品边界：BLREC Server、Analysis Worker、Public Dashboard、Browser Extension 与 Vision Lab。每个产品拥有自己的依赖、版本入口、构建、测试、发布和部署配置；一个产品的制品不得以另一个产品的运行镜像为基础，也不得因为共享源码而携带对方的模型、工具链或平台依赖。

## Considered Options

- 单个 Python wheel 和全功能容器部署简单，但会把只在 Mac Worker 使用的推理依赖与模型带到 NAS，并把所有产品绑定到同一版本和发布窗口。
- 拆成多个仓库可以形成强边界，但会增加共享协议变更、原子提交和当前并行开发的协调成本；现阶段保留单仓库，用产品目录和版本化契约隔离。
- Dashboard 页面与数据发布器属于同一产品，但部署介质不同，必须分别构建；数据发布器不能继承 BLREC Server 镜像。

## Consequences

- BLREC Server 镜像包含服务端与外部管理端，不包含 ONNX 模型、OpenCV、ONNX Runtime、RapidOCR 或 Analysis Worker 命令。
- Analysis Worker 独立拥有命令入口、推理依赖、三个 ONNX 模型、英雄参考图、Mac 部署模板和 `worker-v*` 发布周期。
- Public Dashboard 的 Angular 站点与数据 Publisher 位于同一产品目录但分别构建；Publisher 从 Python slim 构建，不能继承 Server 镜像。
- Vision Lab 的样本、数据集、虚拟环境和模型权重位于外部工作目录，不进入源码包或发布制品。
- 源码层允许 Server 与 Worker 共用分析领域对象和纯 Python 算法核心；制品层只共享版本化协议与数据格式，Server 不安装推理依赖或模型。
- 五个产品的依赖、测试、版本入口和正式发布均由各自 GitHub Workflow 管理；本机不构建正式镜像或发布包。
