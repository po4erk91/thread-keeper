# ThreadKeeper: DB concurrency + pure retrieval quality — Implementation Plan

> План рассчитан на последовательное выполнение небольшими PR. Сначала убираем
> причины длинных write-транзакций и получаем честный baseline, затем меняем
> ranking. Нельзя одновременно выкатывать DB-runtime и новый retrieval без
> раздельных feature flags: иначе регрессию будет невозможно локализовать.

**Цели:**

1. Устранить пользовательские `database is locked` при нескольких одновременно
   работающих MCP-серверах.
2. Улучшить качество чистого retrieval (`search`, `dialog_search`, query-блок в
   `brief`) без обязательного LLM-reranker.

**Не цели первой итерации:** замена SQLite, перенос всей БД в отдельный сервис,
обязательный cross-encoder/LLM reranker, изменение публичного текстового формата
MCP-ответов.

**Базовая архитектура:** read-вызовы используют короткоживущее autocommit-
соединение без DDL/DML; foreground writes выполняются как короткие явные
`BEGIN IMMEDIATE`-транзакции с retry всей DB-only операции; ingest и фоновые
writer-задачи принадлежат daemon-host. Retrieval получает единое внутреннее API,
генерирует dense + FTS кандидатов, объединяет их и только затем форматирует ответ.

**Tech stack:** Python 3.11+, stdlib `sqlite3`, SQLite WAL/FTS5, `sqlite-vec`,
FastEmbed/ONNX, pytest + subprocess/multiprocessing. Новая внешняя зависимость для
первой итерации не нужна.

---

## Что подтверждено в текущем коде

- `get_db()` вызывается в репозитории в 162 местах и на каждом новом соединении
  повторяет `PRAGMA journal_mode=WAL`, schema check, vec0 `CREATE ... IF NOT
  EXISTS` и `commit()`.
- `brief()`, `context()` и `dialog_search()` вызывают `_ensure_session()`;
  существующая сессия делает heartbeat `UPDATE + commit`, поэтому логически
  read-only MCP tools становятся writers.
- Первый `_ensure_session()` каждого процесса синхронно запускает `_ingest_all()`.
  В `_ingest_file()` embeddings вычисляются после первых INSERT и до общего
  commit, поэтому writer-lock удерживается во время дорогой CPU/IO работы.
