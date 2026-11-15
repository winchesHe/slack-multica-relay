# 每日 Bug 值守的固定回复格式

首次完整交付、首次受阻和后续简报统一用 `scripts/duty-slack-format.py` 生成 Slack 正文。生成器只排版，不发送、不读取凭据、不决定任务状态或授权。业务结论由实际证据决定，不能为满足模板补造结果。

固定外观为加粗标题、加粗栏目名、原生项目列表和可点击链接。生成原生 `rich_text`，不再让各次运行自行选 Markdown、mrkdwn 或附件说明来呈现同一正文。保留原有三种内容模板：

| kind | 必填 sections，按展示顺序 |
| --- | --- |
| delivered | story、cause、scope、impact、prs、acceptance、next |
| blocked | story、cause、progress、blocker、conclusion、prs、next |
| update | prs、acceptance |

## 输入

根字段包含 `kind`、`summary`、`issue`、`sections`。首次消息额外包含 `current`、`total`、`source`；后续简报额外包含实际 `version`，不填首次字段。`issue` 必须指向当前任务的 `/grm/issues/GRM-编号`，不能使用 UUID 路径或内部 mention 协议。

每个 section 是 1–10 条内容的数组。每条为纯文字，或由文字、`{"code":"行内代码"}`、`{"label":"链接标题","url":"完整 HTTPS 地址"}` 组成的数组。不要在纯文字里写 Markdown 标记、裸链接、换行或 @；多条内容拆成数组。缺少证据时写清“未核实／未验证”，不删掉必填栏目。

`notes` 是首次消息可选的补充说明数组，最多 3 条，用于已核实的业务归属或负责人缺口；后续简报不填。`notify` 是已核实、已获授权的 Slack user ID 数组，生成原生 user 元素；受阻结果只填 Prompt 指定的审查人。生成器不授予通知权限。

以下仅为输入示例，不能作为真实业务结论发送：

```json
{
  "kind": "delivered",
  "summary": "未知性别显示为空",
  "current": 4,
  "total": 7,
  "source": {"label": "客户反馈", "url": "https://example.com/feedback/1"},
  "issue": {"label": "GRM-1938", "url": "https://multica.devops.moego.dev/grm/issues/GRM-1938"},
  "sections": {
    "story": ["未填写性别时错误显示为 Female。"],
    "cause": [[{"code": "toGenderName"}, " 将非 Male 值统一转换为 Female。"]],
    "scope": ["仅映射合法性别，未知值保持为空。"],
    "impact": ["覆盖空值和合法性别展示。"],
    "prs": [[{"label": "server PR #12", "url": "https://github.com/example/server/pull/12"}]],
    "acceptance": ["按实际动态观察填写结论与未覆盖范围。", "完整 HTML 验收报告见本 thread 附件。"],
    "next": ["按已核实的队列状态填写下一步。"]
  }
}
```

## 生成、发送与核对

```bash
python3 <固定版本脚本>/duty-slack-format.py \
  --input <任务私有目录>/reply.json --output-dir <任务私有目录>/reply
python3 <slack-skill>/scripts/slack.py send \
  --channel <交付频道> --thread-ts <已有根消息时间戳> \
  --text-file <任务私有目录>/reply/fallback.txt \
  --blocks-file <任务私有目录>/reply/blocks.json --as bot --dry-run
```

没有既有 thread 时省略 `--thread-ts`，取得首次根消息后保存。核对预览 actor、频道/thread、正文、任务和 PR 链接、通知清单，再按 Slack Skill 正式发送；不能手改生成文件，修订必须改输入后重新生成与预检。生成失败时修正输入，不降级为附件说明或手拼 blocks。

正文成功后，将完整 HTML 报告经 `files_upload` 上传到同一 thread。附件说明仅用单行纯文本，例如“完整 HTML 验收报告。”，不承载正文、Markdown 或通知。发送结果不明先回读，不重复发正文或上传报告。

写后按消息 ID 回读 `raw.blocks`：标题与栏目使用 `style.bold`，条目为 `rich_text_list`，链接为原生 `link`，通知为原生 `user`；顺序、内容、链接目标与生成文件相同，附件仍在同一 thread。不能仅凭发送成功、fallback 文本或 Markdown 化的 `text_rendered` 判断排版正确。原始 text 元素不得出现未解析的格式标记或字面量反斜线 n。核对失败不记作完成，保留真实回执并处理具体差异。

修复历史消息时原位 `edit --text-file ... --blocks-file ...`，先读取原作者、正文与附件并核对授权，遵循 Slack Skill 的编辑预检与结果未知处理。保留原消息时间戳、thread、附件、通知对象和业务结论，不因格式调整推进队列或重发报告。
