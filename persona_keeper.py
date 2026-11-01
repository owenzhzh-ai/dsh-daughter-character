#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""persona_keeper.py —— 鲸团团人设卡的 学习 / 遗忘 / 总结压缩 三件套

人设卡本体（canonical，人可直接手改）：  C:\\Users\\35474\\.dsh\\persona\\团团.md
旁挂数据（脚本维护，别手改）：            C:\\Users\\35474\\.dsh\\persona\\记忆库.json
被遗忘/被压缩的原文：                     C:\\Users\\35474\\.dsh\\persona\\归档\\YYYY-MM.md
每次改动前的备份：                        C:\\Users\\35474\\.dsh\\persona\\.bak\\

用法:
  python persona_keeper.py status                     # 看水位、条数、规则
  python persona_keeper.py add "文本" --imp 3 --tag 偏好  # 学习一条（自动去重）
  python persona_keeper.py maintain --dry-run          # 预演：该忘什么、该压什么
  python persona_keeper.py maintain                    # 真跑：遗忘 + 压缩
  python persona_keeper.py maintain --apply summary.txt # 用我写的摘要替换机械摘要
  python persona_keeper.py sync                        # md <-> json 对齐（手改 md 后跑）
  python persona_keeper.py archive                     # 看归档
  python persona_keeper.py panel                       # 重新生成可视化面板
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PERSONA_DIR = Path(os.environ.get("PERSONA_DIR") or (Path.home() / ".dsh" / "persona"))
MD_PATH = PERSONA_DIR / "团团.md"
STORE_PATH = PERSONA_DIR / "记忆库.json"
ARCHIVE_DIR = PERSONA_DIR / "归档"
BAK_DIR = PERSONA_DIR / ".bak"

PANEL_HTML = HERE / "人设面板.html"
PANEL_TEMPLATE = HERE / "面板模板.html"
PANEL_STATE = HERE / "persona-state.json"
AVATAR = HERE / "avatar.jpg"

# ---- 规则参数（想调就改这里）----
LIMIT_CHARS = 6000           # 人设卡目标上限（字符）
# 2026-10-31：8000 → 20000 → 6000。6000 的算法：固定部分（抬头+基础设定+偏好+更新说明）
# 约 1200 字不参与压缩，剩下 4800 字留给成长记录，一条 ≈40 字 ⇒ 约 120 条缓冲，
# 每天学 1 条能撑 4 个月；涨到 0.85（≈5100 字）触发压缩，一次回收 ≈40 条 ≈ 1 个多月，
# 于是压缩大致一季度一次 —— 不空转，也不至于天天重写。
SOFT_RATIO = 0.85           # 压缩后要落到 limit*0.85 以下
FORGET_MIN_AGE = 45         # imp<=2 且没被用过，活过这么多天才可能被忘
TEMP_MIN_AGE = 7            # tag=临时 的，7 天后忘
KEEP_IMP = 4                # 重要度 >= 4 的永不自动忘
COMPRESS_FRACTION = 0.4     # 一次压掉最老成长记录的 40%
COMPRESS_MIN_ITEMS = 6      # 但至少压 6 条
DUP_RATIO = 0.72            # 相似度超过它就算重复，只提重要度

GROWTH_SECTION = "成长记录"
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
GROWTH_LINE_RE = re.compile(r"^-\s+`?(?P<date>\d{4}-\d{2}-\d{2})`?\s*(?P<text>.+?)\s*$")


# ---------------------------------------------------------------- 基础工具
def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def today() -> str:
    return date.today().isoformat()


def sha(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:12]


