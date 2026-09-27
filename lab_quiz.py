#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
实验预习答题系统 自动答题工具（通用版，支持任意账户）

站点: http://172.25.75.220  （深大内网，Django + Vue3 SPA）

核心机制（务必理解，否则会白交卷）:
  选项每次取卷都会重新随机排布，只有「选项文本」是固定的。
  因此「算答案用的那份取卷结果」和「交卷时回传的 option_orders」必须是同一份。
  满分路径 = 先交一次拿阅卷 PDF → 读出「标准答案处的选项文本」
           → 重新取卷按文本反查字母 → 用同一份取卷结果再交一次。

用法:
  # 账号（三选一）: 命令行 / 环境变量 / 交互输入
  python lab_quiz.py -u 学号 -p 密码 summary
  set LABQUIZ_USER=xxx & set LABQUIZ_PASSWORD=yyy & python lab_quiz.py summary
  python lab_quiz.py -u xxx summary          # 会提示输入密码

  # 命令
  python lab_quiz.py -u U summary            列出全部预习任务
  python lab_quiz.py -u U solve              处理所有还没满分的卷子（可反复跑，幂等）
  python lab_quiz.py -u U solve --id 3,5,9   只处理指定卷
  python lab_quiz.py -u U solve --dry-run    只算答案不交卷
  python lab_quiz.py -u U finish --id 3 --attempt 279   用已有 PDF 直接交卷（省一次探测）
  python lab_quiz.py -u U parse <pdf>        打印 PDF 解析出的题目与标准答案
  python lab_quiz.py -u U check --id 3 --pdf x.pdf      干跑校验某份卷

  # 多账户批量
  python lab_quiz.py --account a:passA --account b:passB solve
  python lab_quiz.py --accounts-file accounts.json solve   # {"a":"passA","b":"passB"}

依赖: pdfplumber（Pillow 会作为它的依赖一起装上）。缺了脚本会**自动 pip 安装**：
      先试默认源，失败再试清华源，权限不足自动加 --user。
      关掉自动安装: 设环境变量 LABQUIZ_NO_INSTALL=1
注意: 访问内网要清掉代理环境变量, 脚本内部已自动处理。
"""
from __future__ import annotations

import argparse
import difflib
import getpass
import importlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://172.25.75.220/api"
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：代码被解压到临时目录，但题库/截图必须落在 exe 旁边才能持久化
    OUT = os.path.dirname(os.path.abspath(sys.executable))
else:
    OUT = os.path.dirname(os.path.abspath(__file__))


# ============================ 工具函数 ============================
TRANS = str.maketrans({
    "（": "(", "）": ")", "，": ",", "。": ".", "、": ",",
    "：": ":", "；": ";", "？": "?", "！": "!", "　": "",
    "＝": "=", "－": "-", "—": "-", "～": "~",
})


def norm(s):
    if not s:
        return ""
    return re.sub(r"\s+", "", s.translate(TRANS))


def norm2(s):
    """再抹掉 LaTeX 花括号，用于公式选项比对（I_{S} 与 I_S 视为相同）"""
    return norm(s).replace("{", "").replace("}", "")


def is_subseq(short, long):
    """short 是否为 long 的子序列（PDF 会丢掉题干里的公式图片）"""
    it = iter(long)
    return all(ch in it for ch in short)


# 选项是公式图片、PDF 提不出文本的题：人工核对阅卷图后写下的「正确选项文本子串」
# 格式: (题干关键词, 正确选项文本(经 norm2 后)里必须出现的子串)
# 关键词越独特越要放前面。BANK 是全账户共用的（同一份试卷题目相同）。
FORMULA_OVERRIDES = [
    ("之间的关系为", "IBd"),                    # 霍尔: 导体板 U = R_H·I·B/d
    ("静电力的大小", "eUh"),                    # 霍尔: F = eU/h
    ("测量霍尔电压的原理公式", "I_SBd"),         # 霍尔: V_H = R_H·I_S·B/d
    ("载流子浓度", "1R_He"),                    # 霍尔: n = 1/(R_H·e)
    ("位相差", "k\\cdot\\Deltas"),              # 光拍法: Δφ = k·Δs
    ("光强正比于", "E_1+E_2\\right)^2"),        # 双光栅: I ∝ (E1+E2)^2
]

QPAT = re.compile(
    r"(?m)^\s*(\d+)\s*[.、]\s*(.*?)你的答案\s*[:：]\s*(.*?)\s*标准答案\s*[:：]\s*(.*?)"
    r"\s*评阅\s*[:：]\s*(.)\s*(正确|错误)\s*得分\s*[:：]\s*(\d+)\s*分",
    re.S,
)
MARKER_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z])\s*[.、]")


def all_overrides():
    """内置规则 + 同目录外置的 overrides.json（格式: [["题干关键词","正确选项文本子串"], ...]）"""
    extra = []
    p = os.path.join(OUT, "overrides.json")
    if os.path.exists(p):
        try:
            data = json.load(open(p, encoding="utf-8"))
            if isinstance(data, dict):
                data = list(data.items())
            extra = [(str(k), str(v)) for k, v in data]
        except Exception as e:
            print("警告: overrides.json 解析失败，已忽略（%s）" % e)
    return extra + FORMULA_OVERRIDES   # 外置规则优先


# ---------------------- 共享题库（跨账户复用） ----------------------
# 题目内容全账户一致，只有题序/选项序不同。所以「题干 -> 正确选项文本」这张表
# 对所有账户通用：别人不必再花一次机会交卷探答案，直接一次交到满分。
BANK_PATH = os.path.join(OUT, "bank.json")


def load_bank():
    if not os.path.exists(BANK_PATH):
        return {}
    try:
        return json.load(open(BANK_PATH, encoding="utf-8")).get("questions", {})
    except Exception as e:
        print("警告: bank.json 解析失败，已忽略（%s）" % e)
        return {}


def save_bank(qmap):
    json.dump({"version": 1, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"), "questions": qmap},
              open(BANK_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def selectors_from_row(row, overrides, q=None):
    """把阅卷 PDF 某题的正确答案，转成可跨账户复用的「选项选择器」
    exact=True : 选择器是完整选项文本(norm2后) → 新取卷里做相等匹配
    exact=False: 选择器是子串（公式图片题，靠覆盖规则） → 做包含匹配
    q 可为 None（接口题干拿不到时，直接按 PDF 题干处理）"""
    is_judge = row["is_judge"] if q is None else (q["type"] == "judge")
    if is_judge:
        tf = re.findall(r"[TF]", row["raw_std"].upper())
        return {"type": "judge", "exact": True, "selectors": [tf[0]]} if tf else None
    texts = row["correct_texts"]
    if texts and all(t for t in texts):
        return {"type": "choice", "exact": True, "selectors": [norm2(t) for t in texts]}
    for key, sub in overrides:
        if key in row["qtext"]:
            return {"type": "choice", "exact": False, "selectors": [norm2(sub)]}
    return None


def match_row(pool, qn):
    """在 PDF 行池里找与接口题干对应的一行（PDF 会丢公式图片，题干可能是子序列）"""
    for r in pool:
        if r["qtext"] == qn:
            return r
    cands = [r for r in pool if len(r["qtext"]) >= 6 and is_subseq(r["qtext"], qn)]
    if cands:
        return max(cands, key=lambda r: len(r["qtext"]))
    cand = difflib.get_close_matches(qn, [r["qtext"] for r in pool], n=1, cutoff=0.7)
    if cand:
        return next(r for r in pool if r["qtext"] == cand[0])
    return None


def bank_candidates(bank, qn):
    """取该题干的候选答案规格。
    注意：不同实验里可能都有「本实验的实验目的是()」这类同题干、不同选项的题，
    所以一个题干下存多条规格，由调用方按「能否唯一命中选项」来挑选。"""
    exact = bank.get(qn)
    if exact:
        return exact if isinstance(exact, list) else [exact]
    keys = [k for k in bank if len(k) >= 6 and is_subseq(k, qn)]
    if not keys:
        return []
    mx = max(len(k) for k in keys)
    out = []
    for k in keys:
        if len(k) == mx:
            v = bank[k]
            out.extend(v if isinstance(v, list) else [v])
    return out


def _apply_spec(spec, q):
    """把一条规格落到这份卷子上；命中不唯一返回 None"""
    if spec.get("type") == "judge":
        return [spec["selectors"][0]] if q["type"] == "judge" else None
    if q["type"] == "judge":
        return None
    opts = [(norm2(o["text"]), o["id"]) for o in q["options"]]
    letters = []
    for s in spec["selectors"]:
        hit = [L for t, L in opts if (t == s if spec.get("exact") else s in t)]
        if len(hit) != 1:
            return None
        letters.append(hit[0])
    return sorted(set(letters))


def resolve_from_bank(bank, paper):
    """用共享题库直接算出这份卷子的答案（无需任何探测交卷）。
    返回 (answers, missing)；某题候选规格给出的答案不唯一时，算 missing 交给探测流程。"""
    answers, missing = {}, []
    for q in paper["questions"]:
        sols = set()
        for spec in bank_candidates(bank, norm(q["content"])):
            letters = _apply_spec(spec, q)
            if letters:
                sols.add(tuple(letters))
        if len(sols) == 1:
            answers[q["id"]] = list(sols.pop())
        else:
            missing.append(q)
    return answers, missing


def pick_option_seq(block):
    """挑出真正的选项标记序列。
    坑: 题干里可能出现「系数A、S」这类被误认成选项标记的字符。
    做法: 枚举每个 'A.' 作起点，取「连续字母最多 + 前面以括号/句号结尾」的那组。"""
    markers = [(mm.start(), mm.group(1), mm.end()) for mm in MARKER_RE.finditer(block)]
    best, best_key = [], None
    for i, (p0, L0, _e0) in enumerate(markers):
        if L0 != "A":
            continue
        seq, expect = [], "A"
        for pos, L, end in markers[i:]:
            if L == expect:
                seq.append((pos, L, end))
                expect = chr(ord(L) + 1)
        prefix = block[:p0].rstrip()
        punct = 1 if prefix and prefix[-1] in "）)】」。.：:；;、,，" else 0
        key = (len(seq), punct, p0)
        if best_key is None or key > best_key:
            best, best_key = seq, key
    return best


# ============================ 依赖自举 ============================
DEP_NAME = "pdfplumber"
MIRROR_SH = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _has_dep():
    try:
        importlib.invalidate_caches()
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


def _pip_install(pkg, index=None, user=False):
    """给「当前正在跑的解释器」装包。返回 (是否成功, 输出末尾一行)"""
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if index:
        cmd += ["-i", index]
    if user:
        cmd += ["--user"]
    cmd.append(pkg)
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    out = (r.stdout or b"").decode("utf-8", "replace")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return r.returncode == 0, (lines[-1] if lines else "无输出")


def ensure_deps():
    """确保 pdfplumber 可用，缺了就自动装（Pillow 是它的依赖，会一起装上）。
    关掉自动安装：设环境变量 LABQUIZ_NO_INSTALL=1。"""
    if getattr(sys, "frozen", False):
        # 打包成 exe 时依赖已内置，pip 也用不了（sys.executable 指向 exe 自身）
        return _has_dep()

    if _has_dep():
        return True

    print("\n⚠️  当前 Python 缺少 %s（PDF 解析必需）" % DEP_NAME)
    print("    解释器: %s" % sys.executable)
    if os.environ.get("LABQUIZ_NO_INSTALL"):
        print("    已设 LABQUIZ_NO_INSTALL=1，跳过自动安装。\n")
        return False

    print("    正在自动安装，首次约十几秒，装过一次以后就不再需要…")
    for index, user in [(None, False), (None, True), (MIRROR_SH, False), (MIRROR_SH, True)]:
        print("    · 试 %s%s" % (index or "默认源", " + --user" if user else ""))
        ok, tail = _pip_install(DEP_NAME, index, user)
        if ok and _has_dep():
            print("    ✅ %s 安装成功，继续。\n" % DEP_NAME)
            return True
        print("      %s" % tail[:150])
        if "No module named pip" in tail or ("pip" in tail and "not found" in tail):
            print("      → 这个 Python 里没有 pip，先执行：")
            print('         "%s" -m ensurepip --upgrade' % sys.executable)
            break

    print("""
