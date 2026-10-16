#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 峰谷时段 / 定价 指纹自检。

为什么需要它：DeepSeek **没有任何定价接口**（实测 2026-09-13：/models 只返回
模型列表、/user/balance 只返回余额，/pricing /price /peak 一律 404），峰谷时段
只能从定价文档页拿。而 deepseek-price-guard.py 里的 PEAK_RANGES 是硬编码的。

硬编码本身不是问题——时段一年也未必变一次；怕的是它**悄悄变了**而拦截逻辑
还在按旧时段工作：该拦的没拦（高峰烧钱），或者大半夜把你拦下来。

本脚本每周抓一次定价页，把"峰谷时段句 + 全部价格数字"做成指纹，与上次比对，
变了就写状态文件 + 弹 Toast，提醒你去改 PEAK_RANGES。

用法：
    python price-watch.py             # 抓取比对（计划任务每周跑这个）
    python price-watch.py --show      # 只打印当前指纹，不写状态
    python price-watch.py --reset     # 把当前抓取结果设为基线
    python price-watch.py --install-task   # 注册每周一 10:00 的计划任务
"""
import os
import re
import sys
import json
import time
import base64
import hashlib
import argparse
import pathlib
import datetime
import subprocess
import urllib.request

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
STATE_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_price_check.json"))
TASK_NAME = "DSPG Price Watch"
TZ = datetime.timezone(datetime.timedelta(hours=8))
AUMID = "DeepSeekPriceGuard"  # 与 guard 注册的 Toast 显示名一致

# 高峰时段当前硬编码值（与 deepseek-price-guard.py / deepseek-status.py 同步），
# 仅用于在告警里告诉用户"你现在的代码写的是什么"
HARDCODED_PEAK = "工作日 09:00-12:00 / 14:00-18:00（北京时间）= UTC 周一至周五 01:00-04:00 / 06:00-10:00"


def fetch_page(timeout=25):
    req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def strip_html(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&#x27;", "'").replace("&#39;", "'").replace("&quot;", '"')
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text)


def extract(html):
    """抽出与计费相关的关键句 + 全部价格数字，作为指纹素材。"""
    text = strip_html(html)
    sentences = []
    for pat in (r"[^.]*Peak hours[^.]*\.", r"[^.]*[Oo]ff-peak rates[^.]*\."):
        m = re.search(pat, text)
        if m:
            sentences.append(re.sub(r"\s+", " ", m.group(0)).strip())
    amounts = sorted(set(re.findall(r"\$\d+(?:\.\d+)?", text)))
    return sentences, amounts


def fingerprint(sentences, amounts):
    blob = json.dumps({"s": sentences, "a": amounts}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        sys.stderr.write(f"[price-watch] 状态写不进去: {e}\n")


def toast(title, message):
    """Windows Toast 纯展示。

    注：未打包应用的 Toast **按钮点击不会触发**（实测多次），所以这里只发不可
    交互的纯文本通知；要采取行动看状态栏或直接跑脚本。用 -EncodedCommand 传参
    彻底绕开中文/引号转义问题。
    """
    ps = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName("text")
$n.Item(0).AppendChild($t.CreateTextNode("{title}")) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode("{message}")) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{AUMID}").Show($toast)
'''
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=25,
        )
    except Exception:
        pass


def install_task():
    python = sys.executable
    script = str(pathlib.Path(__file__).resolve())
    tr = f'"{python}" "{script}"'
    r = subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
         "/sc", "weekly", "/d", "MON", "/st", "10:00", "/f"],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    print(r.stdout.strip() or r.stderr.strip())
    print(r.returncode == 0 and "计划任务已注册（每周一 10:00）" or "注册失败")


def main():
    ap = argparse.ArgumentParser(description="DeepSeek 定价指纹自检")
    ap.add_argument("--show", action="store_true", help="只打印当前抓取结果")
    ap.add_argument("--reset", action="store_true", help="把当前结果设为基线")
    ap.add_argument("--install-task", action="store_true", help="注册每周计划任务")
    ap.add_argument("--no-toast", action="store_true", help="变化时不弹通知")
    args = ap.parse_args()

    if args.install_task:
        install_task()
        return 0

    try:
        html = fetch_page()
        sentences, amounts = extract(html)
    except Exception as e:
        # 抓取失败不算"变化"，也不能污染基线
        sys.stderr.write(f"[price-watch] 抓取失败: {type(e).__name__}: {e}\n")
        st = load_state()
        st["last_error"] = f"{type(e).__name__}: {e}"
        st["last_error_at"] = int(time.time())
        save_state(st)
        return 1

    if not sentences:
        sys.stderr.write("[price-watch] 页面结构可能变了：没找到峰谷时段句。\n")
        return 1

    fp = fingerprint(sentences, amounts)
    now = datetime.datetime.now(TZ)
    old = load_state()
    baseline = old.get("fingerprint")

    if args.show:
        print("峰谷关键句:")
        for s in sentences:
            print("  *", s)
        print(f"价格数字({len(amounts)} 个): {', '.join(amounts)}")
        print(f"指纹: {fp}")
        print(f"基线: {baseline or '（未建立）'}")
        return 0

    changed = bool(baseline) and baseline != fp
    state = {
        "checked_at": int(time.time()),
        "checked_at_str": now.strftime("%Y-%m-%d %H:%M"),
        "sentences": sentences,
        "amounts": amounts,
        "fingerprint": fp,
        "previous_fingerprint": baseline,
        "changed": changed,
        "hardcoded_peak": HARDCODED_PEAK,
        "source": PRICING_URL,
    }
    if args.reset:
        state["changed"] = False
        state["previous_fingerprint"] = baseline
        state["reset_at"] = now.strftime("%Y-%m-%d %H:%M")
    save_state(state)

    if args.reset:
        print(f"基线已重置 -> {fp[:16]}…")
        return 0

    if changed and not args.no_toast:
        toast(
            "DeepSeek 定价页变了",
            "峰谷时段或价格与上次不同，请检查 PEAK_RANGES。"
            f"当前抓到的时段句：{sentences[0][:90]}",
        )

    print(f"{now.strftime('%Y-%m-%d %H:%M')} 已检查 | 指纹 {fp[:16]}… | "
          + ("⚠ 与基线不同，已告警" if changed else "无变化"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
