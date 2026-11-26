# OSS 逐图直传与整包交付

`objects_v1` 用于经 OSS 上传的新同机审片作业。原有本地工单和 `submission.zip` 兼容链路继续使用各自的协议。

## 目录

```text
video-public/movie-review/sources/{source_id}/source.{ext}
video-gr/movie-review/jobs/{job_id}/review/v{version}/{work_id}/a{attempt}/{upload_id}/
video-gr/movie-review/jobs/{job_id}/delivery/from-v{version}/{work_id}/a{attempt}/{upload_id}/
```

版号、领取次数最少四位，例如 `v0002/a0001`。所有目录由服务端分配。审核目录中 `pairs/000001.jpg` 等文件名用六位帧号。正式记录保存具体的 OSS 对象版本。返修版中未打回的帧指向基版已有的对象版本。

## 注册与领取

- 设备注册在 `capabilities` 中增加 `"oss_objects": true`，用新幂等键注册，并继续发送 `X-Film-Worker-Review-Storage: node_local_v1`。兼容旧 OSS ZIP 流程的设备可另外保留 `oss_delivery: true`。旧增量 ZIP 使用的 `X-Film-Worker-Review-Upload` 与本协议独立。
- 领取响应的 `oss.protocol` 为 `objects_v1` 时走本协议。 `oss.source_url` 为 HTTPS 原片下载链接，下载后核对原片大小和 SHA-256。链接过期时重新访问原有工件下载 API，请求 247 返回最新地址。跟随 302 时，设备 Token 和租约头只发给 247。
- `oss.delivery` 是本次领取的交付方式，`auto` 为自动，`manual` 为手动。首版 `oss.review_scope` 是 `all_frames`，返修是 `rejected_frames`，出片为 `null`。返修范围从 `feedback_manifest.frames` 读取。
- 同机审片继续保留本地工程和白模素材。生成、上传期间照常续租。

## 自动交付

1. `POST /api/v1/work/{work_id}/oss/objects`，请求体为 `{"input_manifest_sha256":"<领单摘要>"}`。返回 `201`，包含 `upload_id`、`region`、`bucket` 和目录 `key`。
2. 使用本地保存的 RAM AK/SK，把文件上传到返回目录下的固定文件名。使用 OSS V4 签名。每个文件设置下表的 `Content-Type`，以及 `x-oss-meta-sha256` 为文件的 SHA-256。分片上传时在初始化请求设置元数据，记录完成响应的 `x-oss-version-id`。每个文件的版本 ID 都要保留。
3. `POST /api/v1/work/{work_id}/oss/submit`。请求体包含 `input_manifest_sha256`、`upload_id`、`objects`。出片再提供原有 `final_verification`。`objects` 的键为目录下的文件名，每项形如 `{"size_bytes":12345,"sha256":"<64 位小写十六进制>","version_id":"<返回的 OSS 对象版本>"}`。
4. 两个 POST 均携带现有设备 Bearer Token、`Idempotency-Key`、`X-Lease-Id`、`X-Fence` 和 `X-Attempt`。收到 `202` 后可结束续租，再轮询 `GET /api/v1/jobs/{job_id}/status?work_id={work_id}`。 `work_state=succeeded` 才表示已入库，`failure_message` 给出校验不通过的原因。工单退回后新领取使用新目录和新上传 ID。

| 文件 | Content-Type | 使用阶段 |
|---|---|---|
| `pairs/000001.jpg` 等 | `image/jpeg` | 首版全帧，返修仅打回帧 |
| `review-index.json`、`version.json`、`production.json` | `application/json` | 首版、返修 |
| `meta.jsonl` | `application/x-ndjson` | 首版、返修 |
| `timeline.json` | `application/json` | 首版 |
| `delivery.zip` | `application/zip` | 出片，必传 |
| `white.mp4`、`comparison.mp4` | `video/mp4` | 出片，可选 |

JPG 原样上传，每张最多 14 MiB。单个清单或元数据文件最多 16 MiB。单个出片对象最多 16 GiB，支持分片续传。客户端应完成对比图画幅、逐帧签名、交付 ZIP 内容、成片像素尺寸、帧数、时序和音轨核验。平台读取小清单并验证其 SHA-256，核对 OSS 对象版本、长度和摘要元数据。JPG 与 ZIP 的完整字节核验由客户端负责。

## 审片清单

`review-index.json` 示例。首版的 `base_manifest_sha256` 为 `null`。返修填领单中基版 `version_manifest` 的 SHA-256。

```json
{
  "job_id": "15",
  "version": 2,
  "source_sha256": "<原片摘要>",
  "timeline_sha256": "<时间线摘要>",
  "input_manifest_sha256": "<领单摘要>",
  "review_storage": {"mode":"node_local_v1","instance_id":"<实例 ID>"},
  "review_media": "objects_v1",
  "base_manifest_sha256": "<基版 version.json 摘要>",
  "frames": [
    {"number": 17, "size_bytes": 123456, "sha256": "<第 17 帧 JPG 文件摘要>"}
  ]
}
```

`version.json` 保留完整版本帧列表及既有时间线、原片、工程和画幅数据。它包含与 `review-index.json` 相同的 `job_id`、`version`、`source_sha256`、`timeline_sha256`、`input_manifest_sha256`、`review_storage` 和 `review_media`。另须提供：

- `review_index_sha256`、`shot_metadata_sha256`、`production_report_sha256`，分别绑定三个已上传文件。
- `production_method: "codex_trial"`、`scene_sha256`、`review_size`、`final_size` 和 `frame_count`。尺寸形如 `{"width":1920,"height":1080}`，`review_size` 是单幅尺寸，对比 JPG 上下拼接。
- 完整的 `frames` 列表，按帧号排序。每项含 `number`、`pts`、`review_signature` 和 `pair_jpeg_path`。 `pair_jpeg_path` 使用六位帧号。保持打回范围外的帧记录与基版逐字段一致。 `review_signature` 继续使用现有的逐帧签名算法。
- 首版 `timeline.json` 沿用现有执行端时间线协议。返修沿用基版时间线。

## 手动交付

执行端生成文件后调用同一个 `/oss/submit`，请求体只传 `input_manifest_sha256`。收到 `awaiting_manual` 后结束续租，显示本地交付路径。管理员在工单详情上传。

- 首版、返修生成一个 ZIP，根目录与自动上传的目录内容相同。浏览器解析 ZIP 并逐文件直传 OSS，保留原始 JPG 字节。
- 出片生成最终 `delivery.zip`，在其根目录包含 `delivery-proof.json`，内容为 `{"input_manifest_sha256":"...","final_verification":{...}}`。浏览器只读取这份小凭证，计算整包 SHA-256，随后整包分片直传 OSS。手动出片登记整包下载。

## 服务端启用

通过 HTTPS 打开「执行管理 → OSS 设置」，交付模式选择「逐图直传 / 成片整包」。 `video-public` 使用公共读，`video-gr` 使用私有并启用版本控制。OSS RAM 权限包括对应前缀的对象写入、分片操作、读取及根据版本读取，还需要用于连接测试的 `oss:GetBucketAcl` 和 `oss:GetBucketVersioning`。保留正式记录引用的对象版本，避免自动过期清理。

浏览器直传的两个 Bucket 需要允许后台站点 Origin 发起 PUT，允许 `Content-Type` 和 `x-oss-meta-sha256` 请求头，审核 Bucket 需将 `x-oss-version-id` 列为允许暴露的响应头。后台通过现有 `oss-process --watch` 队列核验和登记，并可使用部署目录里的队列服务。逐图流程由 247 运行，无需另设云端 ZIP 处理服务。
