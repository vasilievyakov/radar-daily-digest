"""Command line entry point: `python -m radar.cli`.

Everything a human does with this project by hand lives here — priced dry
runs, collection without a model, the corpus verdict, a trend recount and an
environment check. The daily run has its own scheduler; this is the console
for the evening the corpus is built.

Output is Russian because a person reads it; code and identifiers are English.
"""

from __future__ import annotations

import argparse
import inspect
import os
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

from radar.backfill import (
    BackfillOptions,
    BackfillReport,
    BudgetNotSet,
    find_unfinished_run,
    refresh_trends,
    resolve_limit,
    run_backfill,
    select_sources,
)
from radar.collect import collect_all, summarize
from radar.config import ConfigError, ThemeConfig
from radar.db import corpus_readiness, dump_state, init_db
from radar.fetch import Fetcher
from radar.journal import Journal
from radar.llm import API_KEY_ENV
from radar.llm_cli import CLI_BIN_ENV
from radar.runlog import RunLog, new_run_id

DEFAULT_DB = "data/radar.db"
DEFAULT_CONFIG = "config/ai-tools.yaml"
DEFAULT_CACHE = "cache"
DEFAULT_LOGS = "logs"

RULE = "─" * 72


# -- shared plumbing --------------------------------------------------


def _load(args: argparse.Namespace) -> tuple[ThemeConfig, sqlite3.Connection]:
    config = ThemeConfig.load(args.config)
    conn = init_db(args.db)
    return config, conn


def _fetcher(config: ThemeConfig, cache_root: str) -> Fetcher:
    collection = config.collection
    return Fetcher(
        cache_root=cache_root,
        timeout=float(collection.get("timeout_seconds", 30)),
        max_retries=int(collection.get("max_retries", 2)),
        polite_delay=float(collection.get("polite_delay_seconds", 1.0)),
    )


def _priorities(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    return [int(part) for part in str(raw).replace(" ", "").split(",") if part]


def _ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part for part in str(raw).replace(" ", "").split(",") if part]


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes} мин {secs} с" if minutes else f"{secs} с"


def _build_enricher(config: ThemeConfig, args: argparse.Namespace) -> Any:
    """Bind `radar.enrich.LlmEnricher` by parameter name.

    Stage 4 is written in parallel with this file, so its constructor is not
    known here. Backfill itself depends only on the `Enricher` protocol; this
    is the one place that touches the implementation, and it offers what it
    has rather than guessing a positional signature.

    Neither the budget nor the run log is handed over: backfill owns the
    ceiling (two counters would double-count), and the sqlite connection
    behind the run log belongs to the calling thread.
    """
    from radar.cache import ModelCache
    from radar.enrich import LlmEnricher
    from radar.llm_cli import make_backend

    cache = ModelCache(args.cache)
    offered: dict[str, Any] = {
        "config": config,
        "cache": cache,
        "model_cache": cache,
        "cache_root": args.cache,
    }
    params = inspect.signature(LlmEnricher).parameters
    if "client" in params or "backend" in params:
        client = make_backend(
            config,
            cache=cache,
            timeout=float(config.section("llm").get("timeout_seconds", 180)),
            max_schema_retries=int(config.section("llm").get("max_schema_retries", 2)),
        )
        offered["client"] = client
        offered["backend"] = client
    kwargs = {name: value for name, value in offered.items() if name in params}
    missing = [
        name
        for name, param in params.items()
        if name not in kwargs
        and param.default is inspect.Parameter.empty
        and param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name != "self"
    ]
    if missing:
        raise SystemExit(
            "Не удалось собрать обогатитель: LlmEnricher требует "
            f"{', '.join(missing)}. Соберите его вручную или запустите --dry-run."
        )
    return LlmEnricher(**kwargs)


# -- rendering --------------------------------------------------------


