#!/usr/bin/env python3
"""jev.py - 在命令行向 TypeSafe Jev（System One 模型）提类型化问题。

Jev 输入一段 state（纯文本，或 JSON 对象/数组），输出预先定义好的类型化概率，
不生成任何文字。端点：POST https://openrouter.ai/api/alpha/decisions
（模型 ~typesafe/jev-latest）。

三种题型
  noul    -> {"noul": 0.93}                      是/否的概率，0..1
  choice  -> {"choice": "物流仓储", "probabilities": {...}, "confidence": 0.98}
  score   -> {"score": 1.99, "legend": {...}, "probabilities": {...}, "confidence"}

用法
  # 内联出题
  jev.py -s "钱都扣了三天了，单号 88231 还是待发货，再不解决我就投诉！" \
         --noul is_urgent "这条消息是否表达了紧迫性？" \
         --choice department "应该由哪个团队处理？" \
                  账单退款="付款、发票、退款" 物流仓储="发货、快递、库存" \
         --score anger "客户的愤怒程度？" 平静 不满 愤怒 极度愤怒

  # 题集文件（可把 state 一起写在文件里，见 examples/*.json）
  jev.py -q examples/01-ecommerce.json --table
  cat ticket.txt | jev.py -q q_ticket.json --json     # state 从 stdin 读

选项
  -s TEXT|@FILE|-   state；重复使用 key=value 可拼成 JSON 对象
  -q FILE|JSON      完整 questions 对象，或含 state+questions 的自包含请求体
  --table           输出 Markdown 表格（中文列名），默认单行对齐
  --json            输出原始 API 响应，便于接 jq 或入库
  --model M         默认 ~typesafe/jev-latest
  --selftest        离线自检，不消耗额度

密钥：环境变量 OPENROUTER_API_KEY（或 --api-key）。

仓库：https://github.com/burgerwdev/what-is-jev（含 14 个可运行的例子和用法笔记）。
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"
RETRY_ON = {429, 500, 502, 503, 524, 529}


def slug(s):
    """ASCII 名字转小写下划线；中文和全大写缩写原样保留（EMI、R31 更好读）。"""
    s = s.strip()
    if s.isascii() and not (s.upper() == s and any(c.isalpha() for c in s)):
        s = s.lower()
    return re.sub(r"\W+", "_", s).strip("_") or "option"


def maybe_json(s):
    """Structured state stays structured; anything else is plain text."""
    s = s.strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return s


def load_state(vals, preset=None):
    if not vals:
        if preset is not None:  # 题集文件里已经带了 state
            return preset
        if sys.stdin.isatty():
            fail("no state given: use -s TEXT|@FILE|- (or pipe stdin)")
        return maybe_json(sys.stdin.read())
    if len(vals) == 1:
        v = vals[0]
        if v == "-":
            return maybe_json(sys.stdin.read())
        if v.startswith("@"):
            with open(v[1:]) as f:
                return maybe_json(f.read())
        return maybe_json(v)
    def scalar(v):  # key=value pairs may carry JSON scalars: n=3, flag=true
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v

    return {k: scalar(v) for k, _, v in (p.partition("=") for p in vals)}


def build_questions(args, argv=()):
    """返回 (questions, 题集文件里自带的 state, 场景说明)；命令行开关覆盖文件内容。"""
    questions, preset, note = {}, None, None
    if args.questions:
        raw = args.questions
        if not raw.lstrip().startswith("{"):
            with open(raw.lstrip("@")) as f:
                raw = f.read()
        body = json.loads(raw)
        if isinstance(body, dict) and "questions" in body:  # 自包含的请求体
            questions.update(body["questions"])
            preset = body.get("state")
            note = body.get("场景") or body.get("说明") or body.get("note")
        else:
            questions.update(body)
    for key, instructions in args.noul or []:
        questions[key] = {"type": "noul", "instructions": instructions}
    for kind, groups in (("choice", args.choice), ("score", args.score)):
        for g in groups or []:
            key, instructions, opts = g[0], g[1], g[2:]
            if len(opts) < 2:
                fail(f"--{kind} {key}: need at least 2 criteria")
            if kind == "score":
                criteria = list(opts)
            else:
                criteria = {}
                for o in opts:
                    name, sep, desc = o.partition("=")
                    criteria[slug(name)] = desc if sep else name
            questions[key] = {"type": kind, "instructions": instructions, "criteria": criteria}
    if not questions:
        fail("no questions given: use --noul/--choice/--score or -q FILE")
    written = [argv[i + 1] for i, a in enumerate(argv)  # 让输出顺序跟书写顺序一致
               if a in ("--noul", "--choice", "--score") and i + 1 < len(argv)]
    questions = {**{k: questions[k] for k in written if k in questions},
                 **{k: v for k, v in questions.items() if k not in written}}
    return questions, preset, note


def call(payload, api_key, site, title, timeout, retries=2):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if site:
        headers["HTTP-Referer"] = site
    if title:
        headers["X-OpenRouter-Title"] = title
    body = json.dumps(payload).encode()
    err = (0, "")
    for attempt in range(retries + 1):
        req = urllib.request.Request(ENDPOINT, data=body, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), (time.perf_counter() - t0) * 1000
        except urllib.error.HTTPError as e:
            err = (e.code, e.read().decode(errors="replace"))
            if e.code not in RETRY_ON:
                break
        except urllib.error.URLError as e:
            err = (0, str(e))
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    fail(f"request failed (HTTP {err[0]}): {err[1][:800]}")


LANG = {
    "zh": {"type": {"noul": "是否题", "choice": "单选", "score": "评分"}, "yes": "是", "no": "否",
           "head": ["问题", "类型", "结论", "概率分布", "置信度"],
           "model": "模型", "input": "输入", "cost": "成本", "scene": "场景"},
    "en": {"type": {"noul": "yes/no", "choice": "choice", "score": "score"}, "yes": "yes", "no": "no",
           "head": ["Question", "Type", "Answer", "Distribution", "Confidence"],
           "model": "model", "input": "input", "cost": "cost", "scene": "Scenario"},
}


def _w(s):
    """显示宽度：中日韩全角字符占 2 列，否则表格会错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, w):
    return s + " " * max(0, w - _w(s))


