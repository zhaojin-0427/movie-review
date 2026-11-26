# OSS 配置与部署

247 保存工单、审核结果及对象索引。浏览器分片上传和后续下载使用北京 OSS。云端处理服务通过 OSS 内网读取暂存对象、验证 ZIP、发布审核图和交付文件。各制作节点可共用一套固定 RAM AK/SK。云端处理服务通过独立的 RAM 身份发布对象。

## Bucket 与权限

- 原片 Bucket `video-public` 为公共读。所有知道对象 URL 的人可以下载原片。审核 Bucket `video-gr` 为私有。
- 网页和设备共用的 RAM 身份在两桶的 `movie-review/incoming/` 下拥有执行分片上传所需的对象权限。后台的「测试连接」需要两桶的 `oss:GetBucketAcl`。后台签发私有读取链接的身份还需对 `video-gr/movie-review/published/` 授予 `oss:GetObject`。桶配置和正式目录写入由云端发布身份管理。
- 云端发布身份可读两个 Bucket 的暂存对象，可写入正式目录 `movie-review/published/`，可读源时间线等已发布对象。ECS 可绑定该 RAM 角色。也支持由云宿主向进程注入 OSS SDK 读取的凭据环境变量。
- 在两个 Bucket 设置 CORS，允许后台公网 HTTPS Origin 发起 PUT。原片浏览器直接预览或需要 JavaScript 读取对象时同时允许 GET。允许浏览器上传请求头，精确配置允许的站点来源。
- 可以为 `movie-review/incoming/` 设置清理规则，包括终止长期未完成的分片和清理过期待处理对象。手动 ZIP 由管理员选中文件时才创建上传会话。正式对象随历史审核保留，数据库备份只保存其对象引用，Bucket 需要单独的归档或版本保留安排。

## 北京云端校验进程

在与 Bucket 同地域的云主机上安装此版本与 `requirements.txt`。为处理单个最大 16 GiB 的交付包，临时目录应留出至少 50 GiB，内存按实际图像尺寸配置。对外以 HTTPS 反向代理静态路径 `/process`，允许最多 48 MiB 请求及最多两小时的处理连接。接口请求体只包含清单和对象位置。

设置 `MOVIE_REVIEW_OSS_PROCESSOR_TOKEN` 为随机共享密钥，并在 247 使用同一个值。云端使用 ECS 角色时设置 `OSS_ECS_ROLE`，或通过部署环境注入 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` 和可选的 `OSS_SESSION_TOKEN`。部署时默认通过 OSS 内网访问对象。测试环境可使用 `OSS_INTERNAL=0`。

```sh
gunicorn --bind 127.0.0.1:18481 --workers 1 --timeout 7200 'oss_processor:create_processor()'
```

## 247 审核后台

1. 安装更新版依赖与应用代码，运行现有数据库升级流程。
2. 在服务环境配置 `MOVIE_REVIEW_OSS_PROCESSOR_URL` 为云端校验 HTTPS 基础地址，`MOVIE_REVIEW_OSS_PROCESSOR_TOKEN` 与校验端一致。
3. 安装并启用 `deploy/movie-review-oss-queue.service`，重载后台服务。队列读取数据库中已经封存的上传批次。
4. 管理员通过 HTTPS 打开「执行管理 → OSS 设置」，填写两个 Bucket、北京地域 `cn-beijing` 和受限 RAM AK/SK。测试连接后保存。该测试检查 OSS ACL，整条链路还需实际上传跑通。
5. 用支持 OSS 的执行端分别验证自动上传和手动交付，包括打回返修及出片下载。页面上的原始逐帧图从 OSS 读取。单独点击圈选图或导出带圈选的反馈 ZIP 时，247 会按需读取问题图生成标注。

未配置云端地址时保留原有上传入口。已入库的本地原片、版本和成片继续使用原来的读取方式。新作业默认选择同机审片。
