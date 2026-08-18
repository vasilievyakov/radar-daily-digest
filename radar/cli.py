"""Command line entry point: `python -m radar.cli`.

Everything a human does with this project by hand lives here — priced dry
runs, collection without a model, the corpus verdict, a trend recount and an
environment check. The daily run has its own scheduler; this is the console
for the evening the corpus is built.

Output is Russian because a person reads it; code and identifiers are English.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import os
import sqlite3
import threading
import uuid
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.backfill import (
    COST_PROFILE_CLI,
    COST_PROFILE_TOKENS,
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


class _CallLog:
    """Thread-safe stand-in for `RunLog` that the model backend writes to.

    Two problems, one object. The real run log writes to a sqlite connection
    bound to the thread that opened it, and enrichment runs in a pool. And
    `EnrichResult` reports cost but not tokens, so the only place tokens exist
    is inside the backend — which will hand them over to anything that looks
    like a run log. Calls are collected here and written once, from the main
    thread, after the run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.drops: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []

    def model_call(self, **row: Any) -> None:
        with self._lock:
            self.calls.append(row)

    def note(self, message: str) -> None:
        with self._lock:
            self.notes.append(message)

    # The rest of the RunLog surface. Implementing three methods out of nine
    # cost four materials on a single run: enrichment calls `filtered()` to
    # record why it dropped an event, and got AttributeError instead. The
    # reason this class exists is thread safety of the sqlite connection, not
    # a smaller interface, so everything else buffers the same way and is
    # flushed from the main thread by `write`.
    def filtered(
        self,
        url: str,
        title: str,
        reason_code: str,
        stage: str,
        note: str | None = None,
    ) -> None:
        with self._lock:
            self.drops.append(
                {
                    "url": url,
                    "title": title,
                    "reason_code": reason_code,
                    "stage": stage,
                    "note": note,
                }
            )

    def source_result(self, source_id: str, status: Any, items_count: int = 0,
                      latency_ms: int | None = None, error: str | None = None) -> None:
        with self._lock:
            self.sources.append(
                {
                    "source_id": source_id,
                    "status": str(status),
                    "items_count": items_count,
                    "latency_ms": latency_ms,
                    "error": error,
                }
            )

    def delivered(self, channel: str, status: str, message_id: str | None = None,
                  error: str | None = None) -> None:
        with self._lock:
            self.deliveries.append(
                {"channel": channel, "status": status,
                 "message_id": message_id, "error": error}
            )

    @contextmanager
    def stage(self, name: str, in_count: int = 0):
        """Accepted and ignored: staging belongs to the owning RunLog."""
        record: dict[str, Any] = {"stage": name, "in_count": in_count, "out_count": 0}
        yield record

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": list(self.calls),
                "notes": list(self.notes),
                "filtered": list(self.drops),
            }

    def flush(self) -> None:
        """No-op: this buffer is written once, by `write`."""

    def finish(self, status: str = "ok") -> None:
        """No-op: the owning RunLog closes the run."""

    def write(self, conn: sqlite3.Connection, run_id: str) -> tuple[int, int]:
        """Persist the collected rows. Returns (tokens in, tokens out)."""
        with conn:
            for drop in self.drops:
                conn.execute(
                    "INSERT INTO filtered_items (run_id, url, title, reason_code, "
                    "reason_note, stage) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id, url, stage) DO UPDATE SET "
                    "reason_code = excluded.reason_code",
                    (run_id, drop["url"], drop["title"], drop["reason_code"],
                     drop["note"], drop["stage"]),
                )
        tokens_in = tokens_out = 0
        with conn:
            for row in self.calls:
                tokens_in += int(row.get("tokens_in", 0) or 0)
                tokens_out += int(row.get("tokens_out", 0) or 0)
                conn.execute(
                    "INSERT INTO model_calls (call_id, run_id, stage, model, provider, "
                    "tokens_in, tokens_out, cost_usd, cached, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        run_id,
                        str(row.get("stage", "enrich")),
                        str(row.get("model", "unknown")),
                        row.get("provider"),
                        int(row.get("tokens_in", 0) or 0),
                        int(row.get("tokens_out", 0) or 0),
                        float(row.get("cost_usd", 0.0) or 0.0),
                        int(bool(row.get("cached", False))),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        return tokens_in, tokens_out


def _cost_profile(config: ThemeConfig) -> str:
    """Which price model the estimate should use.

    Mirrors how `radar.llm_cli.make_backend` picks a backend, because the two
    have to agree: token arithmetic describes OpenRouter, and the Claude CLI
    is priced per session instead.
    """
    configured = (
        str(
            (config.models or {}).get("backend")
            or config.section("llm").get("backend")
            or "auto"
        )
        .strip()
        .lower()
    )
    if configured in ("openrouter", "api"):
        return COST_PROFILE_TOKENS
    if configured in ("claude-cli", "cli", "claude"):
        return COST_PROFILE_CLI
    return (
        COST_PROFILE_TOKENS
        if os.environ.get(API_KEY_ENV, "").strip()
        else COST_PROFILE_CLI
    )


def _build_enricher(
    config: ThemeConfig,
    args: argparse.Namespace,
    fetcher: Fetcher,
    call_log: _CallLog,
) -> Any:
    """The one place that touches stage 4's implementation.

    Backfill itself depends only on the `Enricher` protocol; assembling the
    concrete enricher is a wiring decision and belongs here.

    The backend logs its calls into `call_log` rather than the real run log:
    the run log's connection belongs to the calling thread and enrichment runs
    in a pool. The budget is not handed over at all — the ceiling belongs to
    backfill, and a second counter inside the enricher would charge every call
    twice.
    """
    from radar.cache import ModelCache
    from radar.enrich import LlmEnricher
    from radar.llm_cli import make_backend

    llm = config.section("llm")
    backend = make_backend(
        config,
        cache=ModelCache(args.cache),
        run_log=call_log,
        timeout=float(llm.get("timeout_seconds", 180)),
        max_schema_retries=int(llm.get("max_schema_retries", 2)),
    )
    return LlmEnricher(
        config,
        backend,
        fetcher=fetcher,
        ingest_mode="backfill",
        # Without this the enricher passes run_log=None into every call and
        # overrides the log the backend was built with: token counts landed
        # nowhere, and the run log showed "5 calls, 0 tokens, $0.20". One row
        # like that discredits every honest number beside it.
        run_log=call_log,
    )


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
        f"Модель обогащения: {report.model_base or 'не задана'}",
        "Цена считается "
        + (
            "по замеру на claude CLI: 0.008 USD за прогретый вызов плюс один "
            "дорогой на создание кэша префикса"
            if report.cost_profile == COST_PROFILE_CLI
            else "по прайсу токенов OpenRouter"
        ),
    ]
    if report.model_critical and report.model_critical != report.model_base:
        lines.append(
            f"Критичные типы уходят на {report.model_critical}: если бы туда ушли "
            f"все материалы, вышло бы {report.estimated_max_usd:.2f} USD. "
            "Реальная цифра между двумя."
        )
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
        f"Прогрев кэша префикса:        {report.warmup_usd:.4f} USD",
        f"Токенов:                      вход {report.tokens_in}, "
        f"выход {report.tokens_out}",
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
        cost_profile=_cost_profile(config),
        log_model_calls=False,  # the backend writes richer rows via _CallLog
        resume=args.resume,
        dry_run=args.dry_run,
        min_trend_members=args.min_trend_members,
    )
    fetcher = _fetcher(config, args.cache)
    call_log = _CallLog()
    enricher = (
        None if args.dry_run else _build_enricher(config, args, fetcher, call_log)
    )

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

    if not report.dry_run:
        tokens_in, tokens_out = call_log.write(conn, report.run_id)
        report.tokens_in += tokens_in
        report.tokens_out += tokens_out

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

    def line(label: str, value: str) -> None:
        print(f"  {label + ':':<26}{value}")

    config: ThemeConfig | None = None
    try:
        config = ThemeConfig.load(args.config)
    except ConfigError:
        ok = False

    binary = os.environ.get(CLI_BIN_ENV, "").strip() or shutil.which("claude")
    line("claude в PATH", binary or "не найден")
    key = os.environ.get(API_KEY_ENV, "").strip()
    line(API_KEY_ENV, f"задан, {len(key)} символов" if key else "не задан")
    if not binary and not key:
        ok = False
        print("    ни одного бэкенда модели: нужен claude в PATH или ключ OpenRouter")
    github = os.environ.get("GITHUB_TOKEN", "").strip()
    line(
        "GITHUB_TOKEN",
        f"задан, {len(github)} символов"
        if github
        else "не задан — GitHub Releases пойдёт по лимиту 60 запросов в час",
    )
    if config is not None:
        line(
            "модель пойдёт через",
            "OpenRouter, цена по прайсу токенов"
            if _cost_profile(config) == COST_PROFILE_TOKENS
            else "claude CLI, около 0.008 USD за прогретый вызов",
        )
        backfillable = config.backfillable_sources()
        by_priority: dict[int, int] = {}
        for source in backfillable:
            by_priority[source.priority] = by_priority.get(source.priority, 0) + 1
        spread = ", ".join(f"п{p}: {n}" for p, n in sorted(by_priority.items()))
        line(
            "конфиг",
            f"{args.config}: источников {len(config.sources)}, "
            f"бэкфиллятся {len(backfillable)} ({spread})",
        )
    else:
        line("конфиг", f"{args.config}: не читается")

    try:
        conn = init_db(args.db)
        state = dump_state(conn)
        line(
            "база",
            f"{args.db}: записей {state['event_statements']}, "
            f"прогонов {state['runs']}, сигналов {state['signals']}",
        )
        unfinished = find_unfinished_run(conn)
        if unfinished:
            print(f"    незавершённый бэкфилл: {unfinished}")
        conn.close()
    except sqlite3.Error as exc:
        ok = False
        line("база", f"{args.db}: ОШИБКА — {exc}")

    cache_root = Path(args.cache)
    for namespace in ("model", "http"):
        folder = cache_root / namespace
        count = sum(1 for _ in folder.rglob("*.json")) if folder.exists() else 0
        line(f"кэш {namespace}", f"{count} записей")

    logs = Path(args.logs)
    line(
        "журналы",
        f"{logs}: {len(list(logs.glob('*.jsonl')))} файлов"
        if logs.exists()
        else f"{logs}: пусто",
    )

    print(RULE)
    print("Окружение готово. Готовность корпуса проверяется отдельно: status." if ok else "Есть проблемы, см. выше.")
    return 0 if ok else 1


