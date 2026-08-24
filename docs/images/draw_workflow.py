#!/usr/bin/env python3
"""Regenerate docs/images/workflow.png — main2main flow diagram (2200x2000)."""
from PIL import Image, ImageDraw, ImageFont
import math

W, H = 2200, 2000
BG = "#f7f8fa"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_R = "/System/Library/Fonts/Supplemental/Arial.ttf"

f_title = ImageFont.truetype(FONT, 34)
f_sub = ImageFont.truetype(FONT_R, 26)
f_small = ImageFont.truetype(FONT_R, 22)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

STYLE = {
    "main":      {"fill": "#ffffff", "border": "#2f5d8a", "title": "#1a3550", "sub": "#6b7c93"},
    "container": {"fill": "#eef4fb", "border": "#8fb4d9", "title": "#1a3550", "sub": "#6b7c93"},
    "decision":  {"fill": "#fff7e8", "border": "#d9a14f", "title": "#7a5a1e", "sub": "#8a7450"},
    "terminal":  {"fill": "#fdf0f0", "border": "#c96a6a", "title": "#7a2626", "sub": "#8a5252"},
    "loop":      {"fill": "#f3faf3", "border": "#4f9d6b", "title": "#1f5c33", "sub": "#4f7a5c"},
}

def node(x, y, w, h, title, sub=None, kind="main"):
    s = STYLE[kind]
    d.rounded_rectangle([x, y, x + w, y + h], radius=18, fill=s["fill"], outline=s["border"], width=3)
    d.text((x + 24, y + 14), title, font=f_title, fill=s["title"])
    if sub:
        d.text((x + 24, y + 58), sub, font=f_sub, fill=s["sub"])
    return (x, y, w, h)