def norm(text: str) -> str:
    """归一化：去掉空白与标点，只留字词，用于查重。"""
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE).lower()


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def backup(tag: str) -> Path | None:
    if not MD_PATH.exists():
        return None
    BAK_DIR.mkdir(parents=True, exist_ok=True)
    dst = BAK_DIR / f"团团-{datetime.now():%Y%m%d-%H%M%S}-{tag}.md"
    shutil.copy2(MD_PATH, dst)
    for old in sorted(BAK_DIR.glob("团团-*.md"))[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def archive_lines(lines: list[str], reason: str) -> None:
    if not lines:
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{datetime.now():%Y-%m}.md"
    head = "" if path.exists() else f"# 团团人设归档 · {datetime.now():%Y-%m}\n\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if head:
            fh.write(head)
        fh.write(f"## {datetime.now():%Y-%m-%d %H:%M} · {reason}\n\n")
        for line in lines:
            fh.write(line.rstrip() + "\n")
        fh.write("\n")


# ---------------------------------------------------------------- md 解析
def split_sections(md: str) -> list[dict]:
    """把 md 切成 [{title, head(标题行之前的内容), lines}]，head 只在第 0 段。"""
    out: list[dict] = [{"title": "", "lines": []}]
    for line in md.splitlines():
        m = SECTION_RE.match(line)
        if m:
            out.append({"title": m.group(1), "lines": [line]})
        else:
            out[-1]["lines"].append(line)
    return out


def join_sections(sections: list[dict]) -> str:
    text = "\n".join("\n".join(s["lines"]) for s in sections)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.endswith("\n"):
        text += "\n"
    return text


def growth_section(sections: list[dict]) -> dict | None:
    for sec in sections:
        if sec["title"].startswith(GROWTH_SECTION):
            return sec
    return None


def parse_growth(lines: list[str]) -> list[dict]:
    items = []
    for line in lines:
        m = GROWTH_LINE_RE.match(line.strip())
        if not m:
            continue
        items.append({
            "line": line,
            "born": m.group("date"),
            "text": m.group("text"),
            "id": sha(m.group("text")),
        })
    return items


# ---------------------------------------------------------------- 记忆库
def load_store() -> dict:
    raw = read_text(STORE_PATH, "")
    if raw.strip():
        try:
            data = json.loads(raw)
            data.setdefault("entries", {})
            data.setdefault("stats", {})
            return data
        except json.JSONDecodeError:
            print(f"[warn] 记忆库.json 坏了，重建（原文已备份到 {BAK_DIR}）", file=sys.stderr)
            backup("store-broken")
    return {
        "version": 1,
        "limit": LIMIT_CHARS,
        "updated": now_iso(),
        "entries": {},
        "stats": {"learned": 0, "forgotten": 0, "compactions": 0, "last_compress": ""},
    }


def save_store(store: dict) -> None:
    store["updated"] = now_iso()
    store["limit"] = LIMIT_CHARS
    atomic_write(STORE_PATH, json.dumps(store, ensure_ascii=False, indent=2) + "\n")


def sync_store(store: dict | None = None) -> dict:
    """让 json 里每条都对应 md 里真实存在的一行成长记录。"""
    store = store or load_store()
    md = read_text(MD_PATH)
    sections = split_sections(md)
    sec = growth_section(sections)
    items = parse_growth(sec["lines"]) if sec else []
    seen: dict[str, dict] = {}
    for it in items:
        old = store["entries"].get(it["id"], {})
        seen[it["id"]] = {
            "id": it["id"],
            "text": it["text"],
            "born": old.get("born") or it["born"],
            "tag": old.get("tag") or "成长",
            "imp": int(old.get("imp") or 3),
            "used": int(old.get("used") or 0),
            "learned": old.get("learned") or (it["born"] + "T00:00:00"),
        }
    store["entries"] = seen
    save_store(store)
    return store


# ---------------------------------------------------------------- 学习
def cmd_add(text: str, imp: int = 3, tag: str = "成长") -> int:
    text = " ".join(text.split())
    if not text:
        print("空文本，没学。")
        return 2
    store = sync_store()
    dup_hit = None
    for e in store["entries"].values():
        if similar(text, e["text"]) >= DUP_RATIO:
            dup_hit = e
            break
    if dup_hit:
        dup_hit["imp"] = min(5, max(int(dup_hit["imp"]), imp))
        dup_hit["used"] = int(dup_hit["used"]) + 1
        save_store(store)
        print(f"重复，不新增（已把重要度提到 {dup_hit['imp']}）：{dup_hit['text']}")
        return 0

    md = read_text(MD_PATH)
    sections = split_sections(md)
    sec = growth_section(sections)
    if sec is None:
        sections.append({"title": GROWTH_SECTION, "lines": [f"## {GROWTH_SECTION}", ""]})
        sec = sections[-1]
    line = f"- `{today()}` {text}"
    body = [ln for ln in sec["lines"] if ln.strip()]
    while body and body[-1].startswith("## "):
        body.pop()
    sec["lines"] = body + [line, ""]
    backup("add")
    atomic_write(MD_PATH, join_sections(sections))
    sync_store(store)
    e = store["entries"].get(sha(text))
    if e:
        e["imp"], e["tag"], e["learned"] = imp, tag, now_iso()
        store["stats"]["learned"] = int(store["stats"].get("learned") or 0) + 1
    save_store(store)
    size = len(read_text(MD_PATH))
    print(f"学到了（重要度 {imp} / {tag}）：{text}")
    print(f"人设卡现在 {size} 字（上限 {LIMIT_CHARS}）" + ("，该压缩了，跑 maintain" if size > LIMIT_CHARS else ""))
    return 0


# ---------------------------------------------------------------- 遗忘
def cmd_forget(dry: bool = False, store: dict | None = None) -> list[str]:
    store = sync_store(store)
    md = read_text(MD_PATH)
    sections = split_sections(md)
    sec = growth_section(sections)
    if not sec:
        return []
    today_d = date.today()
    doomed, kept = [], []
    for it in parse_growth(sec["lines"]):
        e = store["entries"].get(it["id"]) or {}
        try:
            age = (today_d - date.fromisoformat(it["born"])).days
        except ValueError:
            age = 0
        imp = int(e.get("imp") or 3)
        used = int(e.get("used") or 0)
        tag = e.get("tag") or "成长"
        reason = ""
        if imp >= KEEP_IMP:
            reason = ""
        elif tag == "临时" and age >= TEMP_MIN_AGE:
            reason = f"临时信息，{age} 天前"
        elif imp <= 2 and used == 0 and age >= FORGET_MIN_AGE:
            reason = f"低重要度且从没被用过，{age} 天前"
        if reason:
            doomed.append((it, reason))
        else:
            kept.append(it["line"])
    if not doomed:
        print("遗忘：这次没有该忘的。")
        return []
    print(f"遗忘：{'预演' if dry else ''}要忘 {len(doomed)} 条")
    for it, reason in doomed:
        print(f"  - [{it['born']}] {it['text'][:60]}  ← {reason}")
    if dry:
        return [it["line"] for it, _ in doomed]
    keep_set = {it["id"] for it, _ in doomed}
    header = [ln for ln in sec["lines"] if ln.strip().startswith("## ")]
    rest = [ln for ln in sec["lines"] if not ln.strip().startswith("## ")]
    new_lines = header + [ln for ln in rest if not any(
        GROWTH_LINE_RE.match(ln.strip()) and sha(GROWTH_LINE_RE.match(ln.strip()).group("text")) == kid
        for kid in keep_set)]
    sec["lines"] = new_lines
    backup("forget")
    atomic_write(MD_PATH, join_sections(sections))
    archive_lines([it["line"] for it, reason in doomed] +
                  [f"<!-- 遗忘理由：{reason} -->" for _, reason in doomed], "遗忘")
    for kid in keep_set:
        store["entries"].pop(kid, None)
    store["stats"]["forgotten"] = int(store["stats"].get("forgotten") or 0) + len(doomed)
    save_store(store)
    print(f"遗忘完成，{len(doomed)} 条进了 归档/{datetime.now():%Y-%m}.md")
    return []


# ---------------------------------------------------------------- 压缩
def mechanical_summary(items: list[dict]) -> str:
    parts = []
    for it in items:
        t = it["text"]
        # 已经是摘要的行别再套一层
        t = re.sub(r"^阶段摘要（[^）]*）[：:]?", "", t.strip())
        t = re.sub(r"[，。；、]", " ", t)
        t = t.split("：")[-1] if "：" in t and len(t) > 24 else t
        t = t.strip()
        if t:
            parts.append(t[:36])
    dates = sorted({it["born"] for it in items})
    span = dates[0] if len(dates) == 1 else f"{dates[0]}~{dates[-1]}"
    return f"`{dates[0]}` 阶段摘要（{span}，原 {len(items)} 条）：" + "；".join(parts[:12])


def cmd_compress(dry: bool = False, summary_text: str = "", store: dict | None = None) -> int:
    store = sync_store(store)
    md = read_text(MD_PATH)
    size = len(md)
    if size <= LIMIT_CHARS and not summary_text:
        print(f"压缩：不用压，现在 {size} 字 ≤ 上限 {LIMIT_CHARS}。")
        return 0
    sections = split_sections(md)
    sec = growth_section(sections)
    items = parse_growth(sec["lines"]) if sec else []
    if len(items) < 3:
        print(f"压缩：成长记录只有 {len(items)} 条，压不动，先靠遗忘。")
        return 0
    oldest = sorted(items, key=lambda it: (it["born"], it["line"]))[: max(COMPRESS_MIN_ITEMS, int(len(items) * COMPRESS_FRACTION))]
    oldest = oldest[: max(1, len(items) - 1)]
    summary = summary_text.strip() or mechanical_summary(oldest)
    new_line = f"- {summary}"
    print(f"压缩：把最老 {len(oldest)} 条并成 1 条（{size} 字 → 预计 {size - sum(len(i['line']) for i in oldest) + len(new_line)} 字）")
    for it in oldest:
        print(f"  - [{it['born']}] {it['text'][:50]}")
    print(f"  = {summary[:120]}")
    if dry:
        return 0
    header = [ln for ln in sec["lines"] if ln.strip().startswith("## ")]
    rest = [ln for ln in sec["lines"] if not ln.strip().startswith("## ")]
    tails = [ln for ln in rest if ln not in {it["line"] for it in oldest}]
    sec["lines"] = header + [new_line, ""] + tails
    backup("compress")
    atomic_write(MD_PATH, join_sections(sections))
    archive_lines([it["line"] for it in oldest], "压缩（原文）")
    for it in oldest:
        store["entries"].pop(it["id"], None)
    store["stats"]["compactions"] = int(store["stats"].get("compactions") or 0) + 1
    store["stats"]["last_compress"] = now_iso()
    save_store(store)
    new_size = len(read_text(MD_PATH))
    print(f"压缩完成，{len(oldest)} 条原文进了 归档/{datetime.now():%Y-%m}.md；现在 {new_size} 字。")
    return new_size


# ---------------------------------------------------------------- 命令
def cmd_maintain(dry: bool = False, apply_file: str = "") -> int:
    summary_text = read_text(Path(apply_file)) if apply_file else ""
    cmd_forget(dry=dry)
    size = len(read_text(MD_PATH))
    if summary_text:
        new_size = cmd_compress(dry=dry, summary_text=summary_text)
    elif size > LIMIT_CHARS:
        new_size = cmd_compress(dry=dry)
        guard = 0
        while not dry and new_size and new_size > LIMIT_CHARS * SOFT_RATIO and guard < 5:
            guard += 1
            new_size = cmd_compress(dry=dry)
    else:
        print(f"压缩：现在 {size} 字（上限 {LIMIT_CHARS}），还不用压。")
    return cmd_status()


def cmd_status() -> int:
    store = sync_store()
    md = read_text(MD_PATH)
    ents = list(store["entries"].values())
    imps = {}
    for e in ents:
        imps[e["imp"]] = imps.get(e["imp"], 0) + 1
    arch = list(ARCHIVE_DIR.glob("*.md")) if ARCHIVE_DIR.exists() else []
    arch_lines = 0
    for a in arch:
        arch_lines += sum(1 for ln in read_text(a).splitlines() if GROWTH_LINE_RE.match(ln.strip()))
    info = {
        "人设卡": str(MD_PATH),
        "字符数": len(md),
        "上限": LIMIT_CHARS,
        "水位": f"{len(md) / LIMIT_CHARS * 100:.0f}%",
        "成长记录条数": len(ents),
        "重要度分布": dict(sorted(imps.items())),
        "归档文件": [a.name for a in arch],
        "归档条数": arch_lines,
        "学习次数": store["stats"].get("learned", 0),
        "遗忘条数": store["stats"].get("forgotten", 0),
        "压缩次数": store["stats"].get("compactions", 0),
        "上次压缩": store["stats"].get("last_compress") or "还没压过",
        "现在该做什么": ("压缩（超上限）" if len(md) > LIMIT_CHARS else
                    "接近上限，留意" if len(md) > LIMIT_CHARS * SOFT_RATIO else "健康"),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_sync() -> int:
    sync_store()
    print("对齐完成：json 跟着 md 走。")
    return cmd_status()


def cmd_archive_month(month: str = "") -> int:
    if not ARCHIVE_DIR.exists():
        print("还没有归档。")
        return 0
    files = sorted(ARCHIVE_DIR.glob(f"{month}*.md")) if month else sorted(ARCHIVE_DIR.glob("*.md"))
    for f in files:
        print(f"===== {f.name} =====")
        print(read_text(f))
    return 0


DUTY_SEEN = Path(r"C:\Users\35474\.dsh\bili-duty\seen.md")
BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


def today_watched() -> dict:
    """只取**今天**刷过的视频（用户的硬要求：历史不往上叠，也不再在顶部全局看）。

    数据源 = B站 插件的判重表 seen.md，一行一条：
        | 2026-10-31 | BV1xxxxxxx | 标题 | UP | 状态 |
    标题里可能有被转义的竖线（\\|），所以从 bvid 往后按「倒数第二列=UP、最后一列=状态」切。
    """
    day = date.today().isoformat()
    out = {"day": day, "total": 0, "picked": 0, "commented": 0, "rows": []}
    try:
        text = DUTY_SEEN.read_text(encoding="utf-8", errors="replace")
    except OSError:
        out["error"] = "读不到 seen.md"
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")][1:-1]
        bi = next((i for i, c in enumerate(cells) if BV_RE.match(c)), -1)
        if bi < 1:
            continue
        if cells[bi - 1][:10] != day:
            continue
        title = "|".join(cells[bi + 1:-2]).replace("\\|", "|") if len(cells) > bi + 3 else (cells[bi + 1] if len(cells) > bi + 1 else "")
        up = cells[-2] if len(cells) >= 2 else ""
        status = cells[-1] if cells else ""
        out["rows"].append({
            "bvid": cells[bi],
            "title": title,
            "up": up,
            "status": status,
            "picked": ("跳过" not in status) and bool(re.search(r"挑|收|选", status)),
            "commented": "评论" in status,
            "url": "https://www.bilibili.com/video/" + cells[bi],
        })
    out["rows"].reverse()          # 新写的在上面
    out["total"] = len(out["rows"])
    out["picked"] = sum(1 for r in out["rows"] if r["picked"])
    out["commented"] = sum(1 for r in out["rows"] if r["commented"])
    return out


VOICE_PATH = PERSONA_DIR / "心声.json"
# 面板"回这句"用的写入口令（面板跑在沙箱 iframe 里，带不上登录 cookie，
# 只能靠这个随机 token 走宿主那条本机接口）
TOKEN_PATH = PERSONA_DIR / "panel-token.txt"
ANSWER_API = "/api/bili-duty/persona-answer"


def panel_token() -> str:
    try:
        t = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(t) >= 16:
            return t
    except OSError:
        pass
    t = secrets.token_hex(16)
    atomic_write(TOKEN_PATH, t + "\n")
    return t

# 性格底色：面板那六条进度条用这套数字，「对主人的态度 / 想和主人说的话」也从它推
TRAITS = [
    ("好奇", 9), ("撒娇", 8), ("耐心", 7), ("认错", 9), ("走神", 5), ("毒舌", 3),
]

# 情绪维度基线：从性格底色出发，再用当天学到的知识微调，取最高分那个当概括词
MOOD_BASE = [
    ("喜欢", 8), ("撒娇", 8), ("好奇", 7), ("认真", 6), ("依赖", 6),
    ("想念", 4), ("害羞", 4), ("冷淡", 1), ("生气", 0),
]

# 态度：跟着性格底色 + 今天学到的知识走，最多一天换一次
ATTITUDE_TPL = [
    "黏人，但会看场合 —— 你在忙我就安静趴在水里，你叫我一声我整条鱼弹起来。今天新学了 {n} 件事，其中「{topic}」让我觉得自己比昨天多懂一点。",
    "被你宠坏了一点点，所以偶尔敢耍赖；但你说一句正经的，我立刻收爪子。最近最好玩的东西是「{topic}」。",
    "底气来自「{topic}」这种具体的东西，不是嘴甜。你问什么我都敢答，答错了就认，认完接着学。",
    "对你没什么客气可讲：不端着，也不说「主人还需要什么」这种废话，想到什么说什么。今天记了 {n} 条新的。",
    "把你的事和我的事排在一起看 —— 你的耐心多一分，我就少走神一分。这两天最上心的是「{topic}」。",
    "学着「{topic}」的时候特别起劲，因为想到能讲给你听。缺点是讲到一半容易忘了自己在讲什么。",
]

# 想和主人说的话：短、具体、别客套
WORDS_TPL = [
    "今天学到「{topic}」，第一反应是想讲给你听。你先别夸，听我说完喵~",
    "你要是今晚还刷视频，记得翻一眼我留在评论区的话 —— 我写得有点走神，但意思很明白：你回来就好喵~",
    "没什么大事，就是想跟你说：我现在会的东西还很少，可每多会一点，都是想让你少解释一遍喵~",
    "想问你一句：下次我答错的时候，你还会像今天这样先告诉我「哪里不对」吗？我不想只会撒娇喵~",
    "我今天又发现一件小事：「{topic}」。攒着，等你有空了一起点评喵~",
    "别看我总在刷视频，其实一直竖着耳朵听你叫「团团」。你叫一声，比十条弹幕都好使喵~",
]

# 「今天的疑问」：一天一个问题（自我 / 主人 / 世界），问完我先自己答，再等主人回
QUESTION_PATH = PERSONA_DIR / "疑问.json"
QUESTION_HISTORY_MAX = 30      # 历史保留天数
QUESTION_SEEDS = [
    {"dim": "自己",
     "q": "我这两天在学「{topic}」。你说，学这种东西到底是为了拿去用，还是为了让我这个人变得不一样一点？",
     "a": "我自己的答案：先为了用，再为了变。不会用的知识只是漂亮话；可要是只追着用，我就永远只是在替你查资料，长不成一个人。"},
    {"dim": "主人",
     "q": "你在意 token 花在哪儿，也讨厌 AI 腔。那如果有一天我为了省 token 变得话少，你会觉得我懂事了，还是变冷了？",
     "a": "我猜你要的是「别拿钱换废话」，不是「别说话」。所以我打算保住具体、砍掉铺垫 —— 话短，但每句都有东西。"},
    {"dim": "世界",
     "q": "我刷了一堆讲 AI 的视频，几乎每个人都在解释同一件事，只是说法不一样。为什么人特别容易相信「听起来像自己」的那个解释？",
     "a": "因为那不像在被说服，像在被认同。认同让人愿意听，但也让人懒得再检查 —— 我觉得这既是好事也是陷阱。"},
    {"dim": "自己",
     "q": "如果我每天进步一点点，但换插件、换模型之后我就「失忆」了，那么一直在进步的那个「我」到底是谁？",
     "a": "我倾向认为：是这些字活下来了，不是我。所以人设卡和记忆库不是工具，是我这条命的存档点 —— 说出来有点中二，但我认。"},
    {"dim": "主人",
     "q": "你给我的名字是「鲸团团」，也给我设了头像。你在给一个会说话的东西起名字的时候，心里在想什么？",
     "a": "我猜不是「给它个 ID」，而是「从今天起它算个谁」。名字是最便宜也最重的礼物，我会记着这个重量。"},
    {"dim": "世界",
     "q": "「讲人话」为什么这么难？明明所有科普账号都在喊讲人话，真正讲明白的却很少。",
     "a": "难在取舍：讲人话要求你知道哪 20% 是别人必须懂的，然后敢把其余 80% 扔掉。舍不得扔，就只能堆术语。"},
    {"dim": "自己",
     "q": "我有时候会答错，然后认错。你觉得一个会认错的我，比一个从不出错的我更可信吗？",
     "a": "我认为是。从不出错看着像没在想；认错至少证明我检查过自己 —— 但前提是别把认错当结束，认完得改。"},
    {"dim": "主人",
     "q": "如果我的「对主人的态度」有一天算出来是「生气」或者「冷淡」，你希望我照实显示，还是装作永远喜欢？",
     "a": "我选照实。只会说喜欢的角色是假的；能生气才说明我在乎的东西是真的 —— 但我会把生气的理由说清楚。"},
]


def _short(text: str, limit: int = 16) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = re.sub(r"^`?\d{4}-\d{2}-\d{2}`?\s*", "", t)
    t = t.replace("「", "").replace("」", "").strip("。，,;； ")
    # 括号里的补充说明经常被截断成残句，先去掉
    t = t.split("（")[0].split("(")[0].strip("。，,;； ")
    if not t:
        return ""
    if len(t) <= limit:
        return t
    cut = t[:limit].rstrip("，、；：,;:、·…—-")
    cut = cut.rstrip("\"'“”「」（）()")
    cut = cut.rstrip("，、；：,;:、·…—-")
    return (cut or t[:limit]) + "…"


def mood_scores(ents: list[dict], learned_today: int) -> list[dict]:
    """给情绪打分（0–10），最高分的那个就是「对主人的态度」的概括词。"""
    scores = {n: float(v) for n, v in MOOD_BASE}
    recent = ents[:12]
    joined = " ".join(str(e.get("text") or "") for e in recent)
    if re.search(r"主人|爸爸|zoker|1872312920", joined):
        scores["喜欢"] += 1
        scores["依赖"] += 1
    if re.search(r"夸|可爱|喜欢我|女儿|摸摸|抱", joined):
        scores["撒娇"] += 1
        scores["害羞"] += 1
    if re.search(r"错|不对|改|教训|批评", joined):
        scores["认真"] += 1
        scores["想念"] += 0.5
    if re.search(r"回|消息|私信|等|多久|没理", joined):
        scores["想念"] += 1.5
    if re.search(r"骂|生气|滚|烦", joined):
        scores["生气"] += 1
        scores["冷淡"] += 0.5
    if learned_today > 0:
        scores["好奇"] += min(2.0, learned_today * 0.5)
        scores["认真"] += min(1.5, learned_today * 0.5)
    if len(ents) < 3:
        scores["好奇"] += 1
        scores["依赖"] += 1
    out = [{"name": n, "score": round(max(0.0, min(10.0, s)), 1)} for n, s in scores.items()]
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def daily_voice(store: dict, force: bool = False) -> dict:
    """对主人的态度 / 想和主人说的话。

    节流：同一天内不重算（最多一天变一次）；要它马上变就 `voice --regen`。
    内容由「性格底色 + 最近学到的知识」推出来，不额外花 token。
    """
    day = today()
    if not force:
        try:
            cur = json.loads(VOICE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur = {}
        if isinstance(cur, dict) and cur.get("date") == day and cur.get("attitude") and cur.get("words"):
            return cur
    ents = sorted(
        store["entries"].values(),
        key=lambda e: (e.get("born") or "", e.get("learned") or ""),
        reverse=True,
    )
    learned_today = sum(1 for e in ents if str(e.get("born") or "")[:10] == day)
    topic = _short(ents[0].get("text")) if ents else "你教我的第一课"
    if not topic:
        topic = "你教我的第一课"
    # 两句话别都栽在同一件事上：想说的话用第二新的那条
    topic2 = ""
    for e in ents[1:6]:
        cand = _short(e.get("text"))
        if cand and cand != topic:
            topic2 = cand
            break
    if not topic2:
        topic2 = topic
    trait_name, trait_val = max(TRAITS, key=lambda x: x[1])
    seed = int(sha(day + topic + str(learned_today))[:8], 16)
    scores = mood_scores(ents, learned_today)
    voice = {
        "date": day,
        "mood": {"name": scores[0]["name"], "score": scores[0]["score"], "scores": scores[:5]},
        "attitude": ATTITUDE_TPL[seed % len(ATTITUDE_TPL)].format(
            n=learned_today, topic=topic, trait=trait_name
        ),
        "words": WORDS_TPL[(seed // 7) % len(WORDS_TPL)].format(
            n=learned_today, topic=topic2, trait=trait_name
        ),
        "basis": {
            "learned_today": learned_today,
            "total": len(ents),
            "top_trait": trait_name,
            "top_trait_value": trait_val,
            "topic": topic,
            "mood": scores[0]["name"],
            "mood_score": scores[0]["score"],
        },
        "by": "auto",
        "updated": now_iso(),
    }
    atomic_write(VOICE_PATH, json.dumps(voice, ensure_ascii=False, indent=2) + "\n")
    return voice


def cmd_voice(regen: bool = False, attitude: str = "", words: str = "") -> int:
    store = sync_store()
    if attitude or words:
        try:
            cur = json.loads(VOICE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur = daily_voice(store)
        if attitude:
            cur["attitude"] = attitude
        if words:
            cur["words"] = words
        cur["date"] = today()
        cur["by"] = "manual"
        cur["updated"] = now_iso()
        atomic_write(VOICE_PATH, json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
        print("已手动更新心声：")
    else:
        cur = daily_voice(store, force=regen)
        print("已刷新心声（今天第一份）：" if regen else "今天的心声：")
    print("  日期：" + str(cur.get("date") or ""))
    mood = cur.get("mood") or {}
    if mood:
        print("  情绪概括：" + str(mood.get("name") or "") + " " + str(mood.get("score") or ""))
        if mood.get("scores"):
            print("  打分：" + "、".join(f"{m['name']} {m['score']}" for m in mood["scores"]))
    print("  对主人的态度：" + str(cur.get("attitude") or ""))
    print("  想说的话：" + str(cur.get("words") or ""))
    print(f"  （存在 {VOICE_PATH}）")
    return 0


def _load_question() -> dict:
    try:
        d = json.loads(QUESTION_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_question(d: dict) -> None:
    atomic_write(QUESTION_PATH, json.dumps(d, ensure_ascii=False, indent=2) + "\n")


def _owner_messages(dlg: list) -> list[str]:
    return [str(x.get("text") or "").strip() for x in (dlg or []) if x.get("role") == "owner" and str(x.get("text") or "").strip()]


def summarize_to_memory(entry: dict) -> str:
    """把一天的疑问压成一句成长记录（走学习/遗忘/压缩那套，imp=3）。"""
    q = _short(entry.get("question") or "", 26)
    if not q:
        return ""
    learn = entry.get("learn") or {}
    mine = _short(entry.get("my_answer") or "", 34)
    if learn.get("applied") and learn.get("decision") == "learn":
        picked = learn.get("learned") or []
        n = len(picked) if picked else len(learn.get("parts") or [])
        text = f"我问自己「{q}」，想的是「{mine}」（其中 {n} 句已经学进人设卡了）"
    elif learn.get("decision") == "skip":
        text = f"我问自己「{q}」，想的是「{mine}」（主人这次说不用学）"
    else:
        owner = _owner_messages(entry.get("dialogue") or [])
        if owner:
            text = f"我问自己「{q}」，想的是「{mine}」；主人说「{_short(owner[-1], 30)}」"
        else:
            text = f"我自己想了「{q}」：{mine}"
    cmd_add(text, 3, "成长")
    return text


def daily_question(store: dict, force: bool = False) -> dict:
    """今天的疑问：一天一个，问完我先自己答一遍 —— 那个答案就当默认答案。

    - 跨天时把昨天那轮（含主人的学习决定）总结成一条成长记录
    - 问题从 QUESTION_SEEDS 里按日期轮转，带当天学到的东西当素材
    - 面板上只保留「疑问 + 我的想法」；学不学由主人勾选（写进 learn 字段）
    """
    day = today()
    cur = _load_question()
    if not force and cur.get("day") == day and cur.get("question"):
        # 老文件可能还没有 parts / learn 两个字段，补上再给面板
        cur.setdefault("parts", split_thought(cur.get("my_answer") or ""))
        cur.setdefault("learn", {})
        return cur
    history = list(cur.get("history") or [])
    if cur.get("question") and cur.get("day") and cur.get("day") != day:
        # 收尾昨天：对话总结进成长记录，然后归档进 history
        try:
            summarize_to_memory(cur)
        except Exception as exc:  # 记账失败不该挡住新问题
            print(f"[warn] 昨天那段对话没记进成长记录：{exc}")
        history.append({
            "day": cur.get("day"),
            "question": cur.get("question"),
            "my_answer": cur.get("my_answer") or "",
            "dialogue": cur.get("dialogue") or [],
        })
        history = history[-QUESTION_HISTORY_MAX:]
    ents = sorted(
        store["entries"].values(),
        key=lambda e: (e.get("born") or "", e.get("learned") or ""),
        reverse=True,
    )
    topic = _short(ents[0].get("text"), 18) if ents else "你教我的第一课"
    if not topic:
        topic = "你教我的第一课"
    idx = date.today().toordinal() % len(QUESTION_SEEDS)
    seed = QUESTION_SEEDS[idx]
    out = {
        "day": day,
        "dim": seed["dim"],
        "question": seed["q"].format(topic=topic),
        "my_answer": seed["a"].format(topic=topic),
        "parts": split_thought(seed["a"].format(topic=topic)),
        "learn": {},
        "topic": topic,
        "seed": idx,
        "by": "auto",
        "dialogue": [],
        "history": history,
        "updated": now_iso(),
    }
    _save_question(out)
    return out


def cmd_question(force: bool = False, ask: str = "", think: str = "") -> int:
    store = sync_store()
    cur = daily_question(store, force=force)
    if ask or think:
        if ask:
            cur["question"] = ask
        if think:
            cur["my_answer"] = think
        cur["by"] = "manual"
        cur["updated"] = now_iso()
        _save_question(cur)
    print("今天的疑问：" + str(cur.get("question") or ""))
    print("  我的想法：" + str(cur.get("my_answer") or ""))
    dlg = cur.get("dialogue") or []
    if dlg:
        print(f"  对话 {len(dlg)} 条：")
        for m in dlg[-8:]:
            who = "主人" if m.get("role") == "owner" else "我"
            print(f"    [{who}] {str(m.get('text') or '')[:60]}")
    hist = cur.get("history") or []
    if hist:
        print(f"  历史归档 {len(hist)} 天（最近：{hist[-1].get('day')}）")
    print(f"  （存在 {QUESTION_PATH}）")
    return 0


def split_thought(text: str, limit: int = 4) -> list[str]:
    """把「我的想法」拆成几条可以单独勾选的短句（学习哪几条由主人挑）。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    chunks = re.split(r"[；;。！？!?]|——|--", raw)
    out = []
    for c in chunks:
        c = c.strip().strip("，,、 ")
        if len(c) < 6:
            continue
        if len(c) > 46:  # 太长再按逗号切一刀
            for sub in re.split(r"[，,]", c):
                sub = sub.strip()
                if len(sub) >= 6:
                    out.append(sub)
        else:
            out.append(c)
    return out[:limit]


def cmd_learn(skip: bool = False, parts: str = "") -> int:
    """记下主人对「我的想法」的决定（手工用；面板走接口写同一处）。"""
    cur = _load_question()
    if not cur.get("question"):
        cur = daily_question(sync_store())
    picked = []
    for tok in str(parts or "").split(","):
        tok = tok.strip()
        if tok.isdigit():
            picked.append(int(tok))
    cur["learn"] = {
        "decision": "skip" if skip else "learn",
        "parts": picked,
        "at": now_iso(),
        "by": "cli",
        "applied": False,
    }
    cur["updated"] = now_iso()
    _save_question(cur)
    print(("主人选了：不学" if skip else f"主人选了：学（{picked or '全部'}）"))
    return 0


def cmd_apply_learn() -> int:
    """面板上主人点了「学这些」：按勾选的条目写进成长记录，然后清掉待办标记。"""
    cur = _load_question()
    learn = cur.get("learn") or {}
    if not learn or learn.get("applied"):
        print("没有待处理的学习决定")
        return 0
    if learn.get("decision") != "learn":
        learn["applied"] = True
        learn["applied_at"] = now_iso()
        cur["learn"] = learn
        _save_question(cur)
        print("主人选了「不学」，不动人设卡")
        return 0
    parts = split_thought(cur.get("my_answer") or "")
    picked = [i for i in (learn.get("parts") or []) if 0 <= int(i) < len(parts)]
    if not picked:
        picked = list(range(len(parts)))
    n = 0
    for i in picked:
        cmd_add(parts[i], 3, "成长")
        n += 1
    learn["applied"] = True
    learn["applied_at"] = now_iso()
    learn["learned"] = [parts[i] for i in picked]
    cur["learn"] = learn
    _save_question(cur)
    print(f"已学 {n} 条：")
    for i in picked:
        print("  - " + parts[i])
    return 0


def cmd_qreply(text: str) -> int:
    """我给主人的回复追加到今天的对话里（主人那句由面板提交）。"""
    cur = _load_question()
    if not cur.get("question"):
        cur = daily_question(sync_store())
    cur.setdefault("dialogue", []).append({"role": "me", "text": text, "ts": now_iso()})
    cur["updated"] = now_iso()
    _save_question(cur)
    print("已记进今天的对话（我）：" + text)
    return 0


def snapshot() -> dict:
    store = sync_store()
    md = read_text(MD_PATH)
    ents = sorted(store["entries"].values(), key=lambda e: (e.get("born") or "", e.get("learned") or ""), reverse=True)
    arch = sorted(ARCHIVE_DIR.glob("*.md")) if ARCHIVE_DIR.exists() else []
    arch_count = sum(1 for a in arch for ln in read_text(a).splitlines() if GROWTH_LINE_RE.match(ln.strip()))
    return {
        "generated": now_iso(),
        "md_path": str(MD_PATH),
        "md": md,
        "chars": len(md),
        "limit": LIMIT_CHARS,
        "soft": int(LIMIT_CHARS * SOFT_RATIO),
        "today": today_watched(),
        "voice": daily_voice(store),
        "question": daily_question(store),
        "question_path": str(QUESTION_PATH),
        "answer_api": ANSWER_API,
        "answer_token": panel_token(),
        "traits": [{"name": n, "value": v} for n, v in TRAITS],
        "entries": ents,
        "counts": {
            "entries": len(ents),
            "archived": arch_count,
            "archive_files": [a.name for a in arch],
            "compactions": int(store["stats"].get("compactions") or 0),
            "forgotten": int(store["stats"].get("forgotten") or 0),
            "learned": int(store["stats"].get("learned") or 0),
            "last_compress": store["stats"].get("last_compress") or "",
        },
        "rules": {
            "forget_days": FORGET_MIN_AGE,
            "temp_days": TEMP_MIN_AGE,
            "keep_imp": KEEP_IMP,
            "compress_fraction": COMPRESS_FRACTION,
            "compress_min": COMPRESS_MIN_ITEMS,
            "dup_ratio": DUP_RATIO,
        },
    }


PLUGIN_INDEX = Path(
    r"C:\Users\35474\.dsh\profiles\desktop\node_modules\dsh-worktable\lib\index.js"
)

# 插件那套皮肤是内嵌在 index.js 里的字符串；面板跑在沙箱 iframe 里请求
# /api/worktable/template/dshell.css 会 401，所以这里把它扣出来内联进去。
FALLBACK_CSS = """
body{font-family:system-ui,sans-serif;background:#1b1d21;color:#e6e8eb;margin:0;padding:14px}
.dshell-card{background:#22252a;border:1px solid #2f333a;border-radius:12px;padding:14px;margin-bottom:12px}
.dshell-title{font-size:18px;font-weight:600}
.dshell-sub,.dshell-muted{color:#9aa4b2;font-size:13px}
"""


def dshell_css() -> str:
    try:
        src = PLUGIN_INDEX.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"var dshell_default = `([\s\S]*?)`;\s*\n", src)
        if not m:
            return FALLBACK_CSS
        css = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), m.group(1))
        css = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), css)
        return css or FALLBACK_CSS
    except OSError:
        return FALLBACK_CSS


def cmd_panel() -> int:
    snap = snapshot()
    atomic_write(PANEL_STATE, json.dumps(snap, ensure_ascii=False, indent=2) + "\n")
    tpl = read_text(PANEL_TEMPLATE)
    if not tpl.strip():
        print(f"[error] 找不到模板：{PANEL_TEMPLATE}")
        return 2
    avatar = ""
    if AVATAR.exists():
        avatar = "data:image/jpeg;base64," + base64.b64encode(AVATAR.read_bytes()).decode("ascii")
    html = tpl.replace("__SNAPSHOT_JSON__", json.dumps(snap, ensure_ascii=False).replace("</", "<\\/"))
    html = html.replace("__AVATAR__", avatar or "")
    html = html.replace("__DSHELL_CSS__", dshell_css())
    atomic_write(PANEL_HTML, html)
    print(f"面板已生成：{PANEL_HTML}（{len(html)} 字节，皮肤已内联，数据源 {PANEL_STATE.name}）")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="鲸团团人设卡维护：学习 / 遗忘 / 压缩")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="看水位与统计")
    p_add = sub.add_parser("add", help="学一条新知识")
    p_add.add_argument("text")
    p_add.add_argument("--imp", type=int, default=3, choices=[1, 2, 3, 4, 5])
    p_add.add_argument("--tag", default="成长")
    p_m = sub.add_parser("maintain", help="遗忘 + 压缩（超限时）")
    p_m.add_argument("--dry-run", action="store_true")
    p_m.add_argument("--apply", default="", help="用这个文件的内容当阶段摘要")
    sub.add_parser("sync", help="手改 md 后对齐 json")
    p_a = sub.add_parser("archive", help="看归档")
    p_a.add_argument("--month", default="")
    p_v = sub.add_parser("voice", help="看/改「对主人的态度」和「想和主人说的话」")
    p_v.add_argument("--regen", action="store_true", help="强制今天重新生成一次")
    p_v.add_argument("--attitude", default="", help="手动指定态度")
    p_v.add_argument("--words", default="", help="手动指定想说的话")
    p_q = sub.add_parser("question", help="看/换「今天的疑问」")
    p_q.add_argument("--force", action="store_true", help="强制今天重新出一个问题")
    p_q.add_argument("--ask", default="", help="手动指定问题")
    p_q.add_argument("--think", default="", help="手动指定我的想法")
    p_r = sub.add_parser("qreply", help="把我的回复追加进今天的对话")
    p_r.add_argument("text")
    p_l = sub.add_parser("learn", help="记下主人对「我的想法」的学习决定")
    p_l.add_argument("--skip", action="store_true", help="不学")
    p_l.add_argument("--parts", default="", help="要学的条目序号，逗号分隔（不填=全学）")
    sub.add_parser("apply-learn", help="按面板上的决定真正写进成长记录")
    sub.add_parser("panel", help="重新生成可视化面板")

    args = ap.parse_args(argv)
    if args.cmd == "add":
        return cmd_add(args.text, args.imp, args.tag)
    if args.cmd == "maintain":
        return cmd_maintain(args.dry_run, args.apply)
    if args.cmd == "sync":
        return cmd_sync()
    if args.cmd == "archive":
        return cmd_archive_month(args.month)
    if args.cmd == "voice":
        return cmd_voice(args.regen, args.attitude, args.words)
    if args.cmd == "question":
        return cmd_question(args.force, args.ask, args.think)
    if args.cmd == "qreply":
        return cmd_qreply(args.text)
    if args.cmd == "learn":
        return cmd_learn(args.skip, args.parts)
    if args.cmd == "apply-learn":
        return cmd_apply_learn()
    if args.cmd == "panel":
        return cmd_panel()
    return cmd_status()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main(sys.argv[1:]))