def _render_readiness(readiness: dict[str, Any]) -> list[str]:
    lines = [RULE, "ГОТОВНОСТЬ КОРПУСА"]
    vendors = readiness.get("vendors_with_dense_cell", [])
    required = readiness.get("required_vendors", 3)
    per_cell = readiness.get("required_events_per_cell", 3)
    verdict = "ДА" if readiness.get("ready_for_trend_demo") else "НЕТ"
    lines.append(
        f"Есть ли вендор с {per_cell} однотипными датированными событиями: {verdict}"
    )
    lines.append(
        f"Вендоров с плотной ячейкой: {len(vendors)} из {required} требуемых"
        + (f" — {', '.join(vendors)}" if vendors else "")
    )
    lines.append(f"Всего записей в корпусе: {readiness.get('total_statements', 0)}")
    dense = readiness.get("dense_cells", [])
    if dense:
        lines.append("Плотные ячейки:")
        for cell in dense[:15]:
            lines.append(f"  {cell['vendor']} / {cell['change_type']}: {cell['n']}")
        if len(dense) > 15:
            lines.append(f"  ... и ещё {len(dense) - 15}")
    else:
        lines.append("Плотных ячеек нет: демо тренда на этих данных не построить.")
    return lines


def _render_trends(report: BackfillReport) -> list[str]:
    lines = [RULE, "ТРЕНДЫ ПО КОРПУСУ"]
    lines.append(f"Принято кандидатов: {len(report.trends_accepted)}")
    for row in report.trends_accepted[:10]:
        lines.append(
            f"  {row['vendor']} / {row['change_type']}: {row['members']} событий, "
            f"с {row['first_observed']} по {row['last_observed']}"
        )
    lines.append(f"Отклонено кандидатов: {len(report.trends_rejected)}")
    for row in report.trends_rejected[:10]:
        lines.append(
            f"  {row['vendor']} / {row['change_type']}: {row['members']} — "
            f"{row['reason']}"
        )
    return lines


def render_dry_run(report: BackfillReport, command: str) -> str:
    lines = [
        RULE,
        "ПРОБНЫЙ ПРОГОН, ни одного вызова модели не сделано",
        RULE,
        f"Источников: {report.sources_total}",
        f"Собрано материалов: {report.materials_collected}",
        f"Уже в корпусе, пропускаются: {report.materials_already_ingested}",
        f"К обработке: {report.materials_pending}",
        f"Заданий по {report.batch_size} материалов: {report.batches_total}, "
        f"самое крупное — {report.longest_batch}",
        f"Параллель: {report.concurrency} заданий разом",
        f"Оценка времени: примерно {report.eta_seconds / 60:.0f} минут",
        "",
        f"ОЦЕНКА СТОИМОСТИ: {report.estimated_usd:.2f} USD при лимите "
        f"{report.limit_usd:.2f} USD",
    ]
    for note in report.notes:
        lines.append(f"Внимание: {note}")
    lines.append("")
    lines.append("Источники по объёму работы:")
    lines.append(
        f"  {'источник':<44}{'пр.':>4}{'собрано':>9}{'к работе':>10}{'USD':>8}"
    )
    for plan in report.plans:
        if not plan.collected and not plan.pending:
            continue
        lines.append(
            f"  {plan.source_id[:43]:<44}{plan.priority:>4}{plan.collected:>9}"
            f"{plan.pending:>10}{plan.estimated_usd:>8.2f}"
        )
    empty = [p for p in report.plans if p.status != "ok"]
    if empty:
        lines.append("")
        lines.append("Источники без материалов или с ошибкой:")
        for plan in empty:
            lines.append(f"  {plan.source_id}: {plan.status} — {plan.error}")
    lines.extend(_render_readiness(report.readiness))
    lines.append(RULE)
    lines.append(f"Настоящий запуск: {command}")
    return "\n".join(lines)