def down(n, dy=40):
    x, y, w, h = n
    return (x + w // 2, y + h), (x + w // 2, y + h + dy)

def right(n, dx=40):
    x, y, w, h = n
    return (x + w, y + h // 2), (x + w + dx, y + h // 2)

def arrow(p1, p2, label=None, dash=False, color="#5a6b7d", label_dx=0, label_dy=0):
    x1, y1 = p1
    x2, y2 = p2
    if dash:
        n = max(int(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / 14), 2)
        pts = [(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
               for t in [i / n for i in range(n + 1) if i % 2 == 0]]
        if pts:
            d.line(pts, fill=color, width=3)
    else:
        d.line([x1, y1, x2, y2], fill=color, width=3)
    ang = math.atan2(y2 - y1, x2 - x1)
    L = 15
    for a in (ang + 2.6, ang - 2.6):
        d.line([x2, y2, x2 + L * math.cos(a), y2 + L * math.sin(a)], fill=color, width=3)
    if label:
        d.text((x1 + label_dx, y1 + label_dy), label, font=f_small, fill="#33475c")

# ---------- layout ----------
MX, MW = 320, 560          # main column (320-880)
RX, RW = 1150, 520         # right column (1150-1670)
LX, LW = 1760, 420         # loop column (1760-2180)
CH_FR, CH_NS, CH_LS = 1690, 1730, 1970   # vertical channels: fail-retry / next-step / lessons

# main column
n_ini = node(MX, 60, MW, 96, "initialize", "clone repos • vllm-report MCP")
n_ana = node(MX, 226, MW, 110, "analyze_commit_and_plan_step", "detect + plan steps • impact (MCP)")
cont = node(MX, 412, MW, 856, "process_steps  (loop)", "per-step adapt → e2e, max 3 retries", "container")
n_adp = node(MX + 28, 482, MW - 56, 96, "opencode adapt + pre_ci", "MCP guides • lessons (fix mode reuses session)")
n_qa = node(MX + 28, 640, MW - 56, 96, "adapter-qa", "review: pass / fail")
n_e2e = node(MX + 28, 880, MW - 56, 96, "_run_e2e_test", "NPU e2e tests (env-flake classified)")
n_gate = node(MX, 1340, MW, 110, "_final_quality_gate", "format + mypy (dual tree) + CPU-UT batches")
n_post = node(MX, 1522, MW, 104, "generate_final_post", "squash + PR body + description-fill")
n_less = node(MX, 1694, MW, 90, "persist_lessons", "e2e fix rounds → vllm-report lessons")
n_push = node(MX, 1820, MW, 96, "push_to_github", "branch + PR (label ready-all)")

# right column — branches
n_sync = node(RX, 300, RW, 84, "has_no_commit — in sync", "no new commits → done", "terminal")
n_noop = node(RX, 700, RW, 84, "skip e2e (is_noop)", "no vllm-ascend code change", "decision")
n_commit = node(RX, 950, RW, 84, "git commit step", "verified.commit advanced", "loop")
n_revert = node(RX, 1130, RW, 84, "revert → UpgradeFailed", "broken changes discarded", "terminal")

# loop column — PR CI feedback
n_ci = node(LX, 1820, LW, 96, "PR CI (ready-all)", "full test suite on the PR", "loop")
n_track = node(LX, 1684, LW, 90, "track_pr_ci (daily step 10)", "deepest exception per failed check", "loop")
n_lsn = node(LX, 1540, LW, 90, "lessons/<date>.json", "failure summaries → fix guidance", "loop")

# ---------- main column arrows ----------
a, b = down(n_ini, 40); arrow(a, b)
arrow((600, 336), (600, 412), label="HasCommit", label_dx=-58, label_dy=-2)
arrow((880, 281), (1150, 342), label="HasNoCommit", label_dx=-96, label_dy=-26)

arrow((600, 578), (600, 640))
arrow((600, 736), (600, 880), label="pass", label_dx=-30, label_dy=-4)
# retry < 3: adapter-qa left -> adapter left
arrow((348, 688), (290, 530), label="retry < 3 (fix mode)", label_dx=-176, label_dy=-10)
arrow((290, 530), (320, 530))

arrow((600, 1268), (600, 1340), label="UpgradeCompleted", label_dx=-70, label_dy=-4)
arrow((600, 1450), (600, 1522))
arrow((600, 1626), (600, 1694))
arrow((600, 1784), (600, 1820))

# ---------- right column ----------
# is_noop: adapter-qa right -> skip-e2e node left
arrow((852, 688), (1150, 742), label="is_noop", label_dx=140, label_dy=22)
# pass: e2e right -> git-commit left
arrow((852, 928), (1150, 992), label="pass", label_dx=140, label_dy=30)
# fail x3: e2e right -> revert left
arrow((852, 928), (1150, 1172), label="fail x3", label_dx=170, label_dy=112)
# fail retry < 3: e2e right -> channel -> adapter right
arrow((852, 928), (CH_FR, 928))
arrow((CH_FR, 928), (CH_FR, 528), label="fail, retry < 3", label_dx=12, label_dy=-4)
arrow((CH_FR, 528), (852, 528))
# next step: git-commit right -> channel -> adapter right
arrow((1670, 992), (CH_NS, 992))
arrow((CH_NS, 992), (CH_NS, 556), label="next step", label_dx=12, label_dy=-4)
arrow((CH_NS, 556), (852, 556))
# UpgradeFailed: revert right -> generate_final_post right
arrow((1670, 1172), (1710, 1172))
arrow((1710, 1172), (1710, 1574), label="UpgradeFailed", label_dx=16, label_dy=-6)
arrow((1710, 1574), (880, 1574))

# ---------- loop column ----------
arrow((880, 1868), (1760, 1868), label="opens PR", label_dx=-330, label_dy=-26)
arrow((1970, 1820), (1970, 1774))
arrow((1970, 1684), (1970, 1630))
# lessons -> adapter (dashed)
arrow((1970, 1540), (1970, 510), dash=True, color="#4f9d6b",
      label="get_adaptation_lessons → hit", label_dx=12, label_dy=-6)
arrow((1970, 510), (852, 510), dash=True, color="#4f9d6b")

img.save("/Users/luweijun/project/2026/github/main2main_flow/docs/images/workflow.png")
print("saved workflow.png", img.size)