- WAL разрешает параллельных readers, но в SQLite всё равно только один writer.
  `busy_timeout=10s` уменьшает частоту ошибок, но не исправляет длинные
  транзакции. См. официальные SQLite docs:
  [WAL](https://www.sqlite.org/wal.html) и
  [transactions](https://www.sqlite.org/lang_transaction.html).
- `search(notes)` при доступных embeddings использует только dense retrieval.
  Если vectors отсутствуют или покрывают часть таблицы, FTS не участвует и
  возвращается `no_matches`.
- `dialog_search()` уже hybrid, но filters применяются после top-k, fused score
  теряется в `_rrf_combine()`, а FTS и dense имеют разные payload shape.
- `brief(query=...)` дублирует бинарную логику «semantic **или** FTS», вместо
  использования общей retrieval-функции.
- Текущий synthetic eval даёт FTS `52/52`, а `--semantic` — `47/52`; все пять
  провалов находятся в notes search с неполным vector coverage. Это полезный
  regression-сигнал, но не доказательство качества: текущий lexical judge не
  измеряет ranking и слишком легко засчитывает abstention.
- Установленный FastEmbed предупреждает о смене pooling, а `embed_backend`
  хранит только `onnx`/`sentence-transformers`. Старые document vectors и новый
  query vector могут оказаться из разных embedding generations без обнаружения.

---

## Acceptance gates

### DB concurrency

- 12 процессов, каждый выполняет 200 смешанных операций (reads, note/open/close,
  session heartbeat, ingest batch), три последовательных прогона: **0**
  необработанных `SQLITE_BUSY`/`SQLITE_LOCKED` и **0** потерянных writes.
- После stress: `PRAGMA quick_check = ok`; нет orphan rows в `notes_vec` и
  `dialog_vec_map`; число ожидаемых событий/заметок совпадает точно.
- `brief`, `context`, `search`, `dialog_search` после инициализации процесса не
  выполняют DDL/DML — проверяется `Connection.set_trace_callback()`.
- В write callback нет embedding, чтения transcript files, network/subprocess
  или других внешних side effects.
- p99 времени внутри write critical section ≤ 250 ms на snapshot-копии рабочей
  БД; отдельным числом публикуется wait time. Холодная schema migration в этот
  SLO не входит.
- Все соединения закрываются; любое исключение гарантированно делает rollback.

### Retrieval

- Synthetic suite проходит `52/52` в трёх режимах: без vectors, с 50% coverage и
  со 100% coverage.
- На frozen real-world dev set hybrid Recall@5 минимум на 5 п.п. выше лучшего из
  FTS-only/dense-only; на held-out test hybrid не хуже лучшего single retriever,
  MRR@10 не снижается, ни одна ось не регрессирует более чем на 2 п.п.
- False-positive rate на no-answer запросах не хуже baseline и ≤ 5%; abstention
  считается только по thresholded candidate set, а не по отсутствию trap-строки.
- Warm p95 hybrid retrieval ≤ 300 ms на snapshot-копии рабочей БД; cold model
  load измеряется отдельно.
- Средний возвращаемый token budget не растёт более чем на 10% без измеримого
  выигрыша Recall@5.

---

## Track A — устранение lock contention

### Task A0: воспроизводимый contention baseline и диагностика

**Files:**

- Create: `scripts/db_stress.py`
- Create: `tests/test_db_contention.py`
- Modify: `threadkeeper/db.py`
- Modify: `threadkeeper/tools/dashboard.py`

- [ ] Создать worker, который стартует отдельный процесс с общей temp DB и
  детерминированным seed. Профиль нагрузки: 70% reads, 20% foreground writes,
  10% ingest/heartbeat.
- [ ] В каждом результате сохранять `op`, `attempts`, `lock_wait_ms`,
  `critical_section_ms`, SQLite error code; не писать метрики в ту же SQLite DB.
- [ ] Добавить сценарий, где один процесс намеренно держит write lock, чтобы
  regression test воспроизводил текущую ошибку, а не надеялся на race.
- [ ] Добавить `--snapshot`, который сначала делает безопасную копию через
  SQLite backup API и никогда не нагружает live DB.
- [ ] Зафиксировать baseline JSON в CI artifact, но не делать случайные latency
  числа golden-тестом.

**Verify:**

```bash
rtk env PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/test_db_contention.py -v
rtk env PYTHONPATH="$PWD" .venv/bin/python scripts/db_stress.py --processes 12 --ops 200
```

### Task A1: разделить bootstrap, read connections и write transactions

**Files:**

- Modify: `threadkeeper/db.py`
- Create: `tests/test_db_runtime.py`
- Modify: `threadkeeper/server.py`
- Modify: `threadkeeper/host.py`
- Modify: CLI entrypoints, которые открывают DB напрямую

**Новые внутренние интерфейсы:**

```python
bootstrap_db() -> None
read_connection() -> ContextManager[sqlite3.Connection]
run_write(op: str, fn: Callable[[sqlite3.Connection], T], *, deadline_s: float) -> T
```

- [ ] `bootstrap_db()` один раз на процесс выполняет path hardening, WAL setup,
  schema migration и создание vec tables. Миграционный wait остаётся отдельным
  длинным контуром и не смешивается с обычным retry.
- [ ] Явно вызвать bootstrap из MCP server, daemon-host, setup/migration и
  standalone CLI entrypoints. Read connection при несовместимой schema выдаёт
  `SchemaNotReady`, но никогда не пытается мигрировать её скрыто.
- [ ] Обычные соединения открывать с `isolation_level=None` (autocommit),
  `busy_timeout` и `row_factory`; read connection получает
  `PRAGMA query_only=ON` и не выполняет `journal_mode`, DDL или commit.
- [ ] `run_write()` на каждой попытке открывает свежее соединение, делает
  `BEGIN IMMEDIATE`, вызывает DB-only callback, commit и close.
- [ ] Retry разрешён только для `SQLITE_BUSY`/`SQLITE_LOCKED` по
  `sqlite_errorcode`; перед новой попыткой обязательны rollback + close.
- [ ] Backoff: bounded exponential + jitter до общего deadline. После deadline
  вернуть одну типизированную ошибку с `op`, attempts и total wait.
- [ ] Не ретраить callback после неизвестного commit outcome. Сначала проверить
  transaction state/идемпотентный operation key; внешние side effects должны
  происходить после успешного commit.
- [ ] Оставить `get_db()` как временный compatibility shim, пометить low-level и
  запретить новые production call sites отдельным AST-тестом.

**Tests:** read path has no DDL/DML; retry succeeds after released lock; deadline
is bounded; callback exception rolls back; connection closes on every branch;
non-lock OperationalError is surfaced immediately.

### Task A2: сделать session lifecycle независимым от read tools

**Files:**

- Modify: `threadkeeper/identity.py`
- Modify: `threadkeeper/server.py`
- Modify: `threadkeeper/tools/threads.py`
- Modify: `threadkeeper/tools/dialog.py`
- Create: `tests/test_session_read_purity.py`

- [ ] Разбить `_ensure_session()` на `initialize_session_once()`, чистый
  `current_session_id()` и короткий `write_heartbeat()`.
- [ ] Инициализировать session один раз при старте MCP server, а не в первом
  tool call.
- [ ] Удалить synchronous heartbeat из `brief/context/search/dialog_search`.
- [ ] Heartbeat выполнять отдельным rate-limited loop (например, раз в 60 s) и
  через `run_write()`. Mutating tools обновляют presence внутри своей основной
  транзакции без дополнительного commit.
- [ ] Вынести из session init: transcript ingest, FTS backfill, запуск всех
  daemons и dialectic self-heal. Каждая подсистема получает явный startup hook.
- [ ] Тестировать не только результат, но SQL trace: повторный read tool не
  должен содержать `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `PRAGMA journal_mode`.

### Task A3: двухфазный ingest без embeddings под write lock

**Files:**

- Modify: `threadkeeper/ingest.py`
- Modify: `threadkeeper/host.py`
- Create: `tests/test_ingest_transactions.py`
- Modify: `tests/test_ingest_cursor.py`

- [ ] Phase 1 (без write transaction): найти изменившиеся файлы, нормализовать и
  scrub сообщения, прочитать уже существующие UUID через read connection.
- [ ] Phase 2 (тоже без write transaction): batch-вычислить embeddings.
- [ ] Phase 3: маленький `run_write("ingest_batch", ...)` с `INSERT OR IGNORE`,
  vec mirror и cursor update в одной транзакции. Batch size сделать
  конфигурируемым, стартовое значение — 50, затем подобрать по p99.
- [ ] Если concurrent process уже вставил UUID, считать это нормальным dedup, а
  не ошибкой; cursor продвигать только вместе с успешно сохранённым batch.
- [ ] При `THREADKEEPER_DAEMON_HOST=1` one-shot и periodic ingest выполняет
  только host. Thin servers больше не вызывают `_ingest_all()`.
- [ ] При legacy flag-off защитить one-shot ingest `single_flight_lock`, чтобы
  несколько MCP servers не сканировали и не писали один corpus одновременно.
- [ ] Ошибку vec mirror не проглатывать бесследно: base row остаётся source of
  truth, а reconciliation job видит missing vec row и восстанавливает его.

### Task A4: мигрировать mutating tools на единый transaction boundary

**Files:**

- Modify: `threadkeeper/tools/threads.py`
- Modify: `threadkeeper/tools/session.py`
- Modify: `threadkeeper/tools/style.py`
- Modify: `threadkeeper/tools/peers.py`
- Modify: остальные `threadkeeper/tools/*.py` и daemon modules по batches
- Create: `tests/test_db_api_usage.py`

- [ ] Первая партия: `open_thread`, `note`, `close_thread`, `session_end`,
  `verbatim_user`, signals — наиболее частые foreground writes.
- [ ] Embedding и построение payload делать до `run_write`; ID generation,
  base-row + vec-row + event + presence — внутри одной транзакции.
- [ ] Вторая партия: daemon writers. Одна итерация daemon должна захватывать
  single-flight до чтения work queue, но write transaction — только вокруг
  claim/update конкретного batch.
- [ ] Отделить filesystem/network/subprocess side effects от retriable DB
  callback через outbox/operation state, где это требуется.
- [ ] AST-тест постепенно уменьшает allowlist прямых `get_db()/commit()` и в
  конце запрещает их в `threadkeeper/tools`.
- [ ] Никакой `except OperationalError: pass` для lock errors. Допустимый
  degradation должен логировать причину и оставлять восстановимый backlog.

### Task A5: daemon-host rollout и окончательный concurrency gate

**Files:**

- Modify: `threadkeeper/host.py`
- Modify: `threadkeeper/config.py`
- Modify: `tests/test_thin_session.py`
- Create: `tests/test_host_election_soak.py`
- Modify: `docs/operations.md` (или актуальный operations doc)

- [ ] Проверить sustained election: спустя heartbeat TTL существует ровно один
  lock owner и один активный host heartbeat; loser processes быстро завершаются.
- [ ] Сначала включить host-owned ingest и DB runtime под отдельными flags на
  локальном snapshot soak, затем на canary install.
- [ ] После трёх чистых stress-прогонов и 24 h canary включить transaction v2 по
  умолчанию. Daemon-host default менять отдельным PR после проверки startup,
  upgrade и rollback на всех поддерживаемых CLI.
- [ ] Rollback сохраняет старые BLOB embeddings и schema; flags возвращают
  прежний execution path без downgrade DB.
- [ ] Если после A1–A5 acceptance gate всё ещё не проходит, только тогда делать
  отдельный narrow write-broker через существующий host socket. Не начинать с
  RPC-сериализации: короткие foreground writes SQLite должен выдержать сам, а
  перенос 162 call sites за IPC значительно расширяет failure surface.

---

## Track B — качество чистого memory retrieval

### Task B0: единая внутренняя модель кандидата

**Files:**

- Create: `threadkeeper/retrieval.py`
- Create: `tests/test_retrieval_engine.py`
- Modify: `threadkeeper/embeddings.py`

**Interfaces:**

```python
@dataclass
class Candidate:
    id: str
    source: Literal["note", "dialog"]
    content: str
    created_at: int
    thread_id: str | None
    session_id: str | None
    role: str | None
    dense_score: float | None
    lexical_score: float | None
    fused_score: float | None
    matched_by: tuple[str, ...]

retrieve(query: RetrievalQuery) -> list[Candidate]
```

- [ ] Отделить candidate generation/ranking от MCP string formatting.
- [ ] Notes и dialog adapters возвращают одинаковую форму и стабильный ID.
- [ ] FTS queries возвращают BM25/rank и явно сортируются; dense сохраняет raw
  cosine; fusion записывает `fused_score`, а не выбрасывает его.
- [ ] `matched_by` показывает `fts`, `dense` или оба — это нужно для eval и
  диагностики, но публичный формат можно пока оставить прежним.
- [ ] Role/project/thread/time filters должны попадать в SQL/ANN candidate
  generation до top-k, а не фильтровать уже обрезанный список.

### Task B1: hybrid-by-default для notes, dialog и brief

**Files:**

- Modify: `threadkeeper/retrieval.py`
- Modify: `threadkeeper/tools/threads.py`
- Modify: `threadkeeper/tools/dialog.py`
- Modify: `threadkeeper/brief.py`
- Modify: `tests/test_search_fts_punctuation.py`
- Create: `tests/test_hybrid_retrieval.py`

- [ ] Для каждого запроса запускать FTS candidate generation независимо от
  наличия embeddings. Dense является дополнительным каналом, а не выключателем
  lexical retrieval.
- [ ] При отказе host embedding socket, пустом vec index или partial coverage
  FTS продолжает возвращать результаты.
- [ ] Начать с обычного RRF и over-fetch `max(k*8, 40)`; веса/константы менять
  только через ablation на dev set.
- [ ] Для notes добавить корректный `ORDER BY bm25(...)`/`rank`; текущий LIMIT
  без явного ranking удалить.
- [ ] `search`, `dialog_search` и query-секция `brief` вызывают один engine и
  расходятся только форматированием/token budget.
- [ ] Добавить тесты exact identifiers (`T87e`, error codes, paths), paraphrase,
  punctuation, multilingual queries и partial vector coverage.

### Task B2: embedding generation fingerprint и index health

**Files:**

- Modify: `threadkeeper/embeddings.py`
- Modify: `threadkeeper/ingest.py`
- Modify: `threadkeeper/tools/dashboard.py`
- Modify: `threadkeeper/tools/threads.py` (`context` fields if appropriate)
- Modify: `pyproject.toml`
- Create: `tests/test_embedding_generation.py`

- [ ] Превратить `embed_tag()` из `onnx` в fingerprint минимум из backend,
  model ID, dimension, pooling contract и совместимой версии runtime.
- [ ] Зафиксировать совместимый FastEmbed range (текущий `>=0.3` слишком широк)
  и выбрать одну generation; после смены pooling выполнить resumable re-embed.
- [ ] Не сравнивать query vector с rows другой generation. Такие rows остаются
  доступны через FTS до backfill.
- [ ] Публиковать отдельно для notes/dialog: total rows, current-generation BLOB
  coverage, vec mirror coverage, orphan count, last backfill error/time.
- [ ] Backfill выполнять маленькими resumable batches и никогда не держать write
  lock во время encoding.
- [ ] Добавить reconciliation для BLOB↔vec0. Silent vec upsert failure не должен
  превращаться в постоянную невидимую потерю recall.

### Task B3: сделать eval ranking-aware и защищённым от самообмана

**Files:**

- Modify: `scripts/memory_eval/run.py`
- Modify: `scripts/memory_eval/ground_truth.json`
- Modify: `tests/test_memory_eval.py`
- Create: `scripts/memory_eval/label_snapshot.py`
- Modify: `.gitignore` для локальных snapshot/labels artifacts

- [ ] Eval вызывает внутренний `retrieve()` и оценивает candidate IDs/ranks, а
  отдельно тестирует итоговое MCP formatting.
- [ ] Добавить Recall@1/5/10, MRR@10, nDCG@10, no-answer precision/recall/F1,
  latest-fact@k, latency p50/p95 и tokens returned.
- [ ] Semantic fixture обязана реально seed-ить/mock-ить vectors. Текущий тест
  проверяет только default FTS path и не видит провал `47/52`.
- [ ] Для abstention считать успехом отсутствие кандидата выше calibrated
  threshold. Простое отсутствие `trap_substrings` больше не считается отказом.
- [ ] Создать локальный workflow: snapshot через backup API → sample queries →
  ручная разметка relevant IDs → deterministic dev/test split. Сырые диалоги и
  labels пользователя не коммитить.
- [ ] Каждый эксперимент выдаёт таблицу FTS-only / dense-only / hybrid и
  сохраняет config + embedding fingerprint вместе с результатом.

### Task B4: reranking только после hybrid baseline

**Files:**

- Modify: `threadkeeper/retrieval.py`
- Create: `tests/test_retrieval_rerank.py`
- Modify: `scripts/memory_eval/run.py`

- [ ] Добавить session/thread diversity (MMR либо простой cap) только если один
  transcript burst вытесняет независимые релевантные источники.
- [ ] Добавить rule-based temporal intent: `current/latest/now` даёт controlled
  freshness boost; `first/original/previous` его отключает или инвертирует.
- [ ] Calibrate no-match threshold отдельно для dense evidence, exact lexical
  evidence и fused candidates; не threshold-ить голый RRF score между разными k.
- [ ] Не добавлять cross-encoder/LLM reranker, пока hybrid + coverage + temporal
  rules не достигли plateau. Если добавлять позже — отдельный optional flag,
  latency/cost budget и обязательная ablation.
- [ ] Ship только компоненты, которые проходят held-out gates; не объединять
  несколько эвристик в один недиагностируемый PR.

---

## Рекомендуемый порядок PR

1. **PR-1 — Baselines:** A0 + расширение eval из B3 без изменения поведения.
2. **PR-2 — DB runtime:** A1, compatibility shim, unit tests.
3. **PR-3 — Read purity:** A2 и host startup hooks.
4. **PR-4 — Ingest:** A3; это ожидаемый крупнейший выигрыш по lock duration.
5. **PR-5 — Core writes + soak:** первая партия A4, затем A5 gate.
6. **PR-6 — Retrieval core:** B0 + B1 за flag; сразу закрывает partial-coverage
   failure и унифицирует `search/dialog_search/brief`.
7. **PR-7 — Embedding generation:** B2 + resumable backfill.
8. **PR-8 — Ranking improvements:** B4 по одному компоненту с ablation.
9. **PR-9 — Defaults/docs:** включить доказавшие себя flags по умолчанию,
   сохранить быстрый rollback.

Track B можно начинать после PR-1 параллельно с A1–A5, но re-embedding/backfill
нужно запускать только после A3: иначе большой backfill снова создаст длинную
write-транзакцию и исказит concurrency результаты.

## Definition of done

- Concurrency stress и held-out retrieval eval сохранены как воспроизводимые
  команды, а не как разовый ручной отчёт.
- Live MCP tools не возвращают `database is locked` под acceptance profile.
- Read tools после startup действительно read-only по SQL trace.
- Ingest/embedding не выполняются под writer lock.
- Notes retrieval остаётся работоспособным при 0–100% vector coverage.
- Все возвращённые hits имеют единый provenance + fused score во внутреннем API.
- Embedding mismatch обнаруживается и восстанавливается resumable backfill.
- Новые defaults имеют feature-flag rollback и documented operational checks.
