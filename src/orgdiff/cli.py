"""Запуск из командной строки: orgscope старая.docx новая.docx --out out/"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Оргскоп: сравнение оргструктуры и функций в двух редакциях документов")
    ap.add_argument("old", help="документ «до» (docx/pdf/xlsx)")
    ap.add_argument("new", help="документ «после»")
    ap.add_argument("--out", default="out", help="папка для отчётов")
    ap.add_argument("--no-llm", action="store_true", help="только детерминированный анализ, без LLM")
    ap.add_argument("--max-lost", type=int, default=30, help="сколько кандидатов в потерю проверять агентом")
    args = ap.parse_args()

    from .pipeline import run
    from .report import write_html, write_xlsx

    res = run(args.old, args.new, use_llm=False if args.no_llm else None, max_lost=args.max_lost,
              progress=lambda s: print(f"… {s}", flush=True))
    out = Path(args.out)
    x = write_xlsx(res, out / "report.xlsx")
    h = write_html(res, out / "report.html")
    print(f"\n{res.summary}\n")
    for w in res.warnings:
        print(f"⚠ {w}")
    print(f"Находок: {len(res.findings)}. Отчёты: {x}, {h}")


if __name__ == "__main__":
    main()