# -- argument parsing -------------------------------------------------




def _deliver_run(conn: sqlite3.Connection, run: Any, result: Any) -> None:
    """Hand the run to the channels and record the outcome.

    Without this call the delivery layer existed and nothing invoked it, so
    the supervisor reported every healthy run as never delivered — the exact
    blindness the layer was written to remove.
    """
    from radar.deliver import deliver

    surfaces: dict[str, Any] = {}
    try:
        from radar.surfaces import telegram as tg

        # The surface exposes functions, not a class. Wrapping here rather
        # than inventing a class name is the point: the previous version
        # imported `TelegramSurface`, which does not exist, and would have
        # printed "канал недоступен" forever while looking wired up.
        class _Telegram:
            name = "telegram"

            def send_digest(self, signals):
                return tg.send_digest(signals)

        surfaces["telegram"] = _Telegram()
    except Exception as exc:
        print(f"Telegram недоступен: {exc}")

    if not surfaces:
        print("Ни один канал не собран, доставка пропущена.")
        return

    report = deliver(conn, surfaces, run.run_id, run.journal, run.log)
    # `finish()` already froze log_json, so the delivery rows recorded above
    # would never reach the run-log page. Re-flush to fold them in.
    run.log.flush()
    for channel in report.results:
        state = "доставлено" if channel.delivered else f"не доставлено: {channel.error}"
        print(f"  {channel.channel}: {state}")