def _conf(a):
    c = a.get("confidence")
    return "—" if c is None else f"{c:.2f}"


def rows_of(answers, lang="zh"):
    """每题一行：[问题, 类型, 结论, 概率分布, 置信度]。"""
    L = LANG[lang]
    rows = []
    for key, a in answers.items():
        t = a.get("type")
        if t == "noul":
            p = a["noul"]
            verdict = f"{L['yes']} {p:.0%}" if p >= 0.5 else f"{L['no']} {1 - p:.0%}"
            rows.append([key, L["type"]["noul"], verdict, f"{L['yes']} {p:.2f} / {L['no']} {1 - p:.2f}", "—"])
        elif t == "choice":
            probs = a.get("probabilities") or {}
            dist = " / ".join(f"{k} {v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1]))
            rows.append([key, L["type"]["choice"], str(a.get("choice")), dist, _conf(a)])
        elif t == "score":
            legend = {int(k): v for k, v in (a.get("legend") or {}).items()}
            s = a["score"]
            near = legend.get(min(legend, key=lambda i: abs(i - s)), "?") if legend else "?"
            dist = " / ".join(f"{legend.get(int(k), k)} {v:.2f}" for k, v in (a.get("probabilities") or {}).items())
            scale = f"{s:.2f}/{max(legend)}" if legend else f"{s:.2f}"
            rows.append([key, L["type"]["score"], f"{scale} ≈ {near}", dist, _conf(a)])
        else:
            rows.append([key, str(t), json.dumps(a, ensure_ascii=False), "—", "—"])
    return rows


def render_line(rows):
    widths = [max(_w(r[i]) for r in rows) for i in range(5)]
    return "\n".join("  ".join(_pad(r[i], widths[i]) for i in range(5)).rstrip() for r in rows)


def render_table(rows, lang="zh"):
    head = LANG[lang]["head"]
    widths = [max(_w(r[i]) for r in [head] + rows) for i in range(5)]
    cells = lambda r: "| " + " | ".join(_pad(r[i], widths[i]) for i in range(5)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([cells(head), sep] + [cells(r) for r in rows])


def fail(msg):
    print(f"jev: {msg}", file=sys.stderr)
    raise SystemExit(2)


def selftest():
    assert slug("Very angry") == "very_angry" and slug("!!") == "option"
    assert slug("物流仓储") == "物流仓储"  # 中文 key 原样保留
    assert slug("重测EMC") == "重测EMC" and slug("EMC") == "EMC" and slug("emi") == "emi"
    assert maybe_json(' {"a": 1} ') == {"a": 1} and maybe_json("plain text") == "plain text"
    ns = argparse.Namespace(
        questions=None,
        noul=[["is_bug", "Is this a defect?"]],
        choice=[["team", "Who owns it?", "payments=Checkout", "account"]],
        score=[["urgency", "How urgent?", "Low", "High"]],
    )
    q, preset, note = build_questions(ns)
    assert preset is None and note is None
    assert q["is_bug"] == {"type": "noul", "instructions": "Is this a defect?"}
    assert q["team"]["criteria"] == {"payments": "Checkout", "account": "account"}
    assert q["urgency"]["criteria"] == ["Low", "High"]
    body = json.dumps({"场景": "测试", "state": {"ticket": "x"},
                       "questions": {"a": {"type": "noul", "instructions": "?"}}})
    q2, preset2, note2 = build_questions(argparse.Namespace(questions=body, noul=None, choice=None, score=None))
    assert preset2 == {"ticket": "x"} and note2 == "测试" and q2["a"]["type"] == "noul"
    assert load_state([], preset2) == {"ticket": "x"}
    # 输出顺序跟命令行书写顺序一致
    q3, _, _ = build_questions(ns, ["--choice", "urgency", "x", "a", "b", "--noul", "is_bug", "y"])
    assert list(q3) == ["urgency", "is_bug", "team"]
    rows = rows_of({
        "is_bug": {"type": "noul", "noul": 0.93},
        "team": {"type": "choice", "choice": "payments", "confidence": 0.68,
                 "probabilities": {"payments": 0.79, "account": 0.21}},
        "urgency": {"type": "score", "score": 1.99, "confidence": 0.99,
                    "legend": {"0": "Low", "1": "High"}, "probabilities": {"0": 0.01, "1": 0.99}},
    })
    line, table = render_line(rows), render_table(rows)
    assert "是 93%" in line and "payments" in line and "1.99/1 ≈ High" in line
    assert "—" in line  # noul 没有 confidence
    assert table.startswith("| 问题")
    assert len(table.splitlines()) == len(rows) + 2
    assert _w("中文") == 4 and _pad("中文", 6) == "中文  "
    assert load_state(["tier=enterprise", "n=3"]) == {"tier": "enterprise", "n": 3}
    print("selftest ok")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-s", "--state", action="append", metavar="TEXT|@FILE|-",
                   help="state; repeat as key=value for a JSON object")
    p.add_argument("-q", "--questions", metavar="FILE|JSON", help="full questions object")
    p.add_argument("--noul", nargs=2, action="append", metavar=("KEY", "INSTRUCTIONS"))
    p.add_argument("--choice", nargs="+", action="append", metavar="KEY INSTRUCTIONS KEY=BESCRIPTION...")
    p.add_argument("--score", nargs="+", action="append", metavar="KEY INSTRUCTIONS LEVEL...")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY"))
    p.add_argument("--site-url", help="HTTP-Referer header")
    p.add_argument("--title", help="X-OpenRouter-Title header")
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--table", action="store_true", help="输出 Markdown 表格")
    p.add_argument("--lang", choices=("zh", "en"), default="zh", help="输出语言，默认 zh")
    p.add_argument("--json", action="store_true", help="print raw API response")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        return selftest()
    if not args.api_key:
        fail("set OPENROUTER_API_KEY (or pass --api-key)")

    questions, preset, note = build_questions(args, sys.argv[1:])
    payload = {"model": args.model, "state": load_state(args.state, preset), "questions": questions}
    res, ms = call(payload, args.api_key, args.site_url, args.title, args.timeout)
    if args.json:
        print(json.dumps(res, indent=2))
        return

    answers = res.get("answers") or {}
    if not answers:
        fail(f"no answers in response: {json.dumps(res)[:400]}")
    usage = res.get("usage") or {}
    L = LANG[args.lang]
    if note:
        print(f"{L['scene']}: {note}")
    print(f"{L['model']} {res.get('model', args.model)} ({res.get('provider', '?')})"
          f"  {ms:.0f} ms  {L['input']} {usage.get('input_tokens', '?')} tokens"
          f"  {L['cost']} ${usage.get('cost', 0):.7f}")
    rows = rows_of(answers, args.lang)
    print(render_table(rows, args.lang) if args.table else render_line(rows))


if __name__ == "__main__":
    main()
