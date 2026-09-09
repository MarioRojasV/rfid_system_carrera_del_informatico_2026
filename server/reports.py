"""Agregaciones e informes exportables sobre corredores/resultados.

Separado de main.py (que ya pasaba las 1000 líneas) porque esto es
puramente cómputo + presentación: build_report_data cruza runners y
results en memoria (mismo criterio que getRunners() en el frontend —
un corredor cuenta como finalizado si su resultado tiene timestamp) y
arma un único dict de agregados; render_report_xlsx y render_report_pdf
lo convierten a los dos formatos exportables. Los tres se alimentan del
mismo dict para que el resumen en pantalla y los dos exports nunca
queden desincronizados entre sí.
"""

import io
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Optional

import openpyxl
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

REPORT_GENDERS = [("M", "Hombres"), ("F", "Mujeres")]

# Mismo orden y alcance que SUBCATEGORIES en el frontend
# (administrador/src/utils/category.js): las 4 franjas del 10K que
# tienen podio real. "informatico" es un valor posible en la data (ver
# _SUBCATEGORY_BY_LABEL en main.py) pero el frontend la excluyó como
# subcategoría premiada, así que el podio de este informe respeta la
# misma exclusión — sigue apareciendo en by_subcategory (conteo general)
# pero no en la sección de podio.
PODIUM_SUBCATEGORIES = [
    ("veterano", "Veterano"),
    ("mayor", "Mayor"),
    ("master", "Master"),
    ("master_b", "Máster B"),
]

SUBCATEGORY_LABELS = dict(PODIUM_SUBCATEGORIES)
SUBCATEGORY_LABELS["informatico"] = "Informático"

NO_TALLA_LABEL = "Sin talla registrada"


