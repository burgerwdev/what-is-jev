#!/usr/bin/env python3
"""jev.py - ask TypeSafe Jev (System One models) typed questions from the shell.

Jev takes a `state` (plain text, or a JSON object/array) and returns pre-defined
typed probabilities. It generates no text.
Endpoint: POST https://openrouter.ai/api/alpha/decisions (model ~typesafe/jev-latest).

Three question types
  noul    -> {"noul": 0.93}                        probability of yes, 0..1
  choice  -> {"choice": "order_logistics", "probabilities": {...}, "confidence": 0.99}
  score   -> {"score": 2.79, "legend": {...}, "probabilities": {...}, "confidence": 0.79}

Usage
  # questions written inline
  jev.py -s "You took the money three days ago and order 88231 is still not shipped" \\
         --noul   is_urgent  "Does this message express time pressure?" \\
         --choice department "Which team should handle this ticket?" \\
                  order_logistics="Shipping, courier, warehouse stock" \\
                  billing_refunds="Payments, invoices, refunds" \\
         --score  anger      "How angry is the customer?" Calm Annoyed Angry Furious

  # questions from a file (state can live in the same file, see examples/)
  jev.py -q examples/en/01-ecommerce.json --table --lang en
  cat state.txt | jev.py --noul is_negative "Is this a negative review?" --json

Options
  -s TEXT|@FILE|-   state; repeat KEY=VALUE to build a JSON object
  -q FILE|JSON      questions object, or a self-contained body with state + questions
  --table           Markdown table output, default is aligned lines
  --lang {zh,en}    output language, default zh
  --json            raw API response, for jq and storage
  --model M         default ~typesafe/jev-latest
  --selftest        offline self-check, no API calls

Key: OPENROUTER_API_KEY in the environment, or --api-key.

Repo: https://github.com/burgerwdev/what-is-jev
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
                   help="state; repeat KEY=VALUE to build a JSON object")
    p.add_argument("-q", "--questions", metavar="FILE|JSON",
                   help="questions object, or a body containing state + questions")
    p.add_argument("--noul", nargs=2, action="append", metavar=("KEY", "INSTRUCTION"),
                   help='yes/no question: KEY "INSTRUCTION"')
    p.add_argument("--choice", nargs="+", action="append", metavar="ARGS",
                   help='choice question: KEY "INSTRUCTION" OPT=DESCRIPTION ...')
    p.add_argument("--score", nargs="+", action="append", metavar="ARGS",
                   help='scored question: KEY "INSTRUCTION" LEVEL ...')
    p.add_argument("--model", default=DEFAULT_MODEL, help="model id, default " + DEFAULT_MODEL)
    p.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY"),
                   help="defaults to $OPENROUTER_API_KEY")
    p.add_argument("--site-url", help="HTTP-Referer header")
    p.add_argument("--title", help="X-OpenRouter-Title header")
    p.add_argument("--timeout", type=float, default=90, help="request timeout in seconds")
    p.add_argument("--table", action="store_true", help="print a Markdown table")
    p.add_argument("--lang", choices=("zh", "en"), default="zh", help="output language")
    p.add_argument("--json", action="store_true", help="print the raw API response")
    p.add_argument("--selftest", action="store_true", help="run the offline self-check and exit")
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