def _reconcile_cost(conn: sqlite3.Connection, run_id: str) -> None:
    """Make the run row agree with the calls it is a summary of."""
    with conn:
        conn.execute(
            "UPDATE runs SET "
            "cost_usd = (SELECT COALESCE(SUM(cost_usd), 0) FROM model_calls WHERE run_id = ?), "
            "model_calls = (SELECT COUNT(*) FROM model_calls WHERE run_id = ?), "
            "tokens_in = (SELECT COALESCE(SUM(tokens_in), 0) FROM model_calls WHERE run_id = ?), "
            "tokens_out = (SELECT COALESCE(SUM(tokens_out), 0) FROM model_calls WHERE run_id = ?) "
            "WHERE run_id = ?",
            (run_id,) * 5,
        )


def cmd_run(args: argparse.Namespace) -> int:
    """One daily run, end to end, writing signals and nothing else.

    Delivery stays outside: the pipeline ends at the store (PUB-1), and a
    surface reads from there. Without this command the orchestrator existed
    only for its tests, which is another way of saying it did not exist.
    """
    from radar.run import DailyRun

    config, conn = _load(args)
    fetcher = _fetcher(config, args.cache)
    call_log = _CallLog()
    enricher = _build_enricher(config, args, fetcher, call_log)

    for_date = date.fromisoformat(args.for_date) if args.for_date else None
    run = DailyRun(
        conn,
        config,
        fetcher,
        enricher,
        for_date=for_date,
        log_dir=args.log_dir,
        progress=lambda line: print(line, flush=True),
        sources=(
            [s for s in config.sources if s.id in _ids(args.sources)]
            if args.sources
            else None
        ),
    )

    relevance = None
    if not args.no_filter:
        try:
            from radar.filter import RelevanceFilter
            from radar.llm_cli import make_backend

            relevance = RelevanceFilter(
                config,
                make_backend(config, prefer=getattr(args, "backend", None)),
                run_log=run.log,
                journal=run.journal,
                budget=run.budget,
            )
        except Exception as exc:
            print(f"фильтр не собран, материалы пойдут без него: {exc}")

    run.relevance_filter = relevance
    print(f"Прогон {run.run_id} за {run.for_date.isoformat()}.", flush=True)
    result = run.execute()

    print()
    print(RULE)
    print(f"Собрано материалов:      {result.collected}")
    print(f"Историй после склейки:   {result.clusters}")
    print(f"Прошло фильтр:           {result.relevant}")
    print(f"Обогащено:               {result.enriched}")
    print(f"Фактов принято:          {result.facts_kept}")
    print(f"Фактов отбраковано:      {result.facts_rejected}")
    print(f"Сигналов записано:       {len(result.signals)}")
    tokens_in, tokens_out = call_log.write(conn, run.run_id)
    for note in call_log.notes:
        run.log.note(note)
    spent = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM model_calls WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0]
    print(f"Потрачено:               {spent:.4f} USD")
    print(f"Токенов:                 {tokens_in} на вход, {tokens_out} на выход")
    if result.quiet:
        print("Тихий день: сигналов выше порога нет, запись quiet_day создана.")
    if not result.ok:
        print(f"Прогон не завершился на стадии {result.failed_stage}: {result.error}")
    print(RULE)
    if args.deliver:
        # Not gated on having digest items: a quiet day is a record, and PUB-4
        # says silence reaches the reader as a message. Gating here meant the
        # channel stayed untouched on exactly the day worth speaking about.
        _deliver_run(conn, run, result)

    # One run, one cost — and reconciled last, after every writer is done.
    # An earlier attempt ran before delivery, and delivery's flush() rewrote
    # cost_usd from the in-memory counter, quietly undoing it: the page went
    # back to "counter says $0.18, rows say $0.71".
    _reconcile_cost(conn, run.run_id)

    print(f"Страницы: .venv/bin/python -m radar.surfaces.web --run-id {run.run_id}")
    conn.close()
    return 0 if result.ok else 1