def render_report(report: BackfillReport, resume_command: str) -> str:
    lines = [
        RULE,
        f"БЭКФИЛЛ {report.run_id}" + (" (продолжение)" if report.resumed else ""),
        RULE,
        f"Записей добавлено:            {report.statements_added}",
        f"Пропущено как дубли:          {report.statements_duplicate}",
        f"Материалов обработано:        {report.materials_processed} "
        f"из {report.materials_pending}",
        f"Уже были в корпусе:           {report.materials_already_ingested}",
        f"Материалов с ошибкой:         {report.materials_failed}",
        f"Фактов принято:               {report.facts_kept}",
        f"Фактов отбраковано цитатой:   {report.facts_rejected}",
        f"Попаданий в кэш модели:       {report.cache_hits}",
        f"Заданий выполнено:            {report.batches_done} из "
        f"{report.batches_total}"
        + (
            f", пропущено по чекпоинтам {report.batches_skipped}"
            if report.batches_skipped
            else ""
        ),
        f"Источников закрыто:           {report.sources_completed} из "
        f"{report.sources_total}",
        f"Потрачено:                    {report.spent_usd:.4f} USD из "
        f"{report.limit_usd:.2f}, остаток {report.remaining_usd:.4f}",
        f"Время:                        {_duration(report.duration_s)}",
    ]
    if report.cost_by_model:
        lines.append("")
        lines.append("Стоимость по моделям:")
        for model, row in sorted(
            report.cost_by_model.items(), key=lambda kv: -kv[1]["cost_usd"]
        ):
            lines.append(
                f"  {model}: вызовов {int(row['calls'])}, {row['cost_usd']:.4f} USD"
            )

    lines.append("")
    lines.append("Покрытие по ячейкам (вендор, тип изменения):")
    for cell in report.cells[:20]:
        lines.append(f"  {cell['vendor']:<24}{cell['change_type']:<20}{cell['n']:>5}")
    if len(report.cells) > 20:
        lines.append(f"  ... и ещё {len(report.cells) - 20} ячеек")

    lines.extend(_render_trends(report))
    lines.extend(_render_readiness(report.readiness))

    lines.append(RULE)
    if report.budget_exhausted:
        lines.append(
            "Прогон остановлен лимитом стоимости. Всё извлечённое записано, "
            "чекпоинты выставлены."
        )
        lines.append(f"Продолжить с новым лимитом: {resume_command}")
    elif report.interrupted:
        lines.append("Прогон прерван. Всё извлечённое записано.")
        lines.append(f"Продолжить: {resume_command}")
    elif not report.complete:
        lines.append("Прогон завершён не полностью.")
        lines.append(f"Продолжить: {resume_command}")
    else:
        lines.append("Прогон завершён полностью.")
    for note in report.notes:
        lines.append(f"Заметка: {note}")
    return "\n".join(lines)


# -- commands ---------------------------------------------------------


def cmd_backfill(args: argparse.Namespace) -> int:
    config, conn = _load(args)
    try:
        limit_usd = resolve_limit(args.limit_usd, config)
    except BudgetNotSet as exc:
        print(str(exc), file=sys.stderr)
        return 2

    options = BackfillOptions(
        limit_usd=limit_usd,
        source_ids=_ids(args.sources),
        priorities=_priorities(args.priority),
        concurrency=args.concurrency,
        batch_size=args.batch_size,
        resume=args.resume,
        dry_run=args.dry_run,
        min_trend_members=args.min_trend_members,
    )
    fetcher = _fetcher(config, args.cache)
    enricher = None if args.dry_run else _build_enricher(config, args)

    base = f"python -m radar.cli backfill --limit-usd {limit_usd:g}"
    if args.priority:
        base += f" --priority {args.priority}"
    if args.sources:
        base += f" --sources {args.sources}"

    try:
        report = run_backfill(
            conn,
            config,
            fetcher,
            enricher,
            options,
            log_dir=args.logs,
        )
    except BudgetNotSet as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print()
    if report.dry_run:
        print(render_dry_run(report, base))
    else:
        print(render_report(report, f"{base} --resume"))
    conn.close()
    return 0 if report.complete or report.dry_run else 1


