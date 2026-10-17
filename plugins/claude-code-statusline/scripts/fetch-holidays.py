#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取中国法定节假日数据并缓存（供 deepseek-price-guard / deepseek-status 判峰谷）。

数据源：https://timor.tech/api/holiday/year/<年>
  社区维护的节假日 API，跟国务院办公厅放假安排同步（通常每年 11 月收录次年安排）。
  返回 {日期: {holiday: true=放假 / false=调休上班, name, ...}}，本脚本只取
  holiday=true 的放假日。普通周末由调用方按 weekday 自行判断，不在此列表；
  调休上班的周末（如 2026-09-20）也不取——官方计价措辞是「空闲时段包括周末全天」。

输出：~/.claude/.dspg_holidays.json
  {"fetched": Unix 时间戳, "source": "timor.tech",
   "years": {"2026": {"2026-01-01": "元旦", ...}, ...}}

被抓年份：今年 + 次年（次年安排发布前能抓到空表；抓失败时保留缓存里的旧数据）。
整体失败（全部年份都没抓到时）保留旧缓存不覆盖，exit 1（计划任务日志可见）。

用法：python fetch-holidays.py                 # 抓今年+明年
      python fetch-holidays.py --years 2026 2027
"""
import os
import sys
import json
import time
import pathlib
import argparse
import datetime
import urllib.request

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

OUT_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_holidays.json"))
API = "https://timor.tech/api/holiday/year/{}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) deepseek-price-guard/1.0"


def fetch_year(year):
    """返回 {date_str: name}；请求/解析失败抛异常。"""
    req = urllib.request.Request(
        API.format(year),
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"API code={data.get('code')} msg={data.get('msg')}")
    days = {}
    for item in (data.get("holiday") or {}).values():
        if item.get("holiday") and item.get("date"):
            days[item["date"]] = item.get("name") or ""
    return days


def main():
    ap = argparse.ArgumentParser(description="抓取法定节假日数据缓存到本地")
    ap.add_argument("--years", nargs="*", type=int,
                    help="要抓的年份（默认今年+明年）")
    args = ap.parse_args()

    this_year = datetime.date.today().year
    years = args.years or [this_year, this_year + 1]

    cache = {"fetched": 0, "source": "timor.tech", "years": {}}
    try:  # 读旧缓存，按年份合并（某年抓失败时保留旧数据）
        cache = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        cache.setdefault("years", {})
    except Exception:
        pass

    failed = []
    for y in years:
        try:
            days = fetch_year(y)
        except Exception as e:
            failed.append(f"{y}: {e}")
            continue
        if days:
            cache["years"][str(y)] = days
            print(f"{y}: {len(days)} 天")
        else:  # 该年安排尚未发布（如 11 月前的次年）：正常，不算失败
            print(f"{y}: 暂无数据（放假安排未发布？）", file=sys.stderr)

    if failed:
        for f in failed:
            print(f"抓取失败 {f}", file=sys.stderr)
        if not cache["years"]:
            return 1

    cache["fetched"] = int(time.time())
    OUT_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"已写入 {OUT_FILE}（{len(cache['years'])} 个年份，"
          f"共 {sum(len(v) for v in cache['years'].values())} 天）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
