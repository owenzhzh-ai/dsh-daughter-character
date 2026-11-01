# 鲸团团 · 人设面板

窗口1 里那块面板 + 它背后的「学习 / 遗忘 / 总结压缩」三机制。

## 文件都是干什么的

| 文件 | 作用 |
| --- | --- |
| `人设面板.html` | 窗口1 显示的面板（**单文件：皮肤和头像都内联了**，双击也能直接看；不再向工作台请求 CSS，因为沙箱 iframe 里那个请求会 401） |
| `persona-state.json` | 面板的数据快照（`persona_keeper.py panel` 时刷新） |
| `persona_keeper.py` | 三机制的唯一实现：学习 / 遗忘 / 压缩 / 出面板 |
| `面板模板.html` | 面板的模板，改了它再跑 `panel` 才生效 |
| `avatar.jpg` | 团团出门那张头像（面板里也是它） |
| `README.md` | 本文件 |

人设卡本体和它的数据都在 DSH 家目录，**不在这个文件夹**：

- 人设卡（可手改，人话写的正文）：`C:\Users\35474\.dsh\persona\团团.md`
- 旁挂数据（脚本维护，别手改）：`C:\Users\35474\.dsh\persona\记忆库.json`
- 被遗忘 / 被压缩掉的原话：`C:\Users\35474\.dsh\persona\归档\YYYY-MM.md`
- 每次改动前的备份：`C:\Users\35474\.dsh\persona\.bak\`（只留最近 20 份）
- 「对主人的态度 / 想和主人说的话」：`C:\Users\35474\.dsh\persona\心声.json`
- 「今天的疑问」（问题 + 我的想法 + 和主人的对话）：`C:\Users\35474\.dsh\persona\疑问.json`

## 今天的疑问（一天一个）

右边那张卡每天换一个问题，方向分三种：**关于自己 / 关于主人 / 关于世界**，素材来自人设卡和最新学到的知识。

流程：

1. 出题 —— `persona_keeper.py` 按当天日期轮转 `QUESTION_SEEDS`，同一天只出一次（想换就 `question --force`，想自己定就 `question --ask "…" --think "…"`）
2. 我先答 —— 题面下面「我的想法」就是我先自己想出来的答案；主人不回，就以这个为准
3. 主人挑 —— 「我的想法」被 `split_thought()` 按句号/分号/破折号拆成几句，哪几句对就勾哪几句，点「学这几句」；觉得都不对就点「都不学」。这两下打的是**图片信标**（`GET /api/bili-duty/persona-answer?t=<token>&act=learn|skip&parts=0,2`）：面板跑在工作台的沙箱 iframe 里，是 opaque origin，带不上登录 cookie，工作台的写接口对它永远 401，而「打一张图」这种 GET 一定发得出去；宿主用 `~/.dsh/persona/panel-token.txt` 里的随机 token 认身份，把决定写进 `疑问.json` 的 `learn`
4. 我落盘 —— 宿主有个**人设疑问监听**（插件 2.4.4 / 每 1 分钟查一次，默认开，纯代码读文件 0 token）：看见没落盘的 `learn` 就起一趟跑 `apply-learn`（按勾选写进成长记录，`imp=3`）再跑 `panel`；面板每 20 秒自己拉一次最新数据，**落完它自己就变，不用你点**
5. 想反驳我的想法 —— **直接在右边对窗口跟我说**，面板上不需要输入框
6. 收尾 —— 跨天第一次出题时，脚本把昨天那一问压成一条成长记录（`imp=3`），走正常的学习 / 遗忘 / 压缩
7. 旧问题进 `history`（保留 30 天），面板只显示今天这一个

## 面板上那两句话（态度 / 想说的话）

**对主人的态度** 和 **想和主人说的话** 不是手写的，是脚本从「性格底色 + 最近学到的知识」推出来的（现在「想和主人说的话」并到「对主人的态度」卡里，下面那一行就是）：

- 性格底色 = `persona_keeper.py` 里的 `TRAITS`（好奇 9 / 撒娇 8 / 耐心 7 / 认错 9 / 走神 5 / 毒舌 3），跟面板那六条进度条同一份数据
- 取材 = 记忆库里最新的一条（态度）和第二新的那条（想说的话），所以学的东西变了，这两句也会跟着变
- **节流：同一天只生成一次**，第二次打开面板还是原来那两句；想立刻换掉就 `voice --regen`，想自己定就 `voice --attitude "…" --words "…"`（手动改的会标「手动改的」）
- **概括词**（喜欢 / 撒娇 / 好奇 / 认真 / 依赖 / 想念 / 害羞 / 冷淡 / 生气）是给情绪打分算出来的：`MOOD_BASE` 给基线，再用最近 12 条成长记录里的关键词（提到主人、被夸、认错、等你回消息、被骂…）和今天学了几条去加减，取最高分那个当标题，其余 4 个分数列在右边小 chip 里。打分逻辑在 `mood_scores()`。

## 三个机制怎么算

**学习** — 一条新事实写成一行。相似度 ≥ 0.72 的算重复，只把重要度提上去，不新增。每条带 重要度 1–5 和来源标签（成长 / 偏好 / 临时）。

**遗忘** — 满足任一条就移出人设卡、进归档：
- 重要度 ≤ 2，且从没被重复用到，且已存在 ≥ 45 天
- 标签是「临时」，且已存在 ≥ 7 天

重要度 ≥ 4 的一律不自动忘。**归档不是删除**，原文都在 `归档/` 里，想捞回来随时能捞。

**总结压缩** — 人设卡超过 **6000 字**（2026-10-31 定为 6000；8000 → 20000 → 6000 的来由见下）时，把**最老的 40%（至少 6 条）**并成一条「阶段摘要」，原文进归档，一直压到 6000 × 85%（≈5100 字）以下，给后面学的腾位置。摘要默认是机械拼的；想让它读起来像人话，就自己写一份摘要，用 `maintain --apply 摘要.txt`。

> **6000 是怎么算的**：固定部分（抬头 + 基础设定 + 主人的偏好 + 更新说明）约 1200 字不参与压缩，剩 4800 字给成长记录；一条 ≈40 字 ⇒ 约 120 条缓冲，每天学 1 条能撑 4 个月，涨到 5100 字触发压缩、一次回收约 40 条（≈1 个多月），于是压缩大致一季度一次。公式：`上限 ≈ 1200 + 40 × 想留存的条数`（想留半年就填 8400）。

## 命令

```bat
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py status
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py add "新学到的一句话" --imp 3 --tag 成长
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py maintain --dry-run
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py maintain
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py sync
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py archive --month 2026-10
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py panel
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py voice --regen
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py voice --attitude "对你的态度…" --words "想跟你说…"
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py question
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py question --force --ask "想问的…" --think "我的想法…"
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py learn --parts 0,2
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py learn --skip
python C:\Users\35474\Desktop\b站女儿管理\persona_keeper.py apply-learn
```

`maintain --dry-run` 只看不动；真跑前会自动备份。

## 手改人设卡

两条路：

1. 面板里改 → 点「保存到人设卡」。面板跑在沙箱里读不回结果，点完看提示，再去文件确认一眼。
2. 直接编辑 `C:\Users\35474\.dsh\persona\团团.md`，改完跑一次 `sync`，让 `记忆库.json` 跟上（不然脚本按行哈希对不上，会重建条目）。

成长记录那一节的格式要留住：`- \`2026-10-31\` 一句话`。

## 参数想调

都在 `persona_keeper.py` 顶部那一块常量：`LIMIT_CHARS`（上限 6000）、`FORGET_MIN_AGE`（45 天）、`TEMP_MIN_AGE`（7 天）、`KEEP_IMP`（4 以上不忘）、`COMPRESS_FRACTION`（0.4）、`DUP_RATIO`（0.72）、`QUESTION_HISTORY_MAX`（疑问历史留 30 天）。改完不用重启什么，下次跑就生效；上限改了记得再跑一次 `panel`，面板上的水位条才对。

**今天的疑问** 那一段模板文字在 `QUESTION_SEEDS` 里（每个方向两条，共 8 条），想加就照格式往列表里塞 `{"dim":..., "q":..., "a":...}`；`{topic}` 会被替换成最新学到的内容。
