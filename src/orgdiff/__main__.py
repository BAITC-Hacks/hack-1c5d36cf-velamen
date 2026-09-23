import argparse
import asyncio
import sys
from pathlib import Path

from .chunking import make_chunks
from .config import load_settings
from .ingest import deduplicate, parse_cached
from .models import InputFile
from .storage import write_json


def input_files(paths: list[str]) -> list[InputFile]:
    return deduplicate([InputFile(name=Path(path).name, content=Path(path).read_bytes()) for path in paths])


def main() -> int:
    # Windows pipes may default to a legacy encoding even for Russian output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Сравнение организационной структуры и функций")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Прочитать документы без вызова AI")
    inspect.add_argument("--input", nargs="+", required=True)
    inspect.add_argument("--out", type=Path, required=True)
    commands.add_parser("doctor", help="Проверить доступ к OpenAI (платные запросы)")
    analyze = commands.add_parser("analyze", help="Извлечь факты и сравнить наборы документов")
    analyze.add_argument("--before", nargs="+", required=True)
    analyze.add_argument("--after", nargs="+", required=True)
    analyze.add_argument("--until", choices=["extract", "compare"], default="compare")
    analyze.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = load_settings()
        if args.command == "doctor":
            from .llm import Runtime, doctor, use_runtime
            async def check():
                run = Runtime(settings)
                with use_runtime(run):
                    try:
                        await doctor()
                    finally:
                        await run.close()
            asyncio.run(check())
            return 0
        if args.command == "analyze":
            from .pipeline import analyze as run_analysis
            asyncio.run(run_analysis(input_files(args.before), input_files(args.after), args.out,
                                     until=args.until, settings=settings,
                                     progress=lambda event: print(f"{event.stage}: {event.completed}/{event.total}", flush=True)))
            print(f"Контрольная точка {args.until}: {args.out.resolve()}; состояние и ограничения — в run.json")
            return 0
        documents = [parse_cached(file, settings.artifact_dir / "cache") for file in input_files(args.input)]
        chunks = [chunk for doc in documents for chunk in make_chunks(doc, settings.body_chars, settings.context_chars)]
        write_json(args.out / "documents.json", documents)
        write_json(args.out / "chunks.json", chunks)
        print(f"Документов: {len(documents)}; блоков: {sum(len(d.blocks) for d in documents)}; фрагментов: {len(chunks)}")
        for doc in documents:
            for warning in doc.warnings:
                print(f"Предупреждение: {warning}")
        print(f"Результат: {args.out.resolve()}")
        return 0
    except Exception as exc:
        from .llm import error_ru
        print(f"Ошибка: {error_ru(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