def cmd_collect(args: argparse.Namespace) -> int:
    config, conn = _load(args)
    fetcher = _fetcher(config, args.cache)
    sources = (
        select_sources(config, _ids(args.sources), _priorities(args.priority))
        if (args.sources or args.priority)
        else None
    )
    if args.mode == "backfill" and sources is None:
        sources = select_sources(config)

    run_id = f"collect-{new_run_id()}"
    run_log = RunLog(conn, run_id, date.today())
    items, outcomes = collect_all(
        config,
        fetcher,
        run_log=run_log,
        mode=args.mode,
        sources=sources,
        max_workers=6,
    )
    run_log.finish("ok")

    print(RULE)
    print(f"СБОР, режим {args.mode}, прогон {run_id}")
    print(RULE)
    for outcome in sorted(outcomes, key=lambda o: -o.count):
        line = (
            f"  {outcome.source_id[:44]:<45}{str(outcome.status):<8}{outcome.count:>6}"
        )
        if outcome.error:
            line += f"  {outcome.error[:60]}"
        print(line)
    summary = summarize(outcomes)
    print(RULE)
    print(
        f"Источников: {len(outcomes)}, из них успешно {summary.get('ok', 0)}, "
        f"пусто {summary.get('empty', 0)}, с ошибкой {summary.get('failed', 0)}"
    )
    print(f"Материалов собрано: {summary['items']}, после дедупликации {len(items)}")
    conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config, conn = _load(args)
    state = dump_state(conn)
    readiness = corpus_readiness(conn, config.data)

    print(RULE)
    print(f"СОСТОЯНИЕ БАЗЫ {args.db}")
    print(RULE)
    print(f"Схема версии {state['schema_version']}")
    for table in ("clusters", "event_statements", "trends", "signals", "runs"):
        print(f"  {table:<20}{state[table]:>8}")
    depth = state.get("corpus_depth") or {}
    print(
        f"Глубина корпуса: с {depth.get('earliest') or '—'} по "
        f"{depth.get('latest') or '—'}"
    )

    cells = state.get("cells", [])
    if cells:
        print()
        print("Покрытие по ячейкам:")
        for cell in cells[:20]:
            print(f"  {cell['vendor']:<24}{cell['change_type']:<20}{cell['n']:>5}")
        if len(cells) > 20:
            print(f"  ... и ещё {len(cells) - 20} ячеек")

    unfinished = find_unfinished_run(conn)
    if unfinished:
        journal = Journal(conn, log_dir=args.logs, run_id=unfinished)
        done = journal.completed_stages(unfinished)
        print()
        print(
            f"Незавершённый бэкфилл: {unfinished}, чекпоинтов {len(done)}. "
            "Продолжить: python -m radar.cli backfill --resume --limit-usd N"
        )

    print("\n".join(_render_readiness(readiness)))
    conn.close()
    return 0


