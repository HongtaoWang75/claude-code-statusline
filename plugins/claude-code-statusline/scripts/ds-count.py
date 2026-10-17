#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 DeepSeek 官方 tokenizer 本地数 token。

DeepSeek **没有**官方 pip 包（没有 tiktoken 那种东西），官方只发布
tokenizer.json 文件。本脚本从 ModelScope 取 deepseek-ai/DeepSeek-V4.1-Flash
的 tokenizer.json（实测 HF 直连超时、hf-mirror 时通时不通，ModelScope 稳定），
用本机已装的 `tokenizers` 库加载，不需要 transformers。

何时该用：发请求**之前**估算这段文本大概烧多少 token。
何时不该用：想知道"这个会话花了多少" —— 那用 transcript / statusLine 里的
真实 usage（带真实的 cache hit/miss 拆分），比本地估算准得多。

用法：
    python ds-count.py "一段文本"
    python ds-count.py -f notes.md              # 从文件读
    echo "文本" | python ds-count.py            # 从 stdin 读
    python ds-count.py -f notes.md --json       # 机器可读
"""
import os
import sys
import json
import argparse
import pathlib
import urllib.request

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKENIZER_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_tokenizer.json"))
TOKENIZER_URL = (
    "https://modelscope.cn/models/deepseek-ai/"
    "DeepSeek-V4.1-Flash/resolve/master/tokenizer.json"
)


def ensure_tokenizer():
    """tokenizer.json 不在本地就下（6 MB 左右，ModelScope 实测 1~2 秒）。"""
    if TOKENIZER_FILE.exists() and TOKENIZER_FILE.stat().st_size > 0:
        return
    sys.stderr.write("[ds-count] 首次使用，从 ModelScope 下载 tokenizer...\n")
    req = urllib.request.Request(TOKENIZER_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    TOKENIZER_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENIZER_FILE.write_bytes(data)
    sys.stderr.write(f"[ds-count] 已存到 {TOKENIZER_FILE}（{len(data)/1048576:.1f} MB）\n")


def load_tokenizer():
    ensure_tokenizer()
    try:
        from tokenizers import Tokenizer
    except ImportError:
        sys.stderr.write(
            "[ds-count] 缺少 tokenizers 库。装它（不需要 transformers）：\n"
            "    pip install tokenizers\n"
        )
        sys.exit(3)
    return Tokenizer.from_file(str(TOKENIZER_FILE))


def read_input(args):
    """按优先级取输入：位置参数 > -f 文件 > stdin 管道。"""
    if args.text:
        return " ".join(args.text), "<命令行参数>"
    if args.file:
        p = pathlib.Path(args.file)
        try:
            return p.read_text(encoding="utf-8"), str(p)
        except Exception as e:
            sys.stderr.write(f"[ds-count] 读不了 {p}: {e}\n")
            sys.exit(2)
    if sys.stdin is not None and not sys.stdin.isatty():
        try:
            return sys.stdin.read(), "<stdin>"
        except Exception:
            pass
    sys.stderr.write(__doc__)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description="用 DeepSeek 官方 tokenizer 数 token")
    ap.add_argument("text", nargs="*", help="要数的文本")
    ap.add_argument("-f", "--file", help="从文件读（UTF-8）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--chars", action="store_true", help="附带字符数")
    args = ap.parse_args()

    content, source = read_input(args)
    tk = load_tokenizer()
    ids = tk.encode(content).ids
    n = len(ids)

    if args.json:
        out = {
            "tokens": n,
            "source": source,
            "tokenizer": "deepseek-ai/DeepSeek-V4.1-Flash",
        }
        if args.chars:
            out["characters"] = len(content)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if args.chars:
        print(f"{n} tokens  ({len(content)} 字符, {n / max(1, len(content)):.2f} tokens/字符)  [{source}]")
    else:
        print(f"{n} tokens  [{source}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
