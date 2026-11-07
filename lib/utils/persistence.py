import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
import os
from pathlib import Path
import sqlite3
import threading
from time import perf_counter

import anyio
import pydantic
from pydantic_graph import Graph, GraphRunResult, exceptions
from pydantic_graph.nodes import BaseNode, DepsT, End
from pydantic_graph.persistence import (
    BaseStatePersistence,
    EndSnapshot,
    NodeSnapshot,
    RunEndT,
    Snapshot,
    SnapshotStatus,
    StateT,
    _utils,
    build_snapshot_list_type_adapter,
)

from lib.utils.common import PROJECT_ROOT

DB_DIR_ENV_VAR = "PERSISTENCE_DB_DIR"
"""Where to keep the resume databases, overriding `<project>/data/db`.

Point this at local disk whenever the checkout lives on a network filesystem. Every
node transition is a read-modify-write handled by a single writer thread, so the
per-operation latency of the filesystem becomes the rate at which cases can start at
all: a graph run cannot enter its first node until `load_next` has written. These
databases only hold resume state, so keeping them off shared storage costs nothing.

SQLite's WAL mode, which `_init_db` enables, is in any case documented as not working
over a network filesystem: the wal-index is a mmapped file that all readers must share.
"""


def _db_dir() -> Path:
    if override := os.environ.get(DB_DIR_ENV_VAR):
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "db"


DB_PATH = _db_dir()

CONNECT_TIMEOUT_SECONDS = 30.0
"""How long a connection waits for the file lock before giving up with "database is locked".

A judge run drives hundreds of cases at once against a single database file, so the 5s
default is far too short for the write queue that builds up at the start of a run.
"""

DB_WORKERS = min(32, (os.cpu_count() or 1) + 4)
"""Threads for snapshot I/O, matching what the default executor would have offered."""

_db_registry_lock = threading.Lock()
_db_write_locks: dict[Path, threading.Lock] = {}
_initialized_dbs: set[Path] = set()

# Snapshot I/O deliberately keeps off the default executor. On a slow filesystem this
# queue grows long enough that everything else reaching for a thread through
# `asyncio.to_thread` -- loading a case's images, for one -- would wait behind the whole
# backlog before it could even start, which stalls the model calls with it.
_db_executor = ThreadPoolExecutor(
    max_workers=DB_WORKERS, thread_name_prefix="persistence"
)


def get_persistance_db(
    task_type: str,
):
    if not DB_PATH.exists():
        DB_PATH.mkdir(parents=True, exist_ok=True)
    return DB_PATH / f"{task_type}.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=CONNECT_TIMEOUT_SECONDS)
    # journal_mode lives in the database header, but these two are per-connection and so
    # have to be repeated. Under WAL, synchronous=NORMAL only fsyncs on checkpoints, which
    # keeps each write transaction short enough that concurrent cases stop piling up.
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def _write_lock(db_path: Path) -> threading.Lock:
    """One writer at a time per database file.

    Snapshots are stored as a single JSON blob per run, so every node transition is a
    read-modify-write of the whole run. Queuing those in-process is much cheaper than
    letting a few dozen executor threads busy-poll SQLite's file lock until it times out.

    Deliberately a lock rather than a single-threaded executor: a lock hands off to
    whichever waiter wins the race, so a run that has its own writes done can move on
    while others still queue. Strict submission order would instead hold every run back
    until the slowest predecessor finished.
    """
    with _db_registry_lock:
        return _db_write_locks.setdefault(db_path, threading.Lock())


async def _in_db_executor[T](func: Callable[..., T], *args: object) -> T:
    return await asyncio.get_running_loop().run_in_executor(_db_executor, func, *args)


async def run_persistent(
    graph: Graph[StateT, DepsT, RunEndT],
    start_node: BaseNode[StateT, DepsT, RunEndT],
    persistence: BaseStatePersistence[StateT, RunEndT],
    *,
    state: StateT = None,
    deps: DepsT = None,
    infer_name: bool = True,
    reset_error_or_running_snapshots: bool = False,
) -> GraphRunResult[StateT, RunEndT]:
    persistence.set_graph_types(graph)

    if reset_error_or_running_snapshots and isinstance(
        persistence, SQLite3StatePersistence
    ):
        await persistence.reset_error_or_running_snapshots()

    existing = await persistence.load_all()

    if not existing:
        await graph.initialize(
            start_node, persistence, state=state, infer_name=infer_name
        )
    else:
        end_snapshot = next(
            (s for s in reversed(existing) if isinstance(s, EndSnapshot)),
            None,
        )
        if end_snapshot is not None:
            print("Recover result from persistence")
            return GraphRunResult(
                output=end_snapshot.result.data,
                state=end_snapshot.state,
                persistence=persistence,
                traceparent=None,
            )
    if isinstance(persistence, SQLite3StatePersistence):
        print("Start running graph with persistence, run_id: ", persistence.run_id)

    async with graph.iter_from_persistence(
        persistence, deps=deps, infer_name=infer_name
    ) as graph_run:
        async for _ in graph_run:
            pass

    result = graph_run.result

    assert result is not None, (
        "GraphRun should have a result by now, or else raised an Exception."  # Mimic Graph.run method implementation
    )

    return result


