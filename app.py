"""Веб-интерфейс: загрузка двух редакций → анализ → просмотр и выгрузка отчёта.

Запуск: uv run streamlit run app.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from orgdiff import views as V
from orgdiff.pipeline import run
from orgdiff.report import sheets, write_html, write_xlsx

load_dotenv()
SAMPLE = Path(__file__).parent / "data" / "sample"

st.set_page_config(page_title=V.APP_NAME, page_icon="🔍", layout="wide")
st.title(f"🔍 {V.APP_NAME}")
st.caption(V.APP_TAGLINE.capitalize() + ". " + V.DISCLAIMER)

c1, c2 = st.columns(2)
old = c1.file_uploader("Было — документы до реорганизации", type=["docx", "pdf", "xlsx"])
new = c2.file_uploader("Стало — документы после реорганизации", type=["docx", "pdf", "xlsx"])
use_sample = st.checkbox("Взять тестовый набор (Положение о внутреннем аудите, редакции 8 и 9)",
                         value=not (old and new))
has_key = bool(os.environ.get("OPENAI_API_KEY"))
with st.expander("Настройки анализа"):
    use_llm = st.toggle("Смысловой анализ с LLM: проверка потерь агентом, дубли, конфликты интересов",
                        value=has_key, disabled=not has_key,
                        help=None if has_key else "Добавьте OPENAI_API_KEY в файл .env")
    max_lost = st.slider("Сколько ненайденных функций проверять агентом", 0, 60, 30, disabled=not use_llm)


def _save(upload) -> Path:
    tmp = Path(tempfile.mkdtemp()) / upload.name
    tmp.write_bytes(upload.getvalue())
    return tmp


def _table(rows: list[dict], empty: str = "Ничего не найдено") -> None:
    st.html(f"<style>{V.CSS}</style><div class=og>{V.table_html(rows, empty)}</div>")


if st.button("Сравнить", type="primary"):
    if use_sample:
        pa, pb = SAMPLE / "r8.docx", SAMPLE / "r9.docx"
    elif old and new:
        pa, pb = _save(old), _save(new)
    else:
        st.error("Загрузите оба документа или выберите тестовый набор.")
        st.stop()
    with st.status("Анализ…", expanded=True) as status:
        res = run(pa, pb, use_llm=use_llm, max_lost=max_lost, progress=st.write)
        status.update(label="Готово", state="complete", expanded=False)
    out = Path(tempfile.mkdtemp())
    st.session_state["res"] = res
    st.session_state["xlsx"] = write_xlsx(res, out / "report.xlsx").read_bytes()
    st.session_state["html"] = write_html(res, out / "report.html").read_bytes()

res = st.session_state.get("res")
if res:
    ms = V.metrics(res)
    for chunk in (ms[:3], ms[3:]):
        for col, (label, value) in zip(st.columns(3), chunk):
            col.metric(label, value)
    for w in res.warnings:
        st.info(w)

    names = ["Вывод"] + [t for t, _, _ in sheets(res)]
    tabs = st.tabs(names)
    with tabs[0]:
        for title, items in V.conclusion(res):
            st.subheader(title, divider="gray")
            # «— …» — подпункт предыдущего пункта: вложенный список
            st.markdown("\n".join(f"    - {i[2:]}" if i.startswith("— ") else f"- {i}" for i in items))
        d1, d2, _ = st.columns([1, 1, 3])
        d1.download_button("⬇ Excel", st.session_state["xlsx"], "orgscope-report.xlsx", use_container_width=True)
        d2.download_button("⬇ HTML-отчёт", st.session_state["html"], "orgscope-report.html",
                           use_container_width=True)
    for tab, (title, desc, rows) in zip(tabs[1:], sheets(res)):
        with tab:
            st.caption(desc)
            if title == "Все функции":
                statuses = list(dict.fromkeys(r["Что произошло"] for r in rows))
                pick = st.multiselect("Показать", statuses,
                                      default=[s for s in statuses if s != V.FUNC_STATUS["retained"]])
                rows = [r for r in rows if r["Что произошло"] in pick]
            _table(rows)
