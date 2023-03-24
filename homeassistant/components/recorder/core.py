"""Support for recording details."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError
import contextlib
from datetime import datetime, timedelta
import logging
import queue
import sqlite3
import threading
import time
from typing import Any, TypeVar

import async_timeout
from sqlalchemy import create_engine, event as sqlalchemy_event, exc, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.orm.session import Session

from homeassistant.components import persistent_notification
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    MATCH_ALL,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
    async_track_utc_time_change,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import UNDEFINED, UndefinedType
import homeassistant.util.dt as dt_util
from homeassistant.util.enum import try_parse_enum

from . import migration, statistics
from .const import (
    CONTEXT_ID_AS_BINARY_SCHEMA_VERSION,
    DB_WORKER_PREFIX,
    DOMAIN,
    EVENT_TYPE_IDS_SCHEMA_VERSION,
    KEEPALIVE_TIME,
    LEGACY_STATES_EVENT_ID_INDEX_SCHEMA_VERSION,
    MARIADB_PYMYSQL_URL_PREFIX,
    MARIADB_URL_PREFIX,
    MAX_QUEUE_BACKLOG,
    MYSQLDB_PYMYSQL_URL_PREFIX,
    MYSQLDB_URL_PREFIX,
    SQLITE_URL_PREFIX,
    STATES_META_SCHEMA_VERSION,
    STATISTICS_ROWS_SCHEMA_VERSION,
    SupportedDialect,
)
from .db_schema import (
    LEGACY_STATES_EVENT_ID_INDEX,
    SCHEMA_VERSION,
    TABLE_STATES,
    Base,
    EventData,
    Events,
    EventTypes,
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsShortTerm,
)
from .executor import DBInterruptibleThreadPoolExecutor
from .models import DatabaseEngine, StatisticData, StatisticMetaData, UnsupportedDialect
from .pool import POOL_SIZE, MutexPool, RecorderPool
from .queries import (
    has_entity_ids_to_migrate,
    has_event_type_to_migrate,
    has_events_context_ids_to_migrate,
    has_states_context_ids_to_migrate,
)
from .table_managers.event_data import EventDataManager
from .table_managers.event_types import EventTypeManager
from .table_managers.recorder_runs import RecorderRunsManager
from .table_managers.state_attributes import StateAttributesManager
from .table_managers.states import StatesManager
from .table_managers.states_meta import StatesMetaManager
from .table_managers.statistics_meta import StatisticsMetaManager
from .tasks import (
    AdjustLRUSizeTask,
    AdjustStatisticsTask,
    ChangeStatisticsUnitTask,
    ClearStatisticsTask,
    CommitTask,
    CompileMissingStatisticsTask,
    DatabaseLockTask,
    EntityIDMigrationTask,
    EventIdMigrationTask,
    EventsContextIDMigrationTask,
    EventTask,
    EventTypeIDMigrationTask,
    ImportStatisticsTask,
    KeepAliveTask,
    PerodicCleanupTask,
    PurgeTask,
    RecorderTask,
    StatesContextIDMigrationTask,
    StatisticsTask,
    StopTask,
    SynchronizeTask,
    UpdateStatesMetadataTask,
    UpdateStatisticsMetadataTask,
    WaitTask,
)
from .util import (
    build_mysqldb_conv,
    dburl_to_path,
    end_incomplete_runs,
    execute_stmt_lambda_element,
    get_index_by_name,
    is_second_sunday,
    move_away_broken_database,
    session_scope,
    setup_connection_for_dialect,
    validate_or_move_away_sqlite_database,
    write_lock_db_sqlite,
)

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_URL = "sqlite:///{hass_config_path}"

# Controls how often we clean up
# States and Events objects
EXPIRE_AFTER_COMMITS = 120

SHUTDOWN_TASK = object()

COMMIT_TASK = CommitTask()
KEEP_ALIVE_TASK = KeepAliveTask()
WAIT_TASK = WaitTask()
ADJUST_LRU_SIZE_TASK = AdjustLRUSizeTask()

DB_LOCK_TIMEOUT = 30
DB_LOCK_QUEUE_CHECK_TIMEOUT = 1


INVALIDATED_ERR = "Database connection invalidated"
CONNECTIVITY_ERR = "Error in database connectivity during commit"

# Pool size must accommodate Recorder thread + All db executors
MAX_DB_EXECUTOR_WORKERS = POOL_SIZE - 1


class Recorder(threading.Thread):
    """A threaded recorder class."""

    stop_requested: bool

    def __init__(
        self,
        hass: HomeAssistant,
        auto_purge: bool,
        auto_repack: bool,
        keep_days: int,
        commit_interval: int,
        uri: str,
        db_max_retries: int,
        db_retry_wait: int,
        entity_filter: Callable[[str], bool],
        exclude_event_types: set[str],
        exclude_attributes_by_domain: dict[str, set[str]],
    ) -> None:
        """Initialize the recorder."""
        threading.Thread.__init__(self, name="Recorder")

        self.hass = hass
        self.thread_id: int | None = None
        self.auto_purge = auto_purge
        self.auto_repack = auto_repack
        self.keep_days = keep_days
        self._hass_started: asyncio.Future[object] = asyncio.Future()
        self.commit_interval = commit_interval
        self._queue: queue.SimpleQueue[RecorderTask] = queue.SimpleQueue()
        self.db_url = uri
        self.db_max_retries = db_max_retries
        self.db_retry_wait = db_retry_wait
        self.database_engine: DatabaseEngine | None = None
        # Database connection is ready, but non-live migration may be in progress
        db_connected: asyncio.Future[bool] = hass.data[DOMAIN].db_connected
        self.async_db_connected: asyncio.Future[bool] = db_connected
        # Database is ready to use but live migration may be in progress
        self.async_db_ready: asyncio.Future[bool] = asyncio.Future()
        # Database is ready to use and all migration steps completed (used by tests)
        self.async_recorder_ready = asyncio.Event()
        self._queue_watch = threading.Event()
        self.engine: Engine | None = None

        # The entity_filter is exposed on the recorder instance so that
        # it can be used to see if an entity is being recorded and is called
        # by is_entity_recorder and the sensor recorder.
        self.entity_filter = entity_filter
        self.exclude_event_types = exclude_event_types

        self.schema_version = 0
        self._commits_without_expire = 0

        self.recorder_runs_manager = RecorderRunsManager()
        self.states_manager = StatesManager()
        self.event_data_manager = EventDataManager(self)
        self.event_type_manager = EventTypeManager(self)
        self.states_meta_manager = StatesMetaManager(self)
        self.state_attributes_manager = StateAttributesManager(
            self, exclude_attributes_by_domain
        )
        self.statistics_meta_manager = StatisticsMetaManager(self)

        self.event_session: Session | None = None
        self._get_session: Callable[[], Session] | None = None
        self._completed_first_database_setup: bool | None = None
        self.async_migration_event = asyncio.Event()
        self.migration_in_progress = False
        self.migration_is_live = False
        self.use_legacy_events_index = False
        self._database_lock_task: DatabaseLockTask | None = None
        self._db_executor: DBInterruptibleThreadPoolExecutor | None = None

        self._event_listener: CALLBACK_TYPE | None = None
        self._queue_watcher: CALLBACK_TYPE | None = None
        self._keep_alive_listener: CALLBACK_TYPE | None = None
        self._commit_listener: CALLBACK_TYPE | None = None
        self._periodic_listener: CALLBACK_TYPE | None = None
        self._nightly_listener: CALLBACK_TYPE | None = None
        self._dialect_name: SupportedDialect | None = None
        self.enabled = True

    @property
    def backlog(self) -> int:
        """Return the number of items in the recorder backlog."""
        return self._queue.qsize()

    @property
    def dialect_name(self) -> SupportedDialect | None:
        """Return the dialect the recorder uses."""
        return self._dialect_name

    @property
    def _using_file_sqlite(self) -> bool:
        """Short version to check if we are using sqlite3 as a file."""
        return self.db_url != SQLITE_URL_PREFIX and self.db_url.startswith(
            SQLITE_URL_PREFIX
        )

    @property
    def recording(self) -> bool:
        """Return if the recorder is recording."""
        return self._event_listener is not None

    def get_session(self) -> Session:
        """Get a new sqlalchemy session."""
        if self._get_session is None:
            raise RuntimeError("The database connection has not been established")
        return self._get_session()

    def queue_task(self, task: RecorderTask) -> None:
        """Add a task to the recorder queue."""
        self._queue.put(task)

    def set_enable(self, enable: bool) -> None:
        """Enable or disable recording events and states."""
        self.enabled = enable

    @callback
    def async_start_executor(self) -> None:
        """Start the executor."""
        self._db_executor = DBInterruptibleThreadPoolExecutor(
            thread_name_prefix=DB_WORKER_PREFIX,
            max_workers=MAX_DB_EXECUTOR_WORKERS,
            shutdown_hook=self._shutdown_pool,
        )

    def _shutdown_pool(self) -> None:
        """Close the dbpool connections in the current thread."""
        if self.engine and hasattr(self.engine.pool, "shutdown"):
            self.engine.pool.shutdown()

    @callback
    def async_initialize(self) -> None:
        """Initialize the recorder."""
        self._event_listener = self.hass.bus.async_listen(
            MATCH_ALL,
            self.event_listener,
            run_immediately=True,
        )
        self._queue_watcher = async_track_time_interval(
            self.hass, self._async_check_queue, timedelta(minutes=10)
        )

    @callback
    def _async_keep_alive(self, now: datetime) -> None:
        """Queue a keep alive."""
        if self._event_listener:
            self.queue_task(KEEP_ALIVE_TASK)

    @callback
    def _async_commit(self, now: datetime) -> None:
        """Queue a commit."""
        if (
            self._event_listener
            and not self._database_lock_task
            and self._event_session_has_pending_writes()
        ):
            self.queue_task(COMMIT_TASK)

    @callback
    def async_add_executor_job(
        self, target: Callable[..., T], *args: Any
    ) -> asyncio.Future[T]:
        """Add an executor job from within the event loop."""
        return self.hass.loop.run_in_executor(self._db_executor, target, *args)

    def _stop_executor(self) -> None:
        """Stop the executor."""
        if self._db_executor is None:
            return
        self._db_executor.shutdown()
        self._db_executor = None

    @callback
    def _async_check_queue(self, *_: Any) -> None:
        """Periodic check of the queue size to ensure we do not exhaust memory.

        The queue grows during migration or if something really goes wrong.
        """
        size = self.backlog
        _LOGGER.debug("Recorder queue size is: %s", size)
        if size <= MAX_QUEUE_BACKLOG:
            return
        _LOGGER.error(
            (
                "The recorder backlog queue reached the maximum size of %s events; "
                "usually, the system is CPU bound, I/O bound, or the database "
                "is corrupt due to a disk problem; The recorder will stop "
                "recording events to avoid running out of memory"
            ),
            MAX_QUEUE_BACKLOG,
        )
        self._async_stop_queue_watcher_and_event_listener()

    @callback
    def _async_stop_queue_watcher_and_event_listener(self) -> None:
        """Stop watching the queue and listening for events."""
        if self._queue_watcher:
            self._queue_watcher()
            self._queue_watcher = None
        if self._event_listener:
            self._event_listener()
            self._event_listener = None

    @callback
    def _async_stop_listeners(self) -> None:
        """Stop listeners."""
        self._async_stop_queue_watcher_and_event_listener()
        if self._keep_alive_listener:
            self._keep_alive_listener()
            self._keep_alive_listener = None
        if self._commit_listener:
            self._commit_listener()
            self._commit_listener = None
        if self._nightly_listener:
            self._nightly_listener()
            self._nightly_listener = None
        if self._periodic_listener:
            self._periodic_listener()
            self._periodic_listener = None

    @callback
    def _async_event_filter(self, event: Event) -> bool:
        """Filter events."""
        if event.event_type in self.exclude_event_types:
            return False

        if (entity_id := event.data.get(ATTR_ENTITY_ID)) is None:
            return True

        if isinstance(entity_id, str):
            return self.entity_filter(entity_id)

        if isinstance(entity_id, list):
            for eid in entity_id:
                if self.entity_filter(eid):
                    return True
            return False

        # Unknown what it is.
        return True

    @callback
    def _async_empty_queue(self, event: Event) -> None:
        """Empty the queue if its still present at final write."""

        # If the queue is full of events to be processed because
        # the database is so broken that every event results in a retry
        # we will never be able to get though the events to shutdown in time.
        #
        # We drain all the events in the queue and then insert
        # an empty one to ensure the next thing the recorder sees
        # is a request to shutdown.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.queue_task(StopTask())

    async def _async_shutdown(self, event: Event) -> None:
        """Shut down the Recorder."""
        if not self._hass_started.done():
            self._hass_started.set_result(SHUTDOWN_TASK)
        self.queue_task(StopTask())
        self._async_stop_listeners()
        await self.hass.async_add_executor_job(self.join)

    @callback
    def _async_hass_started(self, hass: HomeAssistant) -> None:
        """Notify that hass has started."""
        self._hass_started.set_result(None)

    @callback
    def async_register(self) -> None:
        """Post connection initialize."""
        bus = self.hass.bus
        bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, self._async_empty_queue)
        bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_shutdown)
        async_at_started(self.hass, self._async_hass_started)

    @callback
    def async_connection_failed(self) -> None:
        """Connect failed tasks."""
        self.async_db_connected.set_result(False)
        self.async_db_ready.set_result(False)
        persistent_notification.async_create(
            self.hass,
            "The recorder could not start, check [the logs](/config/logs)",
            "Recorder",
        )
        self._async_stop_listeners()

    @callback
    def async_connection_success(self) -> None:
        """Connect to the database succeeded, schema version and migration need known.

        The database may not yet be ready for use in case of a non-live migration.
        """
        self.async_db_connected.set_result(True)

    @callback
    def async_set_db_ready(self) -> None:
        """Database live and ready for use.

        Called after non-live migration steps are finished.
        """
        if self.async_db_ready.done():
            return
        self.async_db_ready.set_result(True)
        self.async_start_executor()

    @callback
    def _async_set_recorder_ready_migration_done(self) -> None:
        """Finish start and mark recorder ready.

        Called after all migration steps are finished.
        """
        self._async_setup_periodic_tasks()
        self.async_recorder_ready.set()

    @callback
    def async_nightly_tasks(self, now: datetime) -> None:
        """Trigger the purge."""
        if self.auto_purge:
            # Purge will schedule the periodic cleanups
            # after it completes to ensure it does not happen
            # until after the database is vacuumed
            repack = self.auto_repack and is_second_sunday(now)
            purge_before = dt_util.utcnow() - timedelta(days=self.keep_days)
            self.queue_task(PurgeTask(purge_before, repack=repack, apply_filter=False))
        else:
            self.queue_task(PerodicCleanupTask())

    @callback
    def _async_five_minute_tasks(self, now: datetime) -> None:
        """Run tasks every five minutes."""
        self.queue_task(ADJUST_LRU_SIZE_TASK)
        self.async_periodic_statistics()

    def _adjust_lru_size(self) -> None:
        """Trigger the LRU adjustment.

        If the number of entities has increased, increase the size of the LRU
        cache to avoid thrashing.
        """
        new_size = self.hass.states.async_entity_ids_count() * 2
        self.state_attributes_manager.adjust_lru_size(new_size)
        self.states_meta_manager.adjust_lru_size(new_size)
        self.statistics_meta_manager.adjust_lru_size(new_size)

    @callback
    def async_periodic_statistics(self) -> None:
        """Trigger the statistics run.

        Short term statistics run every 5 minutes
        """
        start = statistics.get_start_time()
        self.queue_task(StatisticsTask(start, True))

    @callback
    def async_adjust_statistics(
        self,
        statistic_id: str,
        start_time: datetime,
        sum_adjustment: float,
        adjustment_unit: str,
    ) -> None:
        """Adjust statistics."""
        self.queue_task(
            AdjustStatisticsTask(
                statistic_id, start_time, sum_adjustment, adjustment_unit
            )
        )

    @callback
    def async_clear_statistics(self, statistic_ids: list[str]) -> None:
        """Clear statistics for a list of statistic_ids."""
        self.queue_task(ClearStatisticsTask(statistic_ids))

    @callback
    def async_update_statistics_metadata(
        self,
        statistic_id: str,
        *,
        new_statistic_id: str | UndefinedType = UNDEFINED,
        new_unit_of_measurement: str | None | UndefinedType = UNDEFINED,
    ) -> None:
        """Update statistics metadata for a statistic_id."""
        self.queue_task(
            UpdateStatisticsMetadataTask(
                statistic_id, new_statistic_id, new_unit_of_measurement
            )
        )

    @callback
    def async_update_states_metadata(
        self,
        entity_id: str,
        new_entity_id: str,
    ) -> None:
        """Update states metadata for an entity_id."""
        self.queue_task(UpdateStatesMetadataTask(entity_id, new_entity_id))

    @callback
    def async_change_statistics_unit(
        self,
        statistic_id: str,
        *,
        new_unit_of_measurement: str,
        old_unit_of_measurement: str,
    ) -> None:
        """Change statistics unit for a statistic_id."""
        self.queue_task(
            ChangeStatisticsUnitTask(
                statistic_id, new_unit_of_measurement, old_unit_of_measurement
            )
        )

    @callback
    def async_import_statistics(
        self,
        metadata: StatisticMetaData,
        stats: Iterable[StatisticData],
        table: type[Statistics | StatisticsShortTerm],
    ) -> None:
        """Schedule import of statistics."""
        self.queue_task(ImportStatisticsTask(metadata, stats, table))

    @callback
    def _async_setup_periodic_tasks(self) -> None:
        """Prepare periodic tasks."""
        if self.hass.is_stopping or not self._get_session:
            # Home Assistant is shutting down
            return

        # If the db is using a socket connection, we need to keep alive
        # to prevent errors from unexpected disconnects
        if self.dialect_name != SupportedDialect.SQLITE:
            self._keep_alive_listener = async_track_time_interval(
                self.hass, self._async_keep_alive, timedelta(seconds=KEEPALIVE_TIME)
            )

        # If the commit interval is not 0, we need to commit periodically
        if self.commit_interval:
            self._commit_listener = async_track_time_interval(
                self.hass, self._async_commit, timedelta(seconds=self.commit_interval)
            )

        # Run nightly tasks at 4:12am
        self._nightly_listener = async_track_time_change(
            self.hass, self.async_nightly_tasks, hour=4, minute=12, second=0
        )

        # Compile short term statistics every 5 minutes
        self._periodic_listener = async_track_utc_time_change(
            self.hass, self._async_five_minute_tasks, minute=range(0, 60, 5), second=10
        )

    async def _async_wait_for_started(self) -> object | None:
        """Wait for the hass started future."""
        return await self._hass_started

    def _wait_startup_or_shutdown(self) -> object | None:
        """Wait for startup or shutdown before starting."""
        try:
            return asyncio.run_coroutine_threadsafe(
                self._async_wait_for_started(), self.hass.loop
            ).result()
        except CancelledError as ex:
            _LOGGER.warning(
                "Recorder startup was externally canceled before it could complete: %s",
                ex,
            )
            return SHUTDOWN_TASK

    def run(self) -> None:
        """Start processing events to save."""
        self.thread_id = threading.get_ident()
        setup_result = self._setup_recorder()

        if not setup_result:
            # Give up if we could not connect
            self.hass.add_job(self.async_connection_failed)
            return

        schema_status = migration.validate_db_schema(self.hass, self, self.get_session)
        if schema_status is None:
            # Give up if we could not validate the schema
            self.hass.add_job(self.async_connection_failed)
            return
        self.schema_version = schema_status.current_version

        if schema_status.valid:
            self._setup_run()
        else:
            self.migration_in_progress = True
            self.migration_is_live = migration.live_migration(schema_status)

        self.hass.add_job(self.async_connection_success)
        database_was_ready = self.migration_is_live or schema_status.valid

        if database_was_ready:
            # If the migrate is live or the schema is valid, we need to
            # wait for startup to complete. If its not live, we need to continue
            # on.
            self._activate_and_set_db_ready()

            # We wait to start a live migration until startup has finished
            # since it can be cpu intensive and we do not want it to compete
            # with startup which is also cpu intensive
            if self._wait_startup_or_shutdown() is SHUTDOWN_TASK:
                # Shutdown happened before Home Assistant finished starting
                self.migration_in_progress = False
                # Make sure we cleanly close the run if
                # we restart before startup finishes
                self._shutdown()
                return

        if not schema_status.valid:
            if self._migrate_schema_and_setup_run(schema_status):
                self.schema_version = SCHEMA_VERSION
                if not self._event_listener:
                    # If the schema migration takes so long that the end
                    # queue watcher safety kicks in because MAX_QUEUE_BACKLOG
                    # is reached, we need to reinitialize the listener.
                    self.hass.add_job(self.async_initialize)
            else:
                persistent_notification.create(
                    self.hass,
                    "The database migration failed, check [the logs](/config/logs).",
                    "Database Migration Failed",
                    "recorder_database_migration",
                )
                self.hass.add_job(self.async_set_db_ready)
                self._shutdown()
                return

        if not database_was_ready:
            self._activate_and_set_db_ready()

        # Catch up with missed statistics
        self._schedule_compile_missing_statistics()
        _LOGGER.debug("Recorder processing the queue")
        self._adjust_lru_size()
        self.hass.add_job(self._async_set_recorder_ready_migration_done)
        self._run_event_loop()
        self._shutdown()

    def _activate_and_set_db_ready(self) -> None:
        """Activate the table managers or schedule migrations and mark the db as ready."""
        with session_scope(session=self.get_session(), read_only=True) as session:
            # Prime the statistics meta manager as soon as possible
            # since we want the frontend queries to avoid a thundering
            # herd of queries to find the statistics meta data if
            # there are a lot of statistics graphs on the frontend.
            if self.schema_version >= STATISTICS_ROWS_SCHEMA_VERSION:
                self.statistics_meta_manager.load(session)

            if (
                self.schema_version < CONTEXT_ID_AS_BINARY_SCHEMA_VERSION
                or execute_stmt_lambda_element(
                    session, has_states_context_ids_to_migrate()
                )
            ):
                self.queue_task(StatesContextIDMigrationTask())

            if (
                self.schema_version < CONTEXT_ID_AS_BINARY_SCHEMA_VERSION
                or execute_stmt_lambda_element(
                    session, has_events_context_ids_to_migrate()
                )
            ):
                self.queue_task(EventsContextIDMigrationTask())

            if (
                self.schema_version < EVENT_TYPE_IDS_SCHEMA_VERSION
                or execute_stmt_lambda_element(session, has_event_type_to_migrate())
            ):
                self.queue_task(EventTypeIDMigrationTask())
            else:
                _LOGGER.debug("Activating event_types manager as all data is migrated")
                self.event_type_manager.active = True

            if (
                self.schema_version < STATES_META_SCHEMA_VERSION
                or execute_stmt_lambda_element(session, has_entity_ids_to_migrate())
            ):
                self.queue_task(EntityIDMigrationTask())
            else:
                _LOGGER.debug("Activating states_meta manager as all data is migrated")
                self.states_meta_manager.active = True

            if self.schema_version > LEGACY_STATES_EVENT_ID_INDEX_SCHEMA_VERSION:
                with contextlib.suppress(SQLAlchemyError):
                    # If the index of event_ids on the states table is still present
                    # we need to queue a task to remove it.
                    if get_index_by_name(
                        session, TABLE_STATES, LEGACY_STATES_EVENT_ID_INDEX
                    ):
                        self.queue_task(EventIdMigrationTask())
                        self.use_legacy_events_index = True

        # We must only set the db ready after we have set the table managers
        # to active if there is no data to migrate.
        #
        # This ensures that the history queries will use the new tables
        # and not the old ones as soon as the API is available.
        self.hass.add_job(self.async_set_db_ready)

    def _run_event_loop(self) -> None:
        """Run the event loop for the recorder."""
        # Use a session for the event read loop
        # with a commit every time the event time
        # has changed. This reduces the disk io.
        queue_ = self._queue
        startup_tasks: list[RecorderTask] = []
        while not queue_.empty() and (task := queue_.get_nowait()):
            startup_tasks.append(task)
        self._pre_process_startup_tasks(startup_tasks)
        for task in startup_tasks:
            self._guarded_process_one_task_or_recover(task)

        # Clear startup tasks since this thread runs forever
        # and we don't want to hold them in memory
        del startup_tasks

        self.stop_requested = False
        while not self.stop_requested:
            self._guarded_process_one_task_or_recover(queue_.get())

    def _pre_process_startup_tasks(self, startup_tasks: list[RecorderTask]) -> None:
        """Pre process startup tasks."""
        # Prime all the state_attributes and event_data caches
        # before we start processing events
        state_change_events: list[Event] = []
        non_state_change_events: list[Event] = []

        for task in startup_tasks:
            if isinstance(task, EventTask):
                event_ = task.event
                if event_.event_type == EVENT_STATE_CHANGED:
                    state_change_events.append(event_)
                else:
                    non_state_change_events.append(event_)

        assert self.event_session is not None
        session = self.event_session
        self.event_data_manager.load(non_state_change_events, session)
        self.event_type_manager.load(non_state_change_events, session)
        self.states_meta_manager.load(state_change_events, session)
        self.state_attributes_manager.load(state_change_events, session)

    def _guarded_process_one_task_or_recover(self, task: RecorderTask) -> None:
        """Process a task, guarding against exceptions to ensure the loop does not collapse."""
        _LOGGER.debug("Processing task: %s", task)
        try:
            self._process_one_task_or_recover(task)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Error while processing event %s: %s", task, err)

    def _process_one_task_or_recover(self, task: RecorderTask) -> None:
        """Process an event, reconnect, or recover a malformed database."""
        try:
            # If its not an event, commit everything
            # that is pending before running the task
            if task.commit_before:
                self._commit_event_session_or_retry()
            return task.run(self)
        except exc.DatabaseError as err:
            if self._handle_database_error(err):
                return
            _LOGGER.exception(
                "Unhandled database error while processing task %s: %s", task, err
            )
        except SQLAlchemyError as err:
            _LOGGER.exception("SQLAlchemyError error processing task %s: %s", task, err)

        # Reset the session if an SQLAlchemyError (including DatabaseError)
        # happens to rollback and recover
        self._reopen_event_session()

    def _setup_recorder(self) -> bool:
        """Create a connection to the database."""
        tries = 1

        while tries <= self.db_max_retries:
            try:
                self._setup_connection()
                return migration.initialize_database(self.get_session)
            except UnsupportedDialect:
                break
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "Error during connection setup: %s (retrying in %s seconds)",
                    err,
                    self.db_retry_wait,
                )
            tries += 1
            time.sleep(self.db_retry_wait)

        return False

    @callback
    def _async_migration_started(self) -> None:
        """Set the migration started event."""
        self.async_migration_event.set()

    def _migrate_schema_and_setup_run(
        self, schema_status: migration.SchemaValidationStatus
    ) -> bool:
        """Migrate schema to the latest version."""
        persistent_notification.create(
            self.hass,
            (
                "System performance will temporarily degrade during the database"
                " upgrade. Do not power down or restart the system until the upgrade"
                " completes. Integrations that read the database, such as logbook,"
                " history, and statistics may return inconsistent results until the "
                " upgrade completes. This notification will be automatically dismissed"
                " when the upgrade completes."
            ),
            "Database upgrade in progress",
            "recorder_database_migration",
        )
        self.hass.add_job(self._async_migration_started)

        try:
            assert self.engine is not None
            migration.migrate_schema(
                self, self.hass, self.engine, self.get_session, schema_status
            )
        except exc.DatabaseError as err:
            if self._handle_database_error(err):
                return True
            _LOGGER.exception("Database error during schema migration")
            return False
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Error during schema migration")
            return False
        else:
            self._setup_run()
            return True
        finally:
            self.migration_in_progress = False
            persistent_notification.dismiss(self.hass, "recorder_database_migration")

    def _lock_database(self, task: DatabaseLockTask) -> None:
        @callback
        def _async_set_database_locked(task: DatabaseLockTask) -> None:
            task.database_locked.set()

        with write_lock_db_sqlite(self):
            # Notify that lock is being held, wait until database can be used again.
            self.hass.add_job(_async_set_database_locked, task)
            while not task.database_unlock.wait(timeout=DB_LOCK_QUEUE_CHECK_TIMEOUT):
                if self.backlog > MAX_QUEUE_BACKLOG * 0.9:
                    _LOGGER.warning(
                        "Database queue backlog reached more than 90% of maximum queue "
                        "length while waiting for backup to finish; recorder will now "
                        "resume writing to database. The backup cannot be trusted and "
                        "must be restarted"
                    )
                    task.queue_overflow = True
                    break
        _LOGGER.info(
            "Database queue backlog reached %d entries during backup",
            self.backlog,
        )

    def _process_one_event(self, event: Event) -> None:
        if not self.enabled:
            return
        if event.event_type == EVENT_STATE_CHANGED:
            self._process_state_changed_event_into_session(event)
        else:
            self._process_non_state_changed_event_into_session(event)
        # Commit if the commit interval is zero
        if not self.commit_interval:
            self._commit_event_session_or_retry()

    def _process_non_state_changed_event_into_session(self, event: Event) -> None:
        """Process any event into the session except state changed."""
        session = self.event_session
        assert session is not None
        dbevent = Events.from_event(event)

        # Map the event_type to the EventTypes table
        event_type_manager = self.event_type_manager
        if pending_event_types := event_type_manager.get_pending(event.event_type):
            dbevent.event_type_rel = pending_event_types
        elif event_type_id := event_type_manager.get(event.event_type, session):
            dbevent.event_type_id = event_type_id
        else:
            event_types = EventTypes(event_type=event.event_type)
            event_type_manager.add_pending(event_types)
            session.add(event_types)
            dbevent.event_type_rel = event_types

        if not event.data:
            session.add(dbevent)
            return

        event_data_manager = self.event_data_manager
        if not (shared_data_bytes := event_data_manager.serialize_from_event(event)):
            return

        # Map the event data to the EventData table
        shared_data = shared_data_bytes.decode("utf-8")
        # Matching attributes found in the pending commit
        if pending_event_data := event_data_manager.get_pending(shared_data):
            dbevent.event_data_rel = pending_event_data
        # Matching attributes id found in the cache
        elif (data_id := event_data_manager.get_from_cache(shared_data)) or (
            (hash_ := EventData.hash_shared_data_bytes(shared_data_bytes))
            and (data_id := event_data_manager.get(shared_data, hash_, session))
        ):
            dbevent.data_id = data_id
        else:
            # No matching attributes found, save them in the DB
            dbevent_data = EventData(shared_data=shared_data, hash=hash_)
            event_data_manager.add_pending(dbevent_data)
            session.add(dbevent_data)
            dbevent.event_data_rel = dbevent_data

        session.add(dbevent)

    def _process_state_changed_event_into_session(self, event: Event) -> None:
        """Process a state_changed event into the session."""
        state_attributes_manager = self.state_attributes_manager
        states_meta_manager = self.states_meta_manager
        entity_removed = not event.data.get("new_state")
        entity_id = event.data["entity_id"]

        dbstate = States.from_event(event)

        states_manager = self.states_manager
        if old_state := states_manager.pop_pending(entity_id):
            dbstate.old_state = old_state
        elif old_state_id := states_manager.pop_committed(entity_id):
            dbstate.old_state_id = old_state_id
        if entity_removed:
            dbstate.state = None
        else:
            states_manager.add_pending(entity_id, dbstate)

        if states_meta_manager.active:
            dbstate.entity_id = None

        if entity_id is None or not (
            shared_attrs_bytes := state_attributes_manager.serialize_from_event(event)
        ):
            return

        assert self.event_session is not None
        session = self.event_session
        # Map the entity_id to the StatesMeta table
        if pending_states_meta := states_meta_manager.get_pending(entity_id):
            dbstate.states_meta_rel = pending_states_meta
        elif metadata_id := states_meta_manager.get(entity_id, session, True):
            dbstate.metadata_id = metadata_id
        elif states_meta_manager.active and entity_removed:
            # If the entity was removed, we don't need to add it to the
            # StatesMeta table or record it in the pending commit
            # if it does not have a metadata_id allocated to it as
            # it either never existed or was just renamed.
            return
        else:
            states_meta = StatesMeta(entity_id=entity_id)
            states_meta_manager.add_pending(states_meta)
            session.add(states_meta)
            dbstate.states_meta_rel = states_meta

        # Map the event data to the StateAttributes table
        shared_attrs = shared_attrs_bytes.decode("utf-8")
        dbstate.attributes = None
        # Matching attributes found in the pending commit
        if pending_event_data := state_attributes_manager.get_pending(shared_attrs):
            dbstate.state_attributes = pending_event_data
        # Matching attributes id found in the cache
        elif (
            attributes_id := state_attributes_manager.get_from_cache(shared_attrs)
        ) or (
            (hash_ := StateAttributes.hash_shared_attrs_bytes(shared_attrs_bytes))
            and (
                attributes_id := state_attributes_manager.get(
                    shared_attrs, hash_, session
                )
            )
        ):
            dbstate.attributes_id = attributes_id
        else:
            # No matching attributes found, save them in the DB
            dbstate_attributes = StateAttributes(shared_attrs=shared_attrs, hash=hash_)
            state_attributes_manager.add_pending(dbstate_attributes)
            session.add(dbstate_attributes)
            dbstate.state_attributes = dbstate_attributes

        session.add(dbstate)

    def _handle_database_error(self, err: Exception) -> bool:
        """Handle a database error that may result in moving away the corrupt db."""
        if isinstance(err.__cause__, sqlite3.DatabaseError):
            _LOGGER.exception(
                "Unrecoverable sqlite3 database corruption detected: %s", err
            )
            self._handle_sqlite_corruption()
            return True
        return False

    def _event_session_has_pending_writes(self) -> bool:
        """Return True if there are pending writes in the event session."""
        session = self.event_session
        return bool(session and (session.new or session.dirty))

    def _commit_event_session_or_retry(self) -> None:
        """Commit the event session if there is work to do."""
        if not self._event_session_has_pending_writes():
            return
        tries = 1
        while tries <= self.db_max_retries:
            try:
                self._commit_event_session()
                return
            except (exc.InternalError, exc.OperationalError) as err:
                _LOGGER.error(
                    "%s: Error executing query: %s. (retrying in %s seconds)",
                    INVALIDATED_ERR if err.connection_invalidated else CONNECTIVITY_ERR,
                    err,
                    self.db_retry_wait,
                )
                if tries == self.db_max_retries:
                    raise

                tries += 1
                time.sleep(self.db_retry_wait)

    def _commit_event_session(self) -> None:
        assert self.event_session is not None
        session = self.event_session
        self._commits_without_expire += 1

        session.commit()
        # We just committed the state attributes to the database
        # and we now know the attributes_ids.  We can save
        # many selects for matching attributes by loading them
        # into the LRU or committed now.
        self.states_manager.post_commit_pending()
        self.state_attributes_manager.post_commit_pending()
        self.event_data_manager.post_commit_pending()
        self.event_type_manager.post_commit_pending()
        self.states_meta_manager.post_commit_pending()

        # Expire is an expensive operation (frequently more expensive
        # than the flush and commit itself) so we only
        # do it after EXPIRE_AFTER_COMMITS commits
        if self._commits_without_expire >= EXPIRE_AFTER_COMMITS:
            self._commits_without_expire = 0
            session.expire_all()

    def _handle_sqlite_corruption(self) -> None:
        """Handle the sqlite3 database being corrupt."""
        try:
            self._close_event_session()
        finally:
            self._close_connection()
        move_away_broken_database(dburl_to_path(self.db_url))
        self.recorder_runs_manager.reset()
        self._setup_recorder()
        self._setup_run()

    def _close_event_session(self) -> None:
        """Close the event session."""
        self.states_manager.reset()
        self.state_attributes_manager.reset()
        self.event_data_manager.reset()
        self.event_type_manager.reset()
        self.states_meta_manager.reset()
        self.statistics_meta_manager.reset()

        if not self.event_session:
            return

        try:
            self.event_session.rollback()
            self.event_session.close()
        except SQLAlchemyError as err:
            _LOGGER.exception(
                "Error while rolling back and closing the event session: %s", err
            )

    def _reopen_event_session(self) -> None:
        """Rollback the event session and reopen it after a failure."""
        self._close_event_session()
        self._open_event_session()

    def _open_event_session(self) -> None:
        """Open the event session."""
        self.event_session = self.get_session()
        self.event_session.expire_on_commit = False

    def _post_schema_migration(self, old_version: int, new_version: int) -> None:
        """Run post schema migration tasks."""
        migration.post_schema_migration(self, old_version, new_version)

    def _migrate_states_context_ids(self) -> bool:
        """Migrate states context ids if needed."""
        return migration.migrate_states_context_ids(self)

    def _migrate_events_context_ids(self) -> bool:
        """Migrate events context ids if needed."""
        return migration.migrate_events_context_ids(self)

    def _migrate_event_type_ids(self) -> bool:
        """Migrate event type ids if needed."""
        return migration.migrate_event_type_ids(self)

    def _migrate_entity_ids(self) -> bool:
        """Migrate entity_ids if needed."""
        return migration.migrate_entity_ids(self)

    def _post_migrate_entity_ids(self) -> bool:
        """Post migrate entity_ids if needed."""
        return migration.post_migrate_entity_ids(self)

    def _cleanup_legacy_states_event_ids(self) -> bool:
        """Cleanup legacy event_ids if needed."""
        return migration.cleanup_legacy_states_event_ids(self)

    def _send_keep_alive(self) -> None:
        """Send a keep alive to keep the db connection open."""
        assert self.event_session is not None
        _LOGGER.debug("Sending keepalive")
        self.event_session.connection().scalar(select(1))

    @callback
    def event_listener(self, event: Event) -> None:
        """Listen for new events and put them in the process queue."""
        if self._async_event_filter(event):
            self.queue_task(EventTask(event))

    async def async_block_till_done(self) -> None:
        """Async version of block_till_done."""
        if self._queue.empty() and not self._event_session_has_pending_writes():
            return
        event = asyncio.Event()
        self.queue_task(SynchronizeTask(event))
        await event.wait()

    def block_till_done(self) -> None:
        """Block till all events processed.

        This is only called in tests.

        This only blocks until the queue is empty
        which does not mean the recorder is done.

        Call tests.common's wait_recording_done
        after calling this to ensure the data
        is in the database.
        """
        self._queue_watch.clear()
        self.queue_task(WAIT_TASK)
        self._queue_watch.wait()

    async def lock_database(self) -> bool:
        """Lock database so it can be backed up safely."""
        if self.dialect_name != SupportedDialect.SQLITE:
            _LOGGER.debug(
                "Not a SQLite database or not connected, locking not necessary"
            )
            return True

        if self._database_lock_task:
            _LOGGER.warning("Database already locked")
            return False

        database_locked = asyncio.Event()
        task = DatabaseLockTask(database_locked, threading.Event(), False)
        self.queue_task(task)
        try:
            async with async_timeout.timeout(DB_LOCK_TIMEOUT):
                await database_locked.wait()
        except asyncio.TimeoutError as err:
            task.database_unlock.set()
            raise TimeoutError(
                f"Could not lock database within {DB_LOCK_TIMEOUT} seconds."
            ) from err
        self._database_lock_task = task
        return True

    @callback
    def unlock_database(self) -> bool:
        """Unlock database.

        Returns true if database lock has been held throughout the process.
        """
        if self.dialect_name != SupportedDialect.SQLITE:
            _LOGGER.debug(
                "Not a SQLite database or not connected, unlocking not necessary"
            )
            return True

        if not self._database_lock_task:
            _LOGGER.warning("Database currently not locked")
            return False

        self._database_lock_task.database_unlock.set()
        success = not self._database_lock_task.queue_overflow

        self._database_lock_task = None

        return success

    def _setup_connection(self) -> None:
        """Ensure database is ready to fly."""
        kwargs: dict[str, Any] = {}
        self._completed_first_database_setup = False

        def setup_recorder_connection(
            dbapi_connection: Any, connection_record: Any
        ) -> None:
            """Dbapi specific connection settings."""
            assert self.engine is not None
            if database_engine := setup_connection_for_dialect(
                self,
                self.engine.dialect.name,
                dbapi_connection,
                not self._completed_first_database_setup,
            ):
                self.database_engine = database_engine
            self._completed_first_database_setup = True

        if self.db_url == SQLITE_URL_PREFIX or ":memory:" in self.db_url:
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = MutexPool
            MutexPool.pool_lock = threading.RLock()
            kwargs["pool_reset_on_return"] = None
        elif self.db_url.startswith(SQLITE_URL_PREFIX):
            kwargs["poolclass"] = RecorderPool
        elif self.db_url.startswith(
            (
                MARIADB_URL_PREFIX,
                MARIADB_PYMYSQL_URL_PREFIX,
                MYSQLDB_URL_PREFIX,
                MYSQLDB_PYMYSQL_URL_PREFIX,
            )
        ):
            kwargs["connect_args"] = {"charset": "utf8mb4"}
            if self.db_url.startswith((MARIADB_URL_PREFIX, MYSQLDB_URL_PREFIX)):
                # If they have configured MySQLDB but don't have
                # the MySQLDB module installed this will throw
                # an ImportError which we suppress here since
                # sqlalchemy will give them a better error when
                # it tried to import it below.
                with contextlib.suppress(ImportError):
                    kwargs["connect_args"]["conv"] = build_mysqldb_conv()

        # Disable extended logging for non SQLite databases
        if not self.db_url.startswith(SQLITE_URL_PREFIX):
            kwargs["echo"] = False

        if self._using_file_sqlite:
            validate_or_move_away_sqlite_database(self.db_url)

        self.engine = create_engine(self.db_url, **kwargs, future=True)
        self._dialect_name = try_parse_enum(SupportedDialect, self.engine.dialect.name)
        sqlalchemy_event.listen(self.engine, "connect", setup_recorder_connection)

        Base.metadata.create_all(self.engine)
        self._get_session = scoped_session(sessionmaker(bind=self.engine, future=True))
        _LOGGER.debug("Connected to recorder database")

    def _close_connection(self) -> None:
        """Close the connection."""
        assert self.engine is not None
        self.engine.dispose()
        self.engine = None
        self._get_session = None

    def _setup_run(self) -> None:
        """Log the start of the current run and schedule any needed jobs."""
        with session_scope(session=self.get_session()) as session:
            end_incomplete_runs(session, self.recorder_runs_manager.recording_start)
            self.recorder_runs_manager.start(session)

        self._open_event_session()

    def _schedule_compile_missing_statistics(self) -> None:
        """Add tasks for missing statistics runs."""
        self.queue_task(CompileMissingStatisticsTask())

    def _end_session(self) -> None:
        """End the recorder session."""
        if self.event_session is None:
            return
        if self.recorder_runs_manager.active:
            self.recorder_runs_manager.end(self.event_session)
        try:
            self._commit_event_session_or_retry()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Error saving the event session during shutdown: %s", err)

        self.event_session.close()
        self.recorder_runs_manager.clear()

    def _shutdown(self) -> None:
        """Save end time for current run."""
        _LOGGER.debug("Shutting down recorder")
        self.hass.add_job(self._async_stop_listeners)
        self._stop_executor()
        try:
            self._end_session()
        finally:
            self._close_connection()