@dataclass
class SQLite3StatePersistence(BaseStatePersistence[StateT, RunEndT]):
    db_path: Path
    run_id: str

    lock: anyio.Lock = field(default_factory=anyio.Lock)

    _snapshots_type_adapter: (
        pydantic.TypeAdapter[list[Snapshot[StateT, RunEndT]]] | None
    ) = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._init_db()

    async def snapshot_node(
        self, state: StateT, next_node: BaseNode[StateT, DepsT, RunEndT]
    ) -> None:
        await self._append_save(NodeSnapshot(state=state, node=next_node))

    async def snapshot_node_if_new(
        self,
        snapshot_id: str,
        state: StateT,
        next_node: BaseNode[StateT, DepsT, RunEndT],
    ) -> None:
        snapshots = await self.load_all()
        if not any(s.id == snapshot_id for s in snapshots):
            await self._append_save(NodeSnapshot(state=state, node=next_node))

    async def snapshot_end(self, state: StateT, end: End[RunEndT]) -> None:
        await self._append_save(EndSnapshot(state=state, result=end))

    @asynccontextmanager
    async def record_run(self, snapshot_id: str) -> AsyncIterator[None]:
        snapshots = await self.load_all()
        try:
            snapshot = next(s for s in snapshots if s.id == snapshot_id)
        except StopIteration as e:
            raise LookupError(f"No snapshot found with id={snapshot_id!r}") from e

        assert isinstance(snapshot, NodeSnapshot), "Only NodeSnapshot can be recorded"
        exceptions.GraphNodeStatusError.check(snapshot.status)
        snapshot.status = "running"
        snapshot.start_ts = _utils.now_utc()
        await self._save(snapshots)

        start = perf_counter()
        try:
            yield
        except Exception:
            duration = perf_counter() - start
            await _in_db_executor(self._after_run_sync, snapshot_id, duration, "error")
            raise
        else:
            snapshot.duration = perf_counter() - start
            await _in_db_executor(
                self._after_run_sync, snapshot_id, snapshot.duration, "success"
            )

    async def load_next(self) -> NodeSnapshot | None:
        snapshots = await self.load_all()
        if snapshot := next(
            (
                s
                for s in snapshots
                if isinstance(s, NodeSnapshot) and s.status == "created"
            ),
            None,
        ):
            snapshot.status = "pending"
            await self._save(snapshots)
            return snapshot

    def should_set_types(self) -> bool:
        """Whether types need to be set."""
        return self._snapshots_type_adapter is None

    def set_types(self, state_type: type[StateT], run_end_type: type[RunEndT]) -> None:
        self._snapshots_type_adapter = build_snapshot_list_type_adapter(
            state_type, run_end_type
        )

    async def reset_error_or_running_snapshots(self) -> None:
        """Make an interrupted run resumable again.

        Besides the obvious `pending`/`running`/`error` leftovers, this revives a run whose
        last node succeeded but never got a successor: `GraphRun.next` marks a node
        successful before snapshotting the node it returned, so an interruption in between
        (a crash, or a failed snapshot write) leaves `load_next` with nothing to hand out
        and every later attempt dies with `GraphRuntimeError("Unable to restore snapshot
        from state persistence.")`. Snapshots hold the state from *before* their node ran,
        so re-running the last one simply recreates the missing successor.
        """
        snapshots = await self.load_all()
        if not snapshots or any(isinstance(s, EndSnapshot) for s in snapshots):
            return

        changed = False
        for snapshot in snapshots:
            if isinstance(snapshot, NodeSnapshot) and snapshot.status in {
                "pending",
                "running",
                "error",
            }:
                snapshot.status = "created"
                changed = True

        if not any(
            isinstance(s, NodeSnapshot) and s.status == "created" for s in snapshots
        ):
            last_node = next(
                (s for s in reversed(snapshots) if isinstance(s, NodeSnapshot)), None
            )
            if last_node is not None:
                last_node.status = "created"
                changed = True

        if changed:
            await self._save(snapshots)

    async def load_all(self) -> list[Snapshot[StateT, RunEndT]]:
        return await _in_db_executor(self._load_sync)

    def _load_sync(self) -> list[Snapshot[StateT, RunEndT]]:
        assert self._snapshots_type_adapter is not None, (
            "snapshots type adapter must be set"
        )
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT snapshots FROM graph_runs WHERE run_id = ?", (self.run_id,)
            ).fetchone()
        if row is None:
            return []
        return self._snapshots_type_adapter.validate_json(row[0])

    def _after_run_sync(
        self, snapshot_id: str, duration: float, status: SnapshotStatus
    ) -> None:
        snapshots = self._load_sync()
        snapshot = next(s for s in snapshots if s.id == snapshot_id)
        assert isinstance(snapshot, NodeSnapshot), "Only NodeSnapshot can be recorded"
        snapshot.duration = duration
        snapshot.status = status
        self._save_sync(snapshots)

    async def _save(self, snapshots: list[Snapshot[StateT, RunEndT]]) -> None:
        async with self.lock:
            await _in_db_executor(self._save_sync, snapshots)

    def _save_sync(self, snapshots: list[Snapshot[StateT, RunEndT]]) -> None:
        assert self._snapshots_type_adapter
        json_data = self._snapshots_type_adapter.dump_json(snapshots).decode()
        with _write_lock(self.db_path):
            with closing(_connect(self.db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO graph_runs (run_id, snapshots)
                    VALUES (?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET snapshots = excluded.snapshots;
                """,
                    (self.run_id, json_data),
                )

    async def _append_save(self, snapshot: Snapshot[StateT, RunEndT]) -> None:
        assert self._snapshots_type_adapter is not None, (
            "snapshots type adapter must be set"
        )

        snapshots = await self.load_all()
        snapshots.append(snapshot)
        await self._save(snapshots)

    def _init_db(self):
        # One instance is created per case, so without this guard a judge run would take
        # the exclusive lock needed by `journal_mode` hundreds of times, right while the
        # first cases are already writing snapshots.
        with _db_registry_lock:
            if self.db_path in _initialized_dbs:
                return
            with closing(_connect(self.db_path)) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS graph_runs (
                        run_id TEXT PRIMARY KEY,
                        snapshots TEXT NOT NULL
                    );
                """)
            _initialized_dbs.add(self.db_path)