def _format_seconds(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    return str(timedelta(seconds=int(seconds)))


def _elapsed_stats(elapsed_seconds_list: list) -> dict:
    values = [v for v in elapsed_seconds_list if v is not None]
    if not values:
        return {"count": 0, "best": None, "worst": None, "avg": None, "median": None}
    return {
        "count": len(values),
        "best": _format_seconds(min(values)),
        "worst": _format_seconds(max(values)),
        "avg": _format_seconds(statistics.mean(values)),
        "median": _format_seconds(statistics.median(values)),
    }


def _empty_bucket() -> dict:
    return {"total": 0, "finished": 0, "pending": 0}


def build_report_data(runners: list[dict], results: list[dict]) -> dict:
    """Cruza runners + results y devuelve todos los agregados que
    alimentan /reports/summary y los dos exports. `runners`/`results` ya
    deben venir filtrados (ver _fetch_report_data en main.py) — esta
    función no vuelve a tocar la base de datos."""

    # Un resultado sin timestamp no debería llegar hasta acá (todo lo
    # que hay en la colección "results" tiene uno), pero se filtra
    # igual por las dudas y para que el criterio de "finalizado" quede
    # explícito en un solo lugar.
    results_by_runner_id = {
        r["runner_id"]: r for r in results if r.get("timestamp") is not None
    }

    total = len(runners)
    finished = sum(1 for r in runners if r["runner_id"] in results_by_runner_id)
    pending = total - finished

    by_category = defaultdict(_empty_bucket)
    by_gender = defaultdict(_empty_bucket)
    by_category_gender = defaultdict(_empty_bucket)
    by_subcategory = defaultdict(_empty_bucket)  # solo corredores de 10K
    shirt_size_counts = Counter()
    shirt_delivered = 0
    kit_delivered = 0
    tag_assigned = 0
    special_notes = []
    absentees = []

    for runner in runners:
        runner_id = runner["runner_id"]
        category = runner.get("category") or "—"
        gender = runner.get("gender") or "—"
        is_finished = runner_id in results_by_runner_id

        by_category[category]["total"] += 1
        by_category[category]["finished" if is_finished else "pending"] += 1

        by_gender[gender]["total"] += 1
        by_gender[gender]["finished" if is_finished else "pending"] += 1

        by_category_gender[(category, gender)]["total"] += 1
        by_category_gender[(category, gender)]["finished" if is_finished else "pending"] += 1

        if category == "10K":
            subcategory = runner.get("subcategory") or "—"
            by_subcategory[subcategory]["total"] += 1
            by_subcategory[subcategory]["finished" if is_finished else "pending"] += 1

        shirt_size_counts[runner.get("shirt_size") or NO_TALLA_LABEL] += 1
        if runner.get("shirt_delivered"):
            shirt_delivered += 1
        if runner.get("kit_delivered"):
            kit_delivered += 1
        if runner.get("tag_id"):
            tag_assigned += 1

        note = (runner.get("special_note") or "").strip()
        if note:
            special_notes.append(
                {
                    "runner_id": runner_id,
                    "name": runner.get("name"),
                    "category": category,
                    "note": note,
                }
            )

        if not is_finished:
            absentees.append(
                {
                    "runner_id": runner_id,
                    "name": runner.get("name"),
                    "category": category,
                    "subcategory": runner.get("subcategory") or "",
                    "gender": gender,
                    "tag_id": runner.get("tag_id"),
                }
            )

    all_elapsed = [r.get("elapsed_seconds") for r in results_by_runner_id.values()]
    times_overall = _elapsed_stats(all_elapsed)
    times_by_category = {
        category: _elapsed_stats(
            [
                r.get("elapsed_seconds")
                for r in results_by_runner_id.values()
                if r.get("category") == category
            ]
        )
        for category in by_category
    }

    corrected_count = sum(1 for r in results_by_runner_id.values() if r.get("corrected"))
    source_counts = Counter(
        r.get("source") or "desconocido" for r in results_by_runner_id.values()
    )

    # Podio real: top 3 por tiempo de cada combinación subcategoría ×
    # género premiada del 10K (ver PODIUM_SUBCATEGORIES arriba).
    podium = {}
    for subcat_value, subcat_label in PODIUM_SUBCATEGORIES:
        for gender_value, gender_label in REPORT_GENDERS:
            group_results = [
                r
                for r in results_by_runner_id.values()
                if r.get("category") == "10K"
                and r.get("subcategory") == subcat_value
                and r.get("gender") == gender_value
                and r.get("elapsed_seconds") is not None
            ]
            group_results.sort(key=lambda r: r["elapsed_seconds"])
            podium[f"{subcat_value}_{gender_value}"] = {
                "label": f"{subcat_label} · {gender_label}",
                "top3": [
                    {
                        "runner_id": r["runner_id"],
                        "name": r.get("name"),
                        "elapsed_display": r.get("elapsed_display"),
                    }
                    for r in group_results[:3]
                ],
            }

    return {
        "totals": {"total": total, "finished": finished, "pending": pending},
        "by_category": dict(by_category),
        "by_gender": dict(by_gender),
        "by_category_gender": {
            f"{c}_{g}": v for (c, g), v in by_category_gender.items()
        },
        "by_subcategory": {
            key: {**value, "label": SUBCATEGORY_LABELS.get(key, key)}
            for key, value in by_subcategory.items()
        },
        "shirts": {
            "by_size": dict(shirt_size_counts),
            "delivered": shirt_delivered,
            "pending": total - shirt_delivered,
        },
        "kits": {"delivered": kit_delivered, "pending": total - kit_delivered},
        "tags": {"assigned": tag_assigned, "unassigned": total - tag_assigned},
        "special_notes": special_notes,
        "absentees": absentees,
        "times": {"overall": times_overall, "by_category": times_by_category},
        "results_quality": {
            "corrected": corrected_count,
            "by_source": dict(source_counts),
        },
        "podium": podium,
    }


# ---------------------------------------------------------------------------
# Export a .xlsx
# ---------------------------------------------------------------------------

HEADER_FONT = Font(bold=True)


def _write_table(ws, start_row: int, headers: list[str], rows: list[list]) -> int:
    """Escribe una tabla simple (headers en negrita + filas) a partir de
    start_row. Devuelve la fila siguiente a la última escrita, para
    encadenar varias tablas en la misma hoja."""
    ws.append([])  # fila en blanco de separación, se ignora si start_row == 1
    ws.append(headers)
    for cell in ws[ws.max_row]:
        cell.font = HEADER_FONT
    for row in rows:
        ws.append(row)
    return ws.max_row + 1


def render_report_xlsx(data: dict, runners: list[dict], results: list[dict]) -> bytes:
    wb = openpyxl.Workbook()

    # --- Resumen ---
    ws = wb.active
    ws.title = "Resumen"
    ws.append(["Informe · Carrera del Informático 2026"])
    ws["A1"].font = Font(bold=True, size=14)

    totals = data["totals"]
    _write_table(
        ws,
        ws.max_row,
        ["Total inscritos", "Finalizados", "Pendientes"],
        [[totals["total"], totals["finished"], totals["pending"]]],
    )

    _write_table(
        ws,
        ws.max_row,
        ["Categoría", "Total", "Finalizados", "Pendientes"],
        [
            [category, v["total"], v["finished"], v["pending"]]
            for category, v in data["by_category"].items()
        ],
    )

    _write_table(
        ws,
        ws.max_row,
        ["Género", "Total", "Finalizados", "Pendientes"],
        [
            [gender, v["total"], v["finished"], v["pending"]]
            for gender, v in data["by_gender"].items()
        ],
    )

    _write_table(
        ws,
        ws.max_row,
        ["Subcategoría (10K)", "Total", "Finalizados", "Pendientes"],
        [
            [v["label"], v["total"], v["finished"], v["pending"]]
            for v in data["by_subcategory"].values()
        ],
    )

    _write_table(
        ws,
        ws.max_row,
        ["Talla de camiseta", "Cantidad"],
        [[size, count] for size, count in data["shirts"]["by_size"].items()],
    )
    _write_table(
        ws,
        ws.max_row,
        ["Camisetas entregadas", "Camisetas pendientes", "Kits entregados", "Kits pendientes"],
        [
            [
                data["shirts"]["delivered"],
                data["shirts"]["pending"],
                data["kits"]["delivered"],
                data["kits"]["pending"],
            ]
        ],
    )

    _write_table(
        ws,
        ws.max_row,
        ["Tags RFID asignados", "Tags RFID sin asignar"],
        [[data["tags"]["assigned"], data["tags"]["unassigned"]]],
    )

    _write_table(
        ws,
        ws.max_row,
        ["Resultados corregidos a mano"],
        [[data["results_quality"]["corrected"]]],
    )
    _write_table(
        ws,
        ws.max_row,
        ["Fuente del resultado", "Cantidad"],
        [[source, count] for source, count in data["results_quality"]["by_source"].items()],
    )

    overall = data["times"]["overall"]
    _write_table(
        ws,
        ws.max_row,
        ["Tiempos (todas las categorías)", "Mejor", "Peor", "Promedio", "Mediana"],
        [["General", overall["best"], overall["worst"], overall["avg"], overall["median"]]]
        + [
            [category, v["best"], v["worst"], v["avg"], v["median"]]
            for category, v in data["times"]["by_category"].items()
        ],
    )

    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 40)

    # --- Podio ---
    ws_podium = wb.create_sheet("Podio 10K")
    ws_podium.append(["Categoría", "Puesto", "Corredor", "Tiempo"])
    for cell in ws_podium[1]:
        cell.font = HEADER_FONT
    for group in data["podium"].values():
        if not group["top3"]:
            ws_podium.append([group["label"], "—", "Sin resultados", ""])
            continue
        for position, entry in enumerate(group["top3"], start=1):
            ws_podium.append([group["label"], position, entry["name"], entry["elapsed_display"]])

    # --- Corredores (listado completo) ---
    ws_runners = wb.create_sheet("Corredores")
    runner_headers = [
        "runner_id", "nombre", "género", "categoría", "subcategoría",
        "talla", "tag_id", "camiseta entregada", "kit entregado", "nota especial",
    ]
    ws_runners.append(runner_headers)
    for cell in ws_runners[1]:
        cell.font = HEADER_FONT
    for r in runners:
        ws_runners.append(
            [
                r.get("runner_id"), r.get("name"), r.get("gender"),
                r.get("category"), r.get("subcategory"), r.get("shirt_size"),
                r.get("tag_id"), bool(r.get("shirt_delivered")),
                bool(r.get("kit_delivered")), r.get("special_note") or "",
            ]
        )

    # --- Resultados ---
    ws_results = wb.create_sheet("Resultados")
    result_headers = [
        "runner_id", "nombre", "categoría", "subcategoría", "género",
        "tiempo", "fuente", "corregido",
    ]
    ws_results.append(result_headers)
    for cell in ws_results[1]:
        cell.font = HEADER_FONT
    for r in results:
        if r.get("timestamp") is None:
            continue
        ws_results.append(
            [
                r.get("runner_id"), r.get("name"), r.get("category"),
                r.get("subcategory"), r.get("gender"), r.get("elapsed_display"),
                r.get("source"), bool(r.get("corrected")),
            ]
        )

    # --- Ausentes ---
    ws_absent = wb.create_sheet("Ausentes")
    absent_headers = ["runner_id", "nombre", "categoría", "subcategoría", "género", "tag_id"]
    ws_absent.append(absent_headers)
    for cell in ws_absent[1]:
        cell.font = HEADER_FONT
    for a in data["absentees"]:
        ws_absent.append(
            [a["runner_id"], a["name"], a["category"], a["subcategory"], a["gender"], a["tag_id"]]
        )

    # --- Notas especiales ---
    ws_notes = wb.create_sheet("Notas especiales")
    ws_notes.append(["runner_id", "nombre", "categoría", "nota"])
    for cell in ws_notes[1]:
        cell.font = HEADER_FONT
    for n in data["special_notes"]:
        ws_notes.append([n["runner_id"], n["name"], n["category"], n["note"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Export a .pdf
# ---------------------------------------------------------------------------

_TABLE_STYLE = TableStyle(
    [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2933")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd2d9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fa")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
)


def _pdf_table(headers: list[str], rows: list[list], col_widths=None) -> Table:
    data = [headers] + [[str(cell) if cell is not None else "" for cell in row] for row in rows]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return table


def render_report_pdf(data: dict, generated_at_label: str) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Informe · Carrera del Informático 2026", styles["Title"]))
    story.append(Paragraph(f"Generado: {generated_at_label}", styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))

    totals = data["totals"]
    story.append(Paragraph("Resumen general", styles["Heading2"]))
    story.append(
        _pdf_table(
            ["Total inscritos", "Finalizados", "Pendientes"],
            [[totals["total"], totals["finished"], totals["pending"]]],
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Por categoría", styles["Heading2"]))
    story.append(
        _pdf_table(
            ["Categoría", "Total", "Finalizados", "Pendientes"],
            [[c, v["total"], v["finished"], v["pending"]] for c, v in data["by_category"].items()],
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Por género", styles["Heading2"]))
    story.append(
        _pdf_table(
            ["Género", "Total", "Finalizados", "Pendientes"],
            [[g, v["total"], v["finished"], v["pending"]] for g, v in data["by_gender"].items()],
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    if data["by_subcategory"]:
        story.append(Paragraph("Subcategorías del 10K", styles["Heading2"]))
        story.append(
            _pdf_table(
                ["Subcategoría", "Total", "Finalizados", "Pendientes"],
                [[v["label"], v["total"], v["finished"], v["pending"]] for v in data["by_subcategory"].values()],
            )
        )
        story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Camisetas y kits", styles["Heading2"]))
    story.append(
        _pdf_table(
            ["Talla", "Cantidad"],
            [[size, count] for size, count in data["shirts"]["by_size"].items()],
        )
    )
    story.append(Spacer(1, 0.2 * cm))
    story.append(
        _pdf_table(
            ["Camisetas entregadas", "Camisetas pendientes", "Kits entregados", "Kits pendientes"],
            [[data["shirts"]["delivered"], data["shirts"]["pending"], data["kits"]["delivered"], data["kits"]["pending"]]],
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Tags RFID", styles["Heading2"]))
    story.append(
        _pdf_table(
            ["Asignados", "Sin asignar"],
            [[data["tags"]["assigned"], data["tags"]["unassigned"]]],
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Calidad de los resultados", styles["Heading2"]))
    story.append(
        Paragraph(
            f"Resultados corregidos a mano: {data['results_quality']['corrected']}",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 0.2 * cm))
    story.append(
        _pdf_table(
            ["Fuente del resultado", "Cantidad"],
            [[source, count] for source, count in data["results_quality"]["by_source"].items()],
        )
    )
    story.append(Spacer(1, 0.4 * cm))

    overall = data["times"]["overall"]
    story.append(Paragraph("Tiempos", styles["Heading2"]))
    story.append(
        _pdf_table(
            ["Categoría", "Mejor", "Peor", "Promedio", "Mediana"],
            [["General", overall["best"], overall["worst"], overall["avg"], overall["median"]]]
            + [
                [c, v["best"], v["worst"], v["avg"], v["median"]]
                for c, v in data["times"]["by_category"].items()
            ],
        )
    )

    story.append(PageBreak())
    story.append(Paragraph("Podio 10K (top 3 por subcategoría · género)", styles["Heading2"]))
    podium_rows = []
    for group in data["podium"].values():
        if not group["top3"]:
            podium_rows.append([group["label"], "—", "Sin resultados", ""])
            continue
        for position, entry in enumerate(group["top3"], start=1):
            podium_rows.append([group["label"], position, entry["name"], entry["elapsed_display"]])
    story.append(_pdf_table(["Categoría", "Puesto", "Corredor", "Tiempo"], podium_rows))

    if data["absentees"]:
        story.append(PageBreak())
        story.append(
            Paragraph(f"Ausentes ({len(data['absentees'])})", styles["Heading2"])
        )
        story.append(
            _pdf_table(
                ["runner_id", "Nombre", "Categoría", "Subcategoría", "Género"],
                [
                    [a["runner_id"], a["name"], a["category"], a["subcategory"], a["gender"]]
                    for a in data["absentees"]
                ],
            )
        )

    if data["special_notes"]:
        story.append(PageBreak())
        story.append(
            Paragraph(f"Notas especiales ({len(data['special_notes'])})", styles["Heading2"])
        )
        story.append(
            _pdf_table(
                ["runner_id", "Nombre", "Categoría", "Nota"],
                [[n["runner_id"], n["name"], n["category"], n["note"]] for n in data["special_notes"]],
            )
        )

    doc.build(story)
    return buffer.getvalue()
