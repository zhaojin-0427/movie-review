# 执行端 OSS 交付协议

## 注册和领单

- 保留设备 Token、节点标识、实例 ID、心跳和领取接口。支持 OSS 交付的版本在 `POST /api/v1/workers/register` 的 `capabilities` 增加布尔值 `oss_delivery: true`。升级后发起新的注册。支持同机审片时继续发送 `X-Film-Worker-Review-Storage: node_local_v1`。支持增量返修时同时发送 `X-Film-Worker-Review-Upload: rejected_frames_v1`。
- `POST /api/v1/work/claim` 可能在原有字段之外返回 `oss`。缺少 `oss` 时继续走现有下载和上传协议。
- 有 `oss` 时从 `oss.source_url` 直接下载原片，仍与冻结输入中 `source_video.sha256`、`size_bytes` 对比。下载请求无需设备 Token 或 OSS 凭据。
- 其他冻结输入仍从原有 `/api/v1/artifacts/{artifact_id}/content` 获取。若收到 302，则向 `Location` 发起新的 GET。设备 Token 和工单租约头只发给 247。校验工件大小与摘要。
- `oss.delivery` 是当前领取冻结的交付方式，值为 `auto` 或 `manual`。自动模式下 `oss.upload` 提供 `region`、`bucket`、`key`。手动模式下 `oss.upload` 为 `null`。
- `oss` 位于 `input_manifest` 之外，冻结清单及摘要计算方式维持原有协议。

## 打包产物

产物类型和内容保持原协议。把原有产物文件打成一个外层 ZIP，根目录放 `submission.json`，其余文件由 `outputs` 的路径索引。ZIP 使用 ZIP64，并对已经压缩的视频、JPEG 和内层 ZIP 使用 STORE，减少重复压缩。外层 ZIP 上限为 16 GiB，包含的全部文件总展开大小同样最多 16 GiB。

示例为同机审片首版：

```json
{
  "job_id": "15",
  "work_id": "work_0123456789abcdef",
  "attempt": 1,
  "input_manifest_sha256": "<领单返回的 SHA-256>",
  "outputs": {
    "source_timeline": "out/timeline.json",
    "review_bundle": "out/review.zip",
    "shot_metadata": "out/meta.jsonl",
    "version_manifest": "out/version.json",
    "production_report": "out/production.json"
  }
}
```

- 首版 `prepare` 包含示例中的五项。返修 `revise` 沿用基版的时间线，省去 `source_timeline`。
- 集中存储的首版和返修另含 `scene_bundle`。同机审片的内层 `review.zip` 含索引和对比 JPG，工程和无损白模帧留在领取该作业的实例。如果领取清单包含 `review_upload: rejected_frames_v1`，按增量协议提交 `review_delta` 和完整版本清单。`review_delta.frames` 按帧号列出本次提交的帧，须包含所有打回帧，可附带其他受影响帧，也可列出并提交全帧。云端从基版补齐未提交的帧。未声明该协议时返修提交全帧。
- 出片 `finalize` 的 `outputs` 包含 `white_video`、`comparison_video`、`delivery_bundle`。把原有出片请求的 `final_verification` 放在 `submission.json` 顶层，原字段及摘要照旧。
- 外层每个产物角色有一个独立文件。ZIP 中只能放索引与其列出的产物。版本 JSON、审核 ZIP 索引、时间线和交付 ZIP 的现有校验规则照旧。
- 工单退回待领后，新领取批次会有新目录及 `attempt`，重新生成交付包并使用新批次。保留本机同机审片的工程与历史产物供后续返修、出片使用。

## 自动上传

1. 用在本机保存的固定 RAM AccessKey ID / Secret 和 OSS SDK，上传单个外层 ZIP 到领取返回的精确 `bucket` 与 `key`，使用 HTTPS 与 V4 签名，支持 multipart 断点续传。上传期间继续续租。凭据使用系统密钥存储或本机受限配置保存。
2. 上传成功后调用 `POST /api/v1/work/{work_id}/oss/submit`。保留设备 Bearer Token、`Idempotency-Key`、`X-Lease-Id`、`X-Fence`、`X-Attempt`。JSON 提交 `input_manifest_sha256`、ZIP 的 `size_bytes` 和小写十六进制 `sha256`。
3. `202 {"state":"verifying","transfer_id":"..."}` 表示已进入云端校验。收到 202 后结束该次续租，改为轮询 `GET /api/v1/jobs/{job_id}/status?work_id={work_id}`。看到 `work_state=succeeded` 和回执后再显示提交成功。校验不通过会显示 `failed`，后续按现有管理流程处理。相同请求重试使用同一幂等键。

## 手动交付

生成同一种 ZIP 后调用相同的 `/oss/submit`，JSON 只传 `input_manifest_sha256`。收到 `202 {"state":"awaiting_manual"}` 后结束续租，在执行端显示「待手动交付」和本地 ZIP 路径。管理员可通过微信、网盘等渠道取到文件，再到对应工单上传。等待期间可继续通过作业状态接口查询。