❌ 自动安装没成功。手动装一次就行（任选其一）：
     "%s" -m pip install pdfplumber
     "%s" -m pip install -i %s pdfplumber
   权限不足 → 命令末尾加 --user
   提示没有 pip → 先跑   "%s" -m ensurepip --upgrade
   完全没有网络 → 换台能上网的机器装好后，把 site-packages 里的 pdfplumber 与
     pdfminer、Pillow、pypdfium2 几个目录拷进来
""" % (sys.executable, sys.executable, MIRROR_SH, sys.executable))
    return False


def ensure_bank_file():
    """打包成 exe 时，把内置的题库释放到 exe 旁边。
    这样分发只需要一个 exe；而一旦释放出来，之后的「自学习」就能持久化写回这个文件。"""
    dest = os.path.join(OUT, "bank.json")
    if os.path.exists(dest):
        return dest
    src = os.path.join(getattr(sys, "_MEIPASS", OUT), "bank.json")
    if not os.path.exists(src):
        return dest
    try:
        with open(src, "rb") as f:
            data = f.read()
        with open(dest, "wb") as f:
            f.write(data)
        print("· 已把内置题库释放到: %s" % dest)
    except Exception as e:
        print("· 题库释放失败（%s），本次将退化为探测流程" % e)
    return dest


def parse_pdf(path):
    """解析阅卷 PDF，返回每题: {num, qtext, opts, is_judge, raw_std, correct_letters, correct_texts}"""
    try:
        import pdfplumber
    except ImportError:
        raise SystemExit("缺少 pdfplumber（已跳过自动安装）。手动装：\n"
                         '  "%s" -m pip install pdfplumber' % sys.executable)
    with pdfplumber.open(path) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)

    results = []
    for m in QPAT.finditer(full):
        num = int(m.group(1))
        block = m.group(2)
        std_raw = m.group(4).strip()
        seq = pick_option_seq(block)
        opts = {}
        for i, (pos, L, end) in enumerate(seq):
            nxt = seq[i + 1][0] if i + 1 < len(seq) else len(block)
            opts[L] = norm(block[end:nxt])
        qtext = norm(block[:seq[0][0]]) if seq else norm(block)
        is_judge = not seq
        results.append({
            "num": num,
            "qtext": qtext,
            "opts": opts,
            "is_judge": is_judge,
            "raw_std": std_raw,
            "correct_letters": [] if is_judge else re.findall(r"[A-Z]", std_raw.upper()),
            "correct_texts": [] if is_judge else [opts.get(L, "") for L in
                                                 re.findall(r"[A-Z]", std_raw.upper())],
        })
    if not results:
        raise SystemExit("PDF 解析失败：没有解析到任何题目（%s）" % path)
    return results


def render_pdf_pages(pdf_path, outdir, dpi=300):
    """把 PDF 每页渲染成 PNG，便于人工核对公式题"""
    import pdfplumber
    os.makedirs(outdir, exist_ok=True)
    outs = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, p in enumerate(pdf.pages):
            f = os.path.join(outdir, "%s_p%d.png" % (os.path.splitext(os.path.basename(pdf_path))[0], i + 1))
            p.to_image(resolution=dpi).save(f)
            outs.append(f)
    return outs


# ============================ 客户端 ============================
class LabQuiz:
    def __init__(self, base=DEFAULT_BASE, user=None, password=None, verbose=True):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.token = None
        self.verbose = verbose

    # ---------- HTTP ----------
    def _req(self, method, path, body=None, token=None, raw=False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "application/json")
        if token:
            r.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                b = resp.read()
                return resp.status, (b if raw else json.loads(b.decode("utf-8") or "null"))
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, (b if raw else json.loads(b.decode("utf-8") or "null"))
            except Exception:
                return e.code, b
        except urllib.error.URLError as e:
            raise SystemExit("连不上 %s —— %s\n"
                             "（该站点是校园内网，需在校园网/内网环境下访问）" % (url, e.reason))

    def login(self):
        st, d = self._req("POST", "/auth/login/", {"username": self.user, "password": self.password})
        if st != 200:
            raise SystemExit("登录失败（%s）：%s" % (st, d))
        self.token = d.get("access")
        st, me = self._req("GET", "/auth/me/", token=self.token)
        self.me = me if st == 200 else {}
        self._log("登录成功: %s（%s / %s）"
                  % (self.me.get("name", "?"), self.user, self.me.get("class_name", "?")))
        return self.me

    def _log(self, *a):
        if self.verbose:
            print(*a)

    # ---------- 业务 ----------
    def previews(self):
        st, d = self._req("GET", "/previews/?page_size=200", token=self.token)
        if st != 200:
            raise SystemExit("获取预习列表失败（%s）：%s" % (st, d))
        return d.get("results", d) if isinstance(d, dict) else d

    def attempts(self):
        st, d = self._req("GET", "/attempts/?page_size=500", token=self.token)
        if st != 200:
            return []
        return d.get("results", d) if isinstance(d, dict) else d

    def fetch_paper(self, pid):
        st, d = self._req("GET", "/previews/%s/questions/" % pid, token=self.token)
        if st != 200:
            detail = d.get("detail") if isinstance(d, dict) else d
            raise SystemExit("取卷失败 preview=%s（%s）：%s" % (pid, st, detail))
        return d

    def fetch_paper_safe(self, pid):
        """取卷；次数用尽等情况下返回 (None, 原因)，不抛异常"""
        st, d = self._req("GET", "/previews/%s/questions/" % pid, token=self.token)
        if st != 200:
            return None, (d.get("detail") if isinstance(d, dict) else str(d))
        return d, None

    def submit(self, pid, answers, paper):
        """注意: paper 必须是「算答案时用的那一份取卷结果」"""
        payload = {
            "question_order": paper["question_order"],
            "option_orders": paper["option_orders"],
            "answers": {str(k): v for k, v in answers.items()},
        }
        st, res = self._req("POST", "/previews/%s/submit/" % pid, payload, token=self.token)
        if st != 200:
            return None, (st, res)
        return res, None

    def get_pdf(self, attempt_id, dest, tries=15, wait=2):
        """阅卷 PDF 异步生成，pending 时返回 400，需轮询"""
        last = None
        for _ in range(tries):
            st, b = self._req("GET", "/attempts/%s/pdf/" % attempt_id, token=self.token, raw=True)
            if st == 200 and b[:4] == b"%PDF":
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(b)
                return dest
            last = (st, (b[:120] if isinstance(b, bytes) else b))
            time.sleep(wait)
        raise SystemExit("PDF 下载失败 attempt=%s %s" % (attempt_id, last))

    # ---------- 答案映射 ----------
    def build_answers(self, paper, rows, overrides, pid=None, pdf=None):
        overrides = overrides or []
        answers, report, missing = {}, [], []
        pool = list(rows)
        for q in paper["questions"]:
            qn = norm(q["content"])
            row = match_row(pool, qn)
            if row is None:
                raise SystemExit("题目无法匹配到 PDF: %s" % qn[:60])
            pool.remove(row)

            if q["type"] == "judge":
                tf = re.findall(r"[TF]", row["raw_std"].upper())
                if not tf:
                    raise SystemExit("判断题标准答案解析失败: %s" % row["raw_std"])
                letters = [tf[0]]
            else:
                opts_n1 = {norm(o["text"]): o["id"] for o in q["options"]}
                opts_n2 = [(norm2(o["text"]), o["id"]) for o in q["options"]]
                need = list(row["correct_texts"])
                if not need or any(t == "" for t in need):
                    # 选项是公式图片 -> 人工核对出来的覆盖规则
                    needle = None
                    for key, sub in overrides:
                        if key in qn:
                            needle = norm2(sub)
                            break
                    if needle is None:
                        missing.append((q, row))
                        continue
                    hit = sorted({L for t, L in opts_n2 if needle in t})
                    if len(hit) != 1:
                        raise SystemExit("覆盖规则命中 %d 个选项: %s | %s" % (len(hit), qn[:30], needle))
                    letters = hit
                else:
                    letters = []
                    for ct in need:
                        if ct in opts_n1:
                            letters.append(opts_n1[ct])
                            continue
                        c2 = norm2(ct)
                        hit = [L for t, L in opts_n2 if t == c2]
                        if not hit:
                            m2 = difflib.get_close_matches(ct, list(opts_n1), n=1, cutoff=0.85)
                            if not m2:
                                raise SystemExit("选项文本匹配失败: %s | %s" % (qn[:30], ct[:40]))
                            hit = [opts_n1[m2[0]]]
                        letters.append(hit[0])
            letters = sorted(set(letters))
            answers[q["id"]] = letters
            report.append((q["id"], q["type"], letters, row["correct_letters"] or [row["raw_std"]]))
        return answers, report, missing

    def write_pending(self, pid, missing, pdf, rows):
        """遇到没有覆盖规则的公式题：渲染 PDF 页面 + 落一份待办清单，人工核对后补规则"""
        shots = render_pdf_pages(pdf, os.path.join(OUT, "shots")) if pdf else []
        p = os.path.join(OUT, "pending_overrides.json")
        data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
        for q, row in missing:
            data.append({
                "preview_id": pid,
                "question_id": q["id"],
                "question": q["content"],
                "pdf_standard_answer": row["correct_letters"] or row["raw_std"],
                "options": [{"id": o["id"], "text": o["text"]} for o in q["options"]],
                "shots": shots,
            })
        json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n⚠️  有 %d 道题的选项是公式图片，PDF 提不出文本，需要人工核对：" % len(missing))
        for q, row in missing:
            print("   · preview=%s 题%s  标准答案字母=%s" % (pid, q["id"], row["correct_letters"]))
            print("     题干: %s" % q["content"][:80])
            for o in q["options"]:
                print("       %s. %s" % (o["id"], o["text"]))
        print("   已把该次阅卷 PDF 渲染成图片: %s" % (shots[:3] or "无"))
        print("   待办清单: %s" % p)
        print('   → 看图确认正确答案后，往同目录 overrides.json 里加一条 ["题干关键词", "正确选项文本子串"]')
        print('     （也可以直接写进脚本里的 FORMULA_OVERRIDES），再重跑。')
        return p


# ============================ 命令实现 ============================
def cmd_summary(cli, args):
    items = cli.previews()
    best = {}
    for a in cli.attempts():
        best[a["preview"]] = max(best.get(a["preview"], 0), a["score"])
    print("\n%-4s %-28s %-7s %-7s %-6s %s" % ("id", "实验", "满分", "最高分", "已预习", "剩余次数"))
    print("-" * 74)
    for it in items:
        paper, err = cli.fetch_paper_safe(it["id"])
        rem = paper["remaining_attempts"] if paper else ("0(%s)" % err if err else "-")
        print("%-4s %-28s %-7s %-7s %-6s %s"
              % (it["id"], it["experiment_name"], it["total_score"],
                 best.get(it["id"], "-"), "✅" if it["done"] else "❌", rem))
    full = sum(1 for it in items if best.get(it["id"]) == it["total_score"])
    print("\n满分 %d / %d" % (full, len(items)))
    return items


def _texts_of(paper, qid, letters):
    """把某题的答案字母翻译成选项文本（判断题直接给字母）。
    选项文本跨取卷稳定，字母会因重排而变，所以比对一律走文本。"""
    if paper is None:
        return []
    q = next((x for x in paper["questions"] if x["id"] == qid), None)
    if q is None:
        return []
    if q["type"] == "judge":
        return list(letters)
    m = {o["id"]: norm2(o["text"]) for o in q["options"]}
    return sorted(m.get(L, "?") for L in letters)


def _letters_by_texts(paper, qid, texts):
    """把选项文本反查成「本次取卷」的字母（选项顺序每次取卷都会变）。
    任何一个文本在新卷里找不到唯一选项就返回 None，调用方会退回用标准答案。"""
    if not texts:
        return None
    q = next((x for x in paper["questions"] if x["id"] == qid), None)
    if q is None:
        return None
    if q["type"] == "judge":
        return sorted(set(texts))
    m = {norm2(o["text"]): o["id"] for o in q["options"]}
    out = []
    for t in texts:
        if t not in m:
            return None
        out.append(m[t])
    return sorted(set(out))


def _learn_bank(paper, rows, overrides):
    """自学习：把本轮阅卷 PDF 的标准答案回写共享题库，让别的账户第一次就交满分。
    - 题库没有该题干        → 新增
    - 已有规格能算出正确答案 → 不动（绝不覆盖正确的）
    - 单规格但算错了        → 直接修正
    - 多规格（同题干不同实验撞车）→ 只追加，交给探测流程兜底，避免误删别人实验的答案"""
    bank = load_bank()
    if not bank:
        return 0
    pool, added, fixed = list(rows), 0, 0
    for q in paper["questions"]:
        key = norm(q["content"])
        row = match_row(pool, key)
        if row is None:
            continue
        pool.remove(row)
        spec = selectors_from_row(row, overrides, q)
        if spec is None:
            continue
        good = _apply_spec(spec, q)
        if good is None:
            continue
        bkey = row["qtext"]
        if bkey not in bank:
            alt = [k for k in bank if len(k) >= 6 and is_subseq(k, key)]
            if alt:
                bkey = max(alt, key=len)
        old = bank.get(bkey)
        if not old:
            bank[bkey] = [spec]
            added += 1
            continue
        if not isinstance(old, list):
            old = [old]
            bank[bkey] = old
        if any(_apply_spec(s, q) == good for s in old):
            continue
        if len(old) == 1:
            bank[bkey] = [spec]
            fixed += 1
        elif spec not in old:
            old.append(spec)
            fixed += 1
    if added or fixed:
        save_bank(bank)
        print("   ↳ 共享题库已更新：新增 %d 题 / 修正 %d 题" % (added, fixed))
    return added + fixed


def _converge(cli, pid, name, aid, args, overrides, prev=None, tag="自纠正"):
    """提交后未满分的自纠正闭环。
    阅卷 PDF（带标准答案）→ 重取卷（选项顺序已重排）→ 按选项文本反查新字母 → 重交。
    只要还有剩余次数就必定收敛到满分；同时把标准答案回写共享题库。
    返回 (status, pid, name, score, perfect, paper, answers)"""
    paper_prev, ans_prev = (prev or (None, {}))
    rounds = 0
    while True:
        pdf = cli.get_pdf(aid, os.path.join(OUT, "pdf_%s_%s.pdf" % (pid, aid)))
        rows = parse_pdf(pdf)
        paper = cli.fetch_paper(pid)
        if paper["remaining_attempts"] <= 0:
            print("[%s] %-22s %s：剩余次数为 0，停止" % (pid, name, tag))
            return ("blocked", pid, name, None, None, paper, {})
        answers, _report, missing = cli.build_answers(paper, rows, overrides, pid=pid, pdf=pdf)
        if missing:
            cli.write_pending(pid, missing, pdf, rows)
            print("[%s] %-22s %s：%d 道公式题缺覆盖规则，停止" % (pid, name, tag, len(missing)))
            return ("manual", pid, name, None, None, paper, {})

        # ---- 逐题比对：只改「上一轮确实答错」的题，答对的题原样保留 ----
        wrong, keep = [], {}
        for q in paper["questions"]:
            qid = q["id"]
            if qid not in answers:
                continue
            right = _texts_of(paper, qid, answers[qid])
            mine = _texts_of(paper_prev, qid, ans_prev.get(qid, [])) if paper_prev is not None else []
            if paper_prev is not None and mine and mine == right:
                # 上次就答对了 → 保持原选择，只把它按文本映射回本次取卷的字母
                old = _letters_by_texts(paper, qid, mine)
                if old:
                    keep[qid] = old
                    continue
            if paper_prev is not None and mine != right:
                wrong.append((qid, mine, right))
            # 没答对的题保持用标准答案（answers[qid] 已就位）

        if paper_prev is not None:
            if wrong:
                print("[%s] %-22s 检出 %d 道错题，只改这些：" % (pid, name, len(wrong)))
                for qid, mine, right in wrong[:6]:
                    print("       · 题%-6s 我选 %s → 改为 %s"
                          % (qid, " / ".join(mine) or "空", " / ".join(right)))
                if len(wrong) > 6:
                    print("       · ...另有 %d 道" % (len(wrong) - 6))
                if keep:
                    print("       其余 %d 道答对的题保持原选择不动" % len(keep))
            else:
                print("[%s] %-22s 逐题比对：没有确认答错的题，全部保持原选择"
                      % (pid, name))

        if keep:
            merged = dict(answers)
            merged.update(keep)
            answers = merged

        if not args.no_learn:
            _learn_bank(paper, rows, overrides)

        res, err = cli.submit(pid, answers, paper)
        if err:
            print("[%s] %-22s %s交卷失败 %s" % (pid, name, tag, err))
            return ("fail", pid, name, None, None, paper, answers)
        rounds += 1
        print("[%s] %-22s >>> %s 第 %d 轮 %s / %s（剩余 %s 次）"
              % (pid, name, tag, rounds, res["score"], res["total_score"], res["remaining_attempts"]))
        if res["score"] == res["total_score"]:
            return ("ok", pid, name, res["score"], True, paper, answers)
        if res["remaining_attempts"] <= 0:
            print("[%s] %-22s %s 未能满分且次数用尽（%s / %s）"
                  % (pid, name, tag, res["score"], res["total_score"]))
            return ("partial", pid, name, res["score"], False, paper, answers)
        aid, paper_prev, ans_prev = res["attempt_id"], paper, answers


def _solve_one(cli, pv, args, overrides, all_attempts):
    pid = pv["id"]
    name = pv["experiment_name"]
    total = pv["total_score"]
    hist = [a for a in all_attempts if a["preview"] == pid]
    best = max([a["score"] for a in hist], default=None)

    if best == total and not args.force:
        print("[%s] %-22s 已满分(%s)，跳过" % (pid, name, best))
        return ("skip", pid, name, best, True)

    paper, err = cli.fetch_paper_safe(pid)
    if paper is None:
        print("[%s] %-22s 无法进入答题：%s（最高分 %s）" % (pid, name, err, best))
        return ("blocked", pid, name, best, None)
    total = paper["total_score"] or total

    if paper["remaining_attempts"] <= 0:
        print("[%s] %-22s 剩余次数为 0，无法提交（最高分 %s）" % (pid, name, best))
        return ("blocked", pid, name, best, None)

    # ---- 路径 A：共享题库直交（题目全账户一致，能省掉一次「探测」机会）----
    if not args.no_bank:
        bank = load_bank()
        if bank:
            answers, missing = resolve_from_bank(bank, paper)
            if not missing:
                if args.dry_run:
                    print("[%s] %-22s [dry-run] 题库命中 %d/%d 题，可一次交到满分（未提交）"
                          % (pid, name, len(answers), len(paper["questions"])))
                    return ("dry", pid, name, best, None)
                res, err = cli.submit(pid, answers, paper)
                if err:
                    print("[%s] %-22s 题库直交失败 %s" % (pid, name, err))
                    return ("fail", pid, name, best, None)
                print("[%s] %-22s >>> 题库直交 %s / %s（剩余 %s 次）"
                      % (pid, name, res["score"], res["total_score"], res["remaining_attempts"]))
                if res["score"] == res["total_score"]:
                    return ("ok", pid, name, res["score"], True)
                if args.no_retry:
                    return ("partial", pid, name, res["score"], False)
                print("[%s] %-22s 题库直交未满分（%s 分），启动自纠正" % (pid, name, res["score"]))
                st, _p, _n, sc, perfect, _pa, _an = _converge(
                    cli, pid, name, res["attempt_id"], args, overrides,
                    prev=(paper, answers), tag="自纠正")
                return (st, pid, name, sc, perfect)
            else:
                cli._log("[%s] %-22s 题库缺 %d 题，改用探测流程" % (pid, name, len(missing)))

    src = None
    if hist and not args.reprobe:
        src = max(hist, key=lambda a: a["id"])
        pdf = os.path.join(OUT, "pdf_%s_%s.pdf" % (pid, src["id"]))
        cli._log("[%s] %-22s 复用已有作答 PDF attempt=%s（最高分 %s）" % (pid, name, src["id"], best))
    else:
        if args.dry_run:
            print("[%s] %-22s [dry-run] 需要探测交卷才能拿到答案，已跳过" % (pid, name))
            return ("dry", pid, name, best, None)
        paper = cli.fetch_paper(pid)   # 重新取卷，保证 option_orders 与下面的字母同源
        guess = {str(q["id"]): [q["options"][0]["id"] if q["options"] else "T"]
                 for q in paper["questions"]}
        res, err = cli.submit(pid, guess, paper)
        if err:
            print("[%s] %-22s 探测交卷失败 %s" % (pid, name, err))
            return ("fail", pid, name, best, None)
        print("[%s] %-22s 探测交卷 %s 分（剩余 %s 次）"
              % (pid, name, res["score"], res["remaining_attempts"]))
        pdf = os.path.join(OUT, "pdf_%s_%s.pdf" % (pid, res["attempt_id"]))
        src = {"id": res["attempt_id"]}

    pdf = cli.get_pdf(src["id"], pdf)
    rows = parse_pdf(pdf)

    paper2 = cli.fetch_paper(pid)
    answers, report, missing = cli.build_answers(paper2, rows, overrides, pid=pid, pdf=pdf)
    if missing:
        if args.dry_run:
            cli.write_pending(pid, missing, pdf, rows)
            return ("manual", pid, name, best, None)
        cli.write_pending(pid, missing, pdf, rows)
        print("[%s] %-22s 有 %d 道公式题缺规则，本次不提交" % (pid, name, len(missing)))
        return ("manual", pid, name, best, None)

    if args.dry_run:
        print("[%s] %-22s [dry-run] 解析 %d 题 → 答案已算出，未提交" % (pid, name, len(report)))
        return ("dry", pid, name, best, None)

    if paper2["remaining_attempts"] <= 0:
        print("[%s] %-22s 剩余次数为 0，无法提交" % (pid, name))
        return ("blocked", pid, name, best, None)

    res, err = cli.submit(pid, answers, paper2)
    if err:
        print("[%s] %-22s 交卷失败 %s" % (pid, name, err))
        return ("fail", pid, name, best, None)
    print("[%s] %-22s >>> %s / %s  （剩余 %s 次）"
          % (pid, name, res["score"], res["total_score"], res["remaining_attempts"]))
    if res["score"] == res["total_score"]:
        return ("ok", pid, name, res["score"], True)
    if args.no_retry:
        return ("partial", pid, name, res["score"], False)
    print("[%s] %-22s 未满分，启动自纠正（下载阅卷 PDF → 补正 → 重交）" % (pid, name))
    st, _p, _n, sc, perfect, _pa, _an = _converge(
        cli, pid, name, res["attempt_id"], args, overrides,
        prev=(paper2, answers), tag="自纠正")
    return (st, pid, name, sc, perfect)


def export_score_pdfs(cli, args):
    """把每份卷子「得分最高那次」的阅卷 PDF 导出到本地，文件名带上实验名和分数。
    已经导出过的同名文件会跳过，所以反复运行不会重复下载。"""
    outdir = getattr(args, "pdf_dir", None) or os.path.join(OUT, "阅卷PDF")
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception as e:
        print("\n⚠️  建不了 PDF 目录 %s（%s）" % (outdir, e))
        return []

    try:
        att = cli.attempts()
        pvs = cli.previews()
    except SystemExit as e:
        print("\n⚠️  导出阅卷 PDF 失败：%s" % e)
        return []

    saved, failed, skipped = [], [], 0
    for pv in pvs:
        pid, name = pv["id"], pv["experiment_name"]
        hist = [a for a in att if a["preview"] == pid]
        if not hist:
            continue
        best = max(hist, key=lambda a: (a["score"], a["id"]))
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or ("实验%s" % pid)
        dest = os.path.join(outdir, "%02d_%s_%d分.pdf" % (pid, safe, best["score"]))
        if os.path.exists(dest):
            skipped += 1
            continue
        try:
            cli.get_pdf(best["id"], dest)
            saved.append(os.path.basename(dest))
        except SystemExit as e:
            failed.append("%s(%s)" % (name, str(e)[:60]))

    print("\n---------------- 阅卷 PDF ----------------")
    print("导出目录: %s" % outdir)
    if saved:
        print("本次新增 %d 份:" % len(saved))
        for f in saved:
            print("   · %s" % f)
    if skipped:
        print("已存在跳过 %d 份" % skipped)
    if failed:
        print("失败 %d 份: %s" % (len(failed), "；".join(failed[:3])))
    if not (saved or skipped):
        print("还没有任何作答记录，没有可导出的 PDF")
    return saved


def cmd_solve(cli, args):
    items = cli.previews()
    if args.id:
        want = {int(x) for x in re.split(r"[,\s]+", args.id) if x.strip()}
        items = [it for it in items if it["id"] in want]
        if not items:
            raise SystemExit("指定的卷号不存在: %s" % args.id)
    if args.only_undone:
        items = [it for it in items if not it["done"]]

    print("准备处理 %d 份: %s\n" % (len(items), ", ".join("%s(%s)" % (i["id"], i["experiment_name"]) for i in items)))
    all_attempts = cli.attempts()
    results = []
    for pv in items:
        try:
            results.append(_solve_one(cli, pv, args, all_overrides(), all_attempts))
        except SystemExit as e:
            print("[%s] %-22s 出错: %s" % (pv["id"], pv["experiment_name"], e))
            results.append(("fail", pv["id"], pv["experiment_name"], None, None))
        except Exception as e:  # 单份失败不拖垮整批
            print("[%s] %-22s 异常: %s: %s" % (pv["id"], pv["experiment_name"], type(e).__name__, e))
            results.append(("fail", pv["id"], pv["experiment_name"], None, None))

    print("\n================ 汇总 ================")
    tag = {"ok": "✅", "skip": "⏭️", "dry": "🧪", "manual": "✋", "blocked": "🚫",
           "fail": "❌", "partial": "⚠️"}
    for st, pid, name, score, perfect in results:
        print("%-4s %-28s %-6s %s" % (tag.get(st, st), name, score if score is not None else "-",
                                      "满分" if perfect else ""))

    # 自动把阅卷 PDF 导出到本地，方便留存 / 交给老师
    if not args.no_pdf and not args.dry_run:
        export_score_pdfs(cli, args)

    return results


def cmd_finish(cli, args):
    pid, aid = args.id, args.attempt
    if not pid or not aid:
        raise SystemExit("finish 需要 --id 和 --attempt")
    pdf = cli.get_pdf(aid, os.path.join(OUT, "pdf_%s_%s.pdf" % (pid, aid)))
    rows = parse_pdf(pdf)
    paper = cli.fetch_paper(pid)
    if paper["remaining_attempts"] <= 0:
        raise SystemExit("剩余次数为 0，无法提交")
    answers, report, missing = cli.build_answers(paper, rows, all_overrides(), pid=pid, pdf=pdf)
    if missing:
        cli.write_pending(pid, missing, pdf, rows)
        raise SystemExit("有 %d 道公式题缺覆盖规则，未提交" % len(missing))
    if args.dry_run:
        for qid, typ, letters, std in report:
            print("   题%-6s %-8s -> %-5s (PDF标准:%s)" % (qid, typ, "".join(letters), "".join(std)))
        print("dry-run：未提交")
        return
    res, err = cli.submit(pid, answers, paper)
    if err:
        raise SystemExit("交卷失败 %s" % (err,))
    print("[%s] >>> 最终得分 = %s / %s   剩余次数 = %s"
          % (pid, res["score"], res["total_score"], res["remaining_attempts"]))


def cmd_check(cli, args):
    rows = parse_pdf(args.pdf)
    paper = cli.fetch_paper(args.id)
    answers, report, missing = cli.build_answers(paper, rows, all_overrides(), pid=args.id, pdf=args.pdf)
    print("[%s] PDF %d 题 / 接口 %d 题  剩余次数=%s"
          % (args.id, len(rows), len(paper["questions"]), paper["remaining_attempts"]))
    for qid, typ, letters, std in report:
        print("   题%-6s %-8s -> %-5s (PDF标准:%s)" % (qid, typ, "".join(letters), "".join(std)))
    if missing:
        cli.write_pending(args.id, missing, args.pdf, rows)
        raise SystemExit("有 %d 道公式题缺覆盖规则" % len(missing))
    print("[%s] OK 全部解析成功（未提交）" % args.id)


def cmd_build_bank(cli, args):
    """从本账户的满分阅卷 PDF 里抽出「题干 -> 正确选项」题库，供其它账户直接使用"""
    want = None
    if args.id:
        want = {int(x) for x in re.split(r"[,\s]+", args.id) if x.strip()}
    overrides = all_overrides()
    all_att = cli.attempts()
    qmap, stat = {}, []
    for pv in cli.previews():
        pid = pv["id"]
        if want and pid not in want:
            continue
        total = pv["total_score"]
        hist = [a for a in all_att if a["preview"] == pid and a["score"] == total]
        if not hist:
            print("[%s] %-22s 没有满分记录，跳过" % (pid, pv["experiment_name"]))
            continue
        aid = max(hist, key=lambda a: a["id"])["id"]
        pdf = cli.get_pdf(aid, os.path.join(OUT, "pdf_%s_%s.pdf" % (pid, aid)))
        rows = parse_pdf(pdf)
        paper, _err = cli.fetch_paper_safe(pid)
        if paper is not None:
            pool, pairs = list(rows), []
            for q in paper["questions"]:
                row = match_row(pool, norm(q["content"]))
                if row is None:
                    continue
                pool.remove(row)
                pairs.append((q, row))
            how = "接口题干对齐"
        else:
            pairs, how = [(None, r) for r in rows], "仅用PDF题干"
        n = 0
        for q, row in pairs:
            spec = selectors_from_row(row, overrides, q)
            if spec is None:
                print("   ⚠️ 第%s题 生成选择器失败（缺覆盖规则），跳过" % row["num"])
                continue
            lst = qmap.setdefault(row["qtext"], [])
            if spec not in lst:
                lst.append(spec)
            n += 1
        print("[%s] %-22s 入库 %d 题（%s）" % (pid, pv["experiment_name"], n, how))
        stat.append((pid, n))
    save_bank(qmap)
    print("\n题库已写入: %s（题干 %d 条 / 规格 %d 条）"
          % (BANK_PATH, len(qmap), sum(len(v) for v in qmap.values())))


def cmd_verify_bank(cli, args):
    """校验题库：用题库算出的答案，必须与阅卷 PDF 的答案指向同一批选项"""
    bank = load_bank()
    if not bank:
        raise SystemExit("题库为空，先跑 build-bank")
    overrides = all_overrides()
    all_att = cli.attempts()
    bad = total_q = 0

    def sel_texts(q, letters):
        m = {o["id"]: norm2(o["text"]) for o in q["options"]}
        return sorted(m.get(L, "?") for L in letters)

    for pv in cli.previews():
        pid, total = pv["id"], pv["total_score"]
        hist = [a for a in all_att if a["preview"] == pid and a["score"] == total]
        if not hist:
            print("[%s] %-22s 无满分记录，跳过" % (pid, pv["experiment_name"]))
            continue
        aid = max(hist, key=lambda a: a["id"])["id"]
        pdf = cli.get_pdf(aid, os.path.join(OUT, "pdf_%s_%s.pdf" % (pid, aid)))
        rows = parse_pdf(pdf)
        paper, err = cli.fetch_paper_safe(pid)
        if paper is None:
            print("[%s] %-22s 取不到卷（%s），跳过" % (pid, pv["experiment_name"], err))
            continue
        got, _missing = resolve_from_bank(bank, paper)
        ref, _rep, _m = cli.build_answers(paper, rows, overrides, pid=pid, pdf=pdf)
        diff = []
        for q in paper["questions"]:
            qid = q["id"]
            if qid not in got or qid not in ref:
                diff.append((qid, "题库缺失"))
                continue
            if q["type"] == "judge":
                same = got[qid] == ref[qid]
            else:
                same = sel_texts(q, got[qid]) == sel_texts(q, ref[qid])
            if not same:
                diff.append((qid, "%s vs %s" % ("".join(got[qid]), "".join(ref[qid]))))
            total_q += 1
        if diff:
            bad += len(diff)
            print("[%s] %-22s ⚠️ 不一致 %d 题: %s" % (pid, pv["experiment_name"], len(diff), diff[:4]))
        else:
            print("[%s] %-22s ✅ 题库解与 PDF 解一致（%d 题）" % (pid, pv["experiment_name"], len(paper["questions"])))
    print("\n校验完毕：共比对 %d 题，不一致 %d 题" % (total_q, bad))


def cmd_parse(cli, args):
    for r in parse_pdf(args.pdf):
        tag = "判断题" if r["is_judge"] else "选 择 题"
        print("%-3s %s 标准=%-6s %s" % (r["num"], tag, "".join(r["correct_letters"]) or r["raw_std"], r["qtext"][:40]))
        for L, t in r["opts"].items():
            print("        %s. %s" % (L, t[:60]))


def cmd_shots(cli, args):
    if args.attempt:
        pdf = cli.get_pdf(args.attempt, os.path.join(OUT, "pdf_%s_%s.pdf" % (args.id, args.attempt)))
    else:
        pdf = args.pdf
    print("\n".join(render_pdf_pages(pdf, os.path.join(OUT, "shots"))))


def _interactive(args):
    """双击 exe / bat 时的傻瓜入口：问学号/密码 → 直接跑 solve（带自纠正）"""
    # 双击 exe（没给子命令）时 args 是顶层命名空间，这里补齐 solve 需要的开关
    for k, v in (("id", None), ("dry_run", False), ("force", False), ("reprobe", False),
                 ("only_undone", False), ("no_bank", False),
                 ("no_pdf", False), ("pdf_dir", None)):
        if not hasattr(args, k):
            setattr(args, k, v)
    base = getattr(args, "base", DEFAULT_BASE)

    bar = "=" * 58
    print("\n" + bar)
    print("   实验预习答题系统 · 一键答题")
    print("   目标站点: %s" % base)
    print(bar)

    user = (getattr(args, "user", None) or os.environ.get("LABQUIZ_USER") or "").strip()
    pw = (getattr(args, "password", None) or os.environ.get("LABQUIZ_PASSWORD") or "").strip()

    if not user:
        try:
            user = input("请输入学号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n（没有输入，已退出）")
            return []
    if not user:
        print("\n❌ 没有输入学号，已取消。")
        return []

    if not pw:
        try:
            pw = getpass.getpass("请输入密码（不显示；直接回车 = 与学号相同）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n（没有输入，已退出）")
            return []
        if not pw:
            pw = user

    print()
    cli = LabQuiz(base, user, pw)
    try:
        cli.login()
    except SystemExit as e:
        print("\n❌ %s" % e)
        print("   请检查：1) 学号/密码是否正确  2) 是否连着校园网（该站点只在校园内网可访问）")
        return []
    print("✅ 登录成功，开始处理…\n")

    try:
        out = cmd_solve(cli, args)
    except SystemExit as e:
        print("\n❌ %s" % e)
        return []
    print("\n" + bar)
    print("   处理完毕。")
    print(bar)
    return out


# ============================ 入口 ============================
# 这些命令要解析阅卷 PDF，因此依赖 pdfplumber（summary 不需要，就不强求）
NEEDS_PDF = {"solve", "build-bank", "verify-bank", "finish", "check", "parse", "shots", "interactive"}
# 全部子命令名（用来判断用户到底有没有给子命令）
CMD_NAMES = {"summary", "interactive", "solve", "build-bank", "verify-bank",
             "finish", "check", "parse", "shots"}


def build_parser():
    p = argparse.ArgumentParser(
        description="实验预习答题系统 自动答题工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
账号优先级: --account > --user/--password > 环境变量 LABQUIZ_USER/LABQUIZ_PASSWORD > 交互输入

典型用法
  # 0) 最省事：直接双击本目录的「一键答题.bat」，输入学号 + 密码回车，剩下全自动
  python lab_quiz.py interactive             # 等价于双击 bat（会问学号密码）

  # 1) 看看有哪些卷子、各得多少分
  python lab_quiz.py -u 学号 -p 密码 summary

  # 2) 全部做完（幂等，可反复跑；已满分的自动跳过）
  python lab_quiz.py -u 学号 -p 密码 solve

  # 3) 多个账户一起做（每个账户一条 --account）
  python lab_quiz.py --account A:passA --account B:passB solve

  # 4) 只算答案不交卷，先看看靠不靠谱
  python lab_quiz.py -u 学号 -p 密码 solve --dry-run

共享题库（bank.json）
  题目内容全账户一致，只有题序/选项序随机。所以本目录的 bank.json 存了
  「题干 -> 正确选项」对应关系：别的账户直接用它在**第一次交卷**就拿满分，
  不需要再花一次机会「先随便交一次探答案」。用法：
    python lab_quiz.py -u 学号 -p 密码 build-bank    # 由已满分的账户生成题库
    python lab_quiz.py -u 学号 -p 密码 verify-bank   # 校验题库与阅卷 PDF 是否一致
  想强制走「探测 → 读答案 → 再交」的老流程，加 --no-bank。
  加了 --account 的多账户场景下，若题库缺失某题，会自动退化为探测流程（多花一次机会，但不会错）。

自纠正（默认开启）
  任何一次提交只要没拿到满分，脚本都会自动进入纠正闭环：
    下载这次作答的阅卷 PDF → 解析标准答案 → 重新取卷（选项顺序已重排）
    → 按选项文本反查新字母 → 重交
  并逐题比对「我选的选项文本」和「标准答案选项文本」，把错题打出来；
  同时把标准答案回写 bank.json（只补/修题库，绝不覆盖已知正确的规格）。
  只要还有剩余答题次数，就会一直重交到满分为止。关掉用 --no-retry / --no-learn。

公式图片题
  个别题的选项是 LaTeX 渲染成的图片，PDF 里提不出文字。脚本会自动把阅卷 PDF
  渲染成图片放到 shots/ 并写一份 pending_overrides.json；看图确认正确答案后，
  在同目录 overrides.json 里加一条 ["题干关键词", "正确选项文本子串"] 再重跑即可。

依赖: pdfplumber（Pillow 会随它一起装上）
  缺少时会自动 pip 安装：默认源 → 默认源+--user → 清华源 → 清华源+--user。
  关掉自动安装: 设环境变量 LABQUIZ_NO_INSTALL=1
""")
    p.add_argument("-u", "--user", help="用户名/学号")
    p.add_argument("-p", "--password", help="密码（不传则读环境变量或交互输入）")
    p.add_argument("--account", action="append", default=[],
                   help="用户名:密码，可重复；给多个则依次处理多个账户")
    p.add_argument("--accounts-file", help='JSON 文件 {"用户名":"密码", ...}')
    p.add_argument("--base", default=DEFAULT_BASE, help="API 根地址，默认 %s" % DEFAULT_BASE)
    p.add_argument("--no-retry", action="store_true",
                   help="提交未满分时不启用自纠正（默认启用：阅卷PDF→补正→重交，直到满分或用完次数）")
    p.add_argument("--no-learn", action="store_true",
                   help="自纠正时不把标准答案回写共享题库 bank.json")
    p.add_argument("--no-pdf", action="store_true",
                   help="不自动导出阅卷 PDF")
    p.add_argument("--pdf-dir", help="阅卷 PDF 导出目录，默认 <程序目录>/阅卷PDF")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary", help="列出全部预习任务与得分")

    it = sub.add_parser("interactive", help="傻瓜模式：问学号密码后直接全部做完（双击 exe/bat 用）")
    it.add_argument("-u", "--user", help="学号（不填会交互询问）")
    it.add_argument("-p", "--password", help="密码（不填会交互询问）")
    it.add_argument("--id", help="只处理指定卷号，逗号分隔")
    it.add_argument("--dry-run", action="store_true", help="只算答案不交卷")
    it.add_argument("--force", action="store_true", help="已满分的也重做")
    it.add_argument("--reprobe", action="store_true", help="不复用已有作答，重新探测交卷")
    it.add_argument("--only-undone", action="store_true", help="跳过已标记预习完成的")
    it.add_argument("--no-bank", action="store_true", help="不使用共享题库，强制走探测流程")

    s = sub.add_parser("solve", help="自动处理（幂等，可反复跑）")
    s.add_argument("--id", help="只处理指定卷号，逗号分隔")
    s.add_argument("--dry-run", action="store_true", help="只算答案不交卷")
    s.add_argument("--force", action="store_true", help="已满分的也重做")
    s.add_argument("--reprobe", action="store_true", help="不复用已有作答，重新探测交卷")
    s.add_argument("--only-undone", action="store_true", help="跳过已标记预习完成的")
    s.add_argument("--no-bank", action="store_true", help="不使用共享题库，强制走探测流程")

    b = sub.add_parser("build-bank", help="从本账户的满分阅卷 PDF 抽题库（供其它账户复用）")
    b.add_argument("--id", help="只处理指定卷号，逗号分隔")

    sub.add_parser("verify-bank", help="校验题库与阅卷 PDF 的答案是否一致")

    f = sub.add_parser("finish", help="用已有作答 PDF 直接交卷（省一次探测）")
    f.add_argument("--id", required=True)
    f.add_argument("--attempt", required=True)
    f.add_argument("--dry-run", action="store_true")

    c = sub.add_parser("check", help="用已有 PDF 干跑校验，不交卷")
    c.add_argument("--id", required=True)
    c.add_argument("--pdf", required=True)

    pa = sub.add_parser("parse", help="打印 PDF 解析结果")
    pa.add_argument("pdf")

    sh = sub.add_parser("shots", help="把阅卷 PDF 渲染成图片")
    sh.add_argument("--id")
    sh.add_argument("--attempt")
    sh.add_argument("--pdf")
    return p