def cmd_trends(args: argparse.Namespace) -> int:
    config, conn = _load(args)
    accepted, rejected, saved = refresh_trends(conn, config, args.min_members)
    print(RULE)
    print(
        f"ТРЕНДЫ: принято {len(accepted)}, отклонено {len(rejected)}, записано {saved}"
    )
    print(RULE)
    for candidate in accepted:
        print(
            f"  + {candidate.vendor} / {candidate.change_type}: "
            f"{candidate.members} событий, с {candidate.first_observed} "
            f"по {candidate.last_observed}, медианный интервал "
            f"{candidate.cadence_days() or 0:.0f} дней"
        )
    print()
    for candidate in rejected:
        print(
            f"  - {candidate.vendor} / {candidate.change_type}: "
            f"{candidate.members} событий — {candidate.rejected_reason}"
        )
    if not accepted:
        print("Принятых кандидатов нет: корпусу не хватает плотности.")
    conn.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(RULE)
    print("ПРОВЕРКА ОКРУЖЕНИЯ")
    print(RULE)

    binary = os.environ.get(CLI_BIN_ENV, "").strip() or shutil.which("claude")
    print(f"  claude в PATH:            {binary or 'не найден'}")
    key = os.environ.get(API_KEY_ENV, "").strip()
    print(
        f"  {API_KEY_ENV}:      "
        + (f"задан, {len(key)} символов" if key else "не задан")
    )
    if not binary and not key:
        ok = False
        print("    ни одного бэкенда модели: нужен claude в PATH или ключ OpenRouter")
    github = os.environ.get("GITHUB_TOKEN", "").strip()
    print(
        "  GITHUB_TOKEN:             "
        + (
            f"задан, {len(github)} символов"
            if github
            else "не задан — GitHub Releases пойдёт по лимиту 60 запросов в час"
        )
    )

    try:
        config = ThemeConfig.load(args.config)
        backfillable = config.backfillable_sources()
        by_priority: dict[int, int] = {}
        for source in backfillable:
            by_priority[source.priority] = by_priority.get(source.priority, 0) + 1
        spread = ", ".join(f"п{p}: {n}" for p, n in sorted(by_priority.items()))
        print(
            f"  конфиг {args.config}: источников {len(config.sources)}, "
            f"бэкфиллятся {len(backfillable)} ({spread})"
        )
    except ConfigError as exc:
        ok = False
        print(f"  конфиг {args.config}: ОШИБКА — {exc}")

    try:
        conn = init_db(args.db)
        state = dump_state(conn)
        print(
            f"  база {args.db}: записей {state['event_statements']}, "
            f"прогонов {state['runs']}, сигналов {state['signals']}"
        )
        unfinished = find_unfinished_run(conn)
        if unfinished:
            print(f"    незавершённый бэкфилл: {unfinished}")
        conn.close()
    except sqlite3.Error as exc:
        ok = False
        print(f"  база {args.db}: ОШИБКА — {exc}")

    cache_root = Path(args.cache)
    for namespace in ("model", "http"):
        folder = cache_root / namespace
        count = sum(1 for _ in folder.rglob("*.json")) if folder.exists() else 0
        print(f"  кэш {namespace}: {count} записей")

    logs = Path(args.logs)
    print(
        f"  журналы {logs}: "
        + (f"{len(list(logs.glob('*.jsonl')))} файлов" if logs.exists() else "пусто")
    )

    print(RULE)
    print("Готово к запуску." if ok else "Есть проблемы, см. выше.")
    return 0 if ok else 1


# -- argument parsing -------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m radar.cli",
        description="Радар изменений в AI-инструментах: ручные операции.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="файл базы")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="конфиг темы")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help="корень кэшей")
    parser.add_argument("--logs", default=DEFAULT_LOGS, help="каталог журналов")
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser("backfill", help="наполнить корпус историей")
    backfill.add_argument(
        "--limit-usd",
        type=float,
        default=None,
        help="лимит стоимости прогона; без него и без budget.max_usd_per_backfill "
        "запуск не производится",
    )
    backfill.add_argument("--sources", default=None, help="список id через запятую")
    backfill.add_argument(
        "--priority",
        default=None,
        help="приоритеты источников через запятую, например 1",
    )
    backfill.add_argument(
        "--concurrency", type=int, default=None, help="сколько заданий разом"
    )
    backfill.add_argument(
        "--batch-size", type=int, default=100, help="материалов в одном задании"
    )
    backfill.add_argument(
        "--resume",
        action="store_true",
        help="продолжить последний незавершённый прогон",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="собрать материалы и оценить стоимость, не обращаясь к модели",
    )
    backfill.add_argument("--min-trend-members", type=int, default=None)
    backfill.set_defaults(func=cmd_backfill)

    collect = sub.add_parser("collect", help="только сбор, без модели")
    collect.add_argument("--mode", choices=("live", "backfill"), default="live")
    collect.add_argument("--sources", default=None)
    collect.add_argument("--priority", default=None)
    collect.set_defaults(func=cmd_collect)

    status = sub.add_parser("status", help="состояние базы и готовность корпуса")
    status.set_defaults(func=cmd_status)

    trends = sub.add_parser("trends", help="пересчёт трендов по корпусу")
    trends.add_argument("--min-members", type=int, default=None)
    trends.set_defaults(func=cmd_trends)

    doctor = sub.add_parser("doctor", help="проверка окружения")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Ошибка конфига: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