def cmd_supervise(args: argparse.Namespace) -> int:
    """Diagnose runs and say what to do about them.

    The module existed and was covered at 96 percent while being unreachable
    from anywhere but its own test. The failure it is built against — a daily
    agent that quietly stopped running and looks from outside exactly like a
    quiet day — was therefore undetectable by construction.
    """
    from radar.journal import Journal
    from radar.supervisor import Action, Supervisor

    config, conn = _load(args)
    journal = Journal(conn, log_dir=args.log_dir)
    supervisor = Supervisor(conn, journal)
    report = supervisor.report()

    print(RULE)
    print("НАБЛЮДЕНИЕ ЗА ПРОГОНАМИ")
    print(RULE)

    missed = report["missed_days"]
    if missed:
        print(f"Дней без доставленной сводки: {len(missed)}")
        for day in missed:
            print(f"  {day}")
        print("Молчание агента снаружи неотличимо от тихого дня, поэтому оно")
        print("считается отдельно и не растворяется в статистике прогонов.")
    else:
        print("Пропущенных дней нет.")
    print()

    unhealthy = report["unhealthy_runs"]
    if not unhealthy:
        print("Все прогоны завершены и доставлены.")
        conn.close()
        return 0

    print(f"Прогонов, требующих внимания: {len(unhealthy)}")
    for run in unhealthy:
        print()
        print(f"  {run['run_id']}  {run['state']}")
        print(f"    {run['reason']}")
        print(f"    стадий пройдено: {len(run['completed_stages'])}, "
              f"сигналов записано: {run['signals_written']}, "
              f"доставка: {'да' if run['delivered'] else 'нет'}")
        if run["failures"]:
            for failure in run["failures"][:3]:
                print(f"    отказ: {failure}")
        if run["next_stage"]:
            print(f"    продолжать со стадии: {run['next_stage']}")

    actions = report["actions"]
    if actions:
        print()
        print(RULE)
        print("ЧТО ДЕЛАТЬ")
        for action in actions:
            if action["action"] == str(Action.RESUME):
                print(f"  {action['run_id']}: продолжить с {action['resume_from']}")
            elif action["action"] == str(Action.RESTART):
                print(f"  {action['run_id']}: перезапустить целиком")
            else:
                print(f"  {action['run_id']}: {action['action']} — {action['reason']}")

    if args.json:
        import json as _json

        print()
        print(_json.dumps(report, ensure_ascii=False, indent=2, default=str))

    conn.close()
    # Non-zero so a scheduler notices without parsing the output.
    return 1


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

    run_cmd = sub.add_parser("run", help="один ежедневный прогон целиком")
    run_cmd.add_argument("--for-date", help="дата прогона в формате ГГГГ-ММ-ДД")
    run_cmd.add_argument("--log-dir", default="logs")
    run_cmd.add_argument("--deliver", action="store_true",
                         help="отправить результат в каналы после прогона")
    run_cmd.add_argument("--no-filter", action="store_true",
                         help="пропустить стадию релевантности")
    run_cmd.add_argument("--sources", help="ограничить источники, через запятую")
    run_cmd.set_defaults(func=cmd_run)

    supervise = sub.add_parser(
        "supervise", help="состояние прогонов и что с ними делать"
    )
    supervise.add_argument("--log-dir", default="logs")
    supervise.add_argument("--json", action="store_true",
                           help="добавить машиночитаемый отчёт")
    supervise.set_defaults(func=cmd_supervise)

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