def resolve_accounts(args):
    """返回 [(user, password), ...]"""
    out = []
    for a in args.account:
        if ":" not in a:
            raise SystemExit("--account 格式应为 用户名:密码，收到 %r" % a)
        u, pw = a.split(":", 1)
        out.append((u, pw))
    if args.accounts_file:
        with open(args.accounts_file, encoding="utf-8") as f:
            for u, pw in json.load(f).items():
                out.append((u, pw))
    if not out and (args.user or os.environ.get("LABQUIZ_USER")):
        u = args.user or os.environ["LABQUIZ_USER"]
        pw = args.password or os.environ.get("LABQUIZ_PASSWORD")
        if not pw:
            pw = getpass.getpass("请输入 %s 的密码: " % u)
        out.append((u, pw))
    if not out:
        raise SystemExit("未指定账户。用 -u/--user 或 --account 用户名:密码（也可设 LABQUIZ_USER / LABQUIZ_PASSWORD）")
    return out


def main():
    # 中文 Windows 上 stdout 被重定向成管道/文件时编码是 GBK，遇到 emoji 会直接抛
    # UnicodeEncodeError 把程序崩掉（打包成 exe 后尤其明显）→ 统一兜底成不崩
    for _s in ("stdout", "stderr"):
        try:
            getattr(sys, _s).reconfigure(errors="replace")
        except Exception:
            pass

    # 内网访问：清掉沙箱/系统注入的代理变量，避免请求被代理拦掉
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    argv = sys.argv[1:]
    # 双击 exe / 直接跑脚本时没有子命令 → 默认傻瓜模式。
    # 否则 argparse 会打一段 usage 就退出，窗口一闪就没（这就是当初「闪退」的原因）。
    if not any(a in CMD_NAMES for a in argv) and not any(a in ("-h", "--help") for a in argv):
        argv = ["interactive"] + argv
    args = build_parser().parse_args(argv)

    # 需要解析阅卷 PDF 的命令：先确认 pdfplumber 就位，缺了自动装
    if args.cmd in NEEDS_PDF and not ensure_deps():
        raise SystemExit("依赖缺失，已中止。")

    # exe 版：首次运行时把内置题库释放到 exe 旁边，之后的自学习才能写回
    ensure_bank_file()

    if args.cmd == "interactive":
        return _interactive(args)
    accounts = resolve_accounts(args)
    rc_all = []
    for i, (u, pw) in enumerate(accounts):
        if len(accounts) > 1:
            print("\n########## 账户 %d/%d: %s ##########" % (i + 1, len(accounts), u))
        cli = LabQuiz(args.base, u, pw)
        cli.login()
        rc_all.append((u, {
            "summary": cmd_summary,
            "solve": cmd_solve,
            "build-bank": cmd_build_bank,
            "verify-bank": cmd_verify_bank,
            "finish": cmd_finish,
            "check": cmd_check,
            "parse": cmd_parse,
            "shots": cmd_shots,
        }[args.cmd](cli, args)))
    return rc_all


if __name__ == "__main__":
    _code = 0
    try:
        main()
    except SystemExit as e:
        c = e.code
        if c in (None, 0):
            _code = 0
        else:
            if not isinstance(c, int):
                print(c)          # 这类消息本来由解释器打印，被我们接住了就自己打
            _code = c if isinstance(c, int) else 1
    except KeyboardInterrupt:
        print("\n已取消。")
        _code = 130
    except Exception as e:
        if getattr(sys, "frozen", False):
            # 双击场景别甩一堆栈给用户看
            print("\n❌ 出错了：%s: %s" % (type(e).__name__, e))
        else:
            import traceback
            traceback.print_exc()
        _code = 1
    finally:
        # 双击 exe 时结果一闪就没了 —— 只要真的连着终端，结束前停一下
        if getattr(sys, "frozen", False):
            try:
                if sys.stdin and sys.stdin.isatty():
                    input("\n按回车键关闭窗口…")
            except Exception:
                pass
    sys.exit(_code)
