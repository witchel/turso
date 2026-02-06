//! Parallel Write Benchmarks
//!
//! Measures concurrent write performance across multiple dimensions:
//! - Writer scalability (1 to 16 writers)
//! - Write contention patterns (separate tables, disjoint keys, overlapping, hot rows)
//! - Mixed read-write workload (YCSB-B style: 95% reads, 5% writes)
//! - Write burst (thundering herd with simultaneous commit)
//!
//! Run with: cargo bench --bench parallel_write_benchmark

#[cfg(not(feature = "codspeed"))]
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};
#[cfg(not(feature = "codspeed"))]
use pprof::criterion::{Output, PProfProfiler};

#[cfg(feature = "codspeed")]
use codspeed_criterion_compat::{
    criterion_group, criterion_main, BenchmarkId, Criterion, Throughput,
};

use std::sync::{Arc, Barrier};
use std::time::{Duration, Instant};
use tempfile::TempDir;
use turso_core::{Database, LimboError, PlatformIO, StepResult};

#[cfg(not(target_family = "wasm"))]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/// Setup a turso database in WAL mode.
fn setup_limbo(temp_dir: &TempDir, schema: &str) -> Arc<Database> {
    let db_path = temp_dir.path().join("bench.db");
    #[allow(clippy::arc_with_non_send_sync)]
    let io = Arc::new(PlatformIO::new().unwrap());
    let db = Database::open_file(io, db_path.to_str().unwrap()).unwrap();
    let conn = db.connect().unwrap();
    conn.execute(schema).unwrap();
    db
}

/// Setup a turso database in experimental MVCC mode.
fn setup_limbo_mvcc(temp_dir: &TempDir, schema: &str) -> Arc<Database> {
    let db_path = temp_dir.path().join("bench.db");
    #[allow(clippy::arc_with_non_send_sync)]
    let io = Arc::new(PlatformIO::new().unwrap());
    let db = Database::open_file(io, db_path.to_str().unwrap()).unwrap();
    let conn = db.connect().unwrap();
    conn.execute("PRAGMA journal_mode = 'experimental_mvcc'")
        .unwrap();
    conn.execute(schema).unwrap();
    db
}

/// Setup a rusqlite database in WAL mode.
fn setup_rusqlite(temp_dir: &TempDir, schema: &str) -> rusqlite::Connection {
    let db_path = temp_dir.path().join("bench.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.pragma_update(None, "synchronous", "FULL").unwrap();
    conn.pragma_update(None, "journal_mode", "WAL").unwrap();
    conn.execute_batch(schema).unwrap();
    conn
}

/// Generate a batch INSERT statement: `INSERT INTO <table> (id, data) VALUES (start, ...), ...`
fn generate_batch_insert(table: &str, start: i64, count: usize) -> String {
    let mut sql = format!("INSERT INTO {table} (id, data) VALUES ");
    for i in 0..count {
        if i > 0 {
            sql.push(',');
        }
        let id = start + i as i64;
        sql.push_str(&format!("({id}, 'v{id}')"));
    }
    sql
}

/// Generate per-connection insert batches with non-overlapping key ranges.
fn generate_inserts_per_connection(
    table: &str,
    num_connections: usize,
    num_batches: usize,
    rows_per_batch: usize,
) -> Vec<Vec<String>> {
    let mut all = Vec::with_capacity(num_connections);
    for conn_idx in 0..num_connections {
        let mut batches = Vec::with_capacity(num_batches);
        for batch_idx in 0..num_batches {
            let start =
                (conn_idx * num_batches * rows_per_batch + batch_idx * rows_per_batch) as i64;
            batches.push(generate_batch_insert(table, start, rows_per_batch));
        }
        all.push(batches);
    }
    all
}

// ---------------------------------------------------------------------------
// Cooperative (single-thread, round-robin) scheduling helpers
// ---------------------------------------------------------------------------

/// Run cooperative WAL writes: single thread, round-robin stepping across connections.
fn run_cooperative_wal(num_connections: usize, num_batches: usize, rows_per_batch: usize) {
    struct ConnState {
        conn: Arc<turso_core::Connection>,
        inserts: Vec<String>,
        current_statement: Option<turso_core::Statement>,
    }

    let temp_dir = tempfile::tempdir().unwrap();
    let db = setup_limbo(
        &temp_dir,
        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
    );

    let all_inserts =
        generate_inserts_per_connection("test", num_connections, num_batches, rows_per_batch);

    let mut connections: Vec<ConnState> = (0..num_connections)
        .map(|i| ConnState {
            conn: db.connect().unwrap(),
            inserts: all_inserts[i].clone(),
            current_statement: None,
        })
        .collect();

    loop {
        let all_finished = connections
            .iter()
            .all(|c| c.inserts.is_empty() && c.current_statement.is_none());
        if all_finished {
            break;
        }

        for conn in connections.iter_mut() {
            if conn.current_statement.is_none() && !conn.inserts.is_empty() {
                let sql = conn.inserts.remove(0);
                conn.current_statement = Some(conn.conn.prepare(&sql).unwrap());
            }
            let Some(stmt) = conn.current_statement.as_mut() else {
                continue;
            };
            match stmt.step().unwrap() {
                StepResult::Done => {
                    conn.current_statement = None;
                }
                StepResult::IO => {} // batch IO below
                StepResult::Busy => {
                    stmt.reset();
                }
                _ => unreachable!(),
            }
        }
        db.io.step().unwrap();
    }
}

/// Run cooperative MVCC writes: single thread, round-robin with BEGIN CONCURRENT per batch.
fn run_cooperative_mvcc(num_connections: usize, num_batches: usize, rows_per_batch: usize) {
    struct ConnState {
        conn: Arc<turso_core::Connection>,
        inserts: Vec<String>,
        current_statement: Option<turso_core::Statement>,
        current_insert: Option<String>,
    }

    let temp_dir = tempfile::tempdir().unwrap();
    let db = setup_limbo_mvcc(
        &temp_dir,
        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
    );

    let all_inserts =
        generate_inserts_per_connection("test", num_connections, num_batches, rows_per_batch);

    let mut connections: Vec<ConnState> = (0..num_connections)
        .map(|i| ConnState {
            conn: db.connect().unwrap(),
            inserts: all_inserts[i].clone(),
            current_statement: None,
            current_insert: None,
        })
        .collect();

    loop {
        let all_finished = connections
            .iter()
            .all(|c| c.inserts.is_empty() && c.current_statement.is_none());
        if all_finished {
            break;
        }

        for conn in connections.iter_mut() {
            if conn.current_statement.is_none() && !conn.inserts.is_empty() {
                let sql = conn.inserts.remove(0);
                conn.conn.execute("BEGIN CONCURRENT").unwrap();
                conn.current_statement = Some(conn.conn.prepare(&sql).unwrap());
                conn.current_insert = Some(sql);
            }
            let Some(stmt) = conn.current_statement.as_mut() else {
                continue;
            };
            let is_commit = stmt.get_sql() == "COMMIT";
            match stmt.step() {
                Ok(StepResult::Done) => {
                    if is_commit {
                        conn.current_statement = None;
                        conn.current_insert = None;
                    } else {
                        conn.current_statement = Some(conn.conn.prepare("COMMIT").unwrap());
                    }
                }
                Ok(StepResult::IO) => {}
                Ok(StepResult::Busy) => {
                    stmt.reset();
                }
                Err(LimboError::SchemaUpdated) => {
                    conn.current_statement = Some(
                        conn.conn
                            .prepare(conn.current_insert.as_ref().unwrap())
                            .unwrap(),
                    );
                }
                Err(e) => panic!("unexpected error: {e:?}"),
                _ => unreachable!(),
            }
        }
        db.io.step().unwrap();
    }
}

// ---------------------------------------------------------------------------
// Threaded helpers
// ---------------------------------------------------------------------------

/// Execute SQL on a connection with busy-retry loop.
fn execute_with_retry(conn: &Arc<turso_core::Connection>, sql: &str) {
    loop {
        match conn.execute(sql) {
            Ok(()) => break,
            Err(turso_core::LimboError::Busy) => {
                std::thread::yield_now();
            }
            Err(e) => panic!("execute error: {e:?}"),
        }
    }
}

/// Execute a full MVCC transaction (BEGIN CONCURRENT + sql + COMMIT) with retry on
/// WriteWriteConflict and Busy errors.
fn execute_mvcc_with_retry(conn: &Arc<turso_core::Connection>, sql: &str) {
    loop {
        conn.execute("BEGIN CONCURRENT").unwrap();
        match conn.execute(sql) {
            Ok(()) => {}
            Err(turso_core::LimboError::WriteWriteConflict) | Err(turso_core::LimboError::Busy) => {
                let _ = conn.execute("ROLLBACK");
                std::thread::yield_now();
                continue;
            }
            Err(e) => panic!("execute error during INSERT: {e:?}"),
        }
        match conn.execute("COMMIT") {
            Ok(()) => break,
            Err(turso_core::LimboError::WriteWriteConflict) | Err(turso_core::LimboError::Busy) => {
                let _ = conn.execute("ROLLBACK");
                std::thread::yield_now();
                continue;
            }
            Err(e) => panic!("execute error during COMMIT: {e:?}"),
        }
    }
}

/// Run threaded WAL writes: each writer on its own OS thread.
/// Uses a table without PRIMARY KEY to avoid UNIQUE constraint failures on BUSY retries,
/// since WAL auto-commit doesn't guarantee atomicity for multi-row INSERTs.
fn run_threaded_wal(num_connections: usize, num_batches: usize, rows_per_batch: usize) {
    let temp_dir = tempfile::tempdir().unwrap();
    let db = setup_limbo(&temp_dir, "CREATE TABLE test (id INTEGER, data TEXT)");
    let all_inserts =
        generate_inserts_per_connection("test", num_connections, num_batches, rows_per_batch);

    let barrier = Arc::new(Barrier::new(num_connections));
    let mut handles = Vec::with_capacity(num_connections);

    for inserts in all_inserts {
        let db = db.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            let conn = db.connect().unwrap();
            barrier.wait();
            for sql in &inserts {
                execute_with_retry(&conn, sql);
            }
        }));
    }

    for h in handles {
        h.join().unwrap();
    }
}

/// Run threaded MVCC writes: each writer on its own OS thread with BEGIN CONCURRENT.
fn run_threaded_mvcc(num_connections: usize, num_batches: usize, rows_per_batch: usize) {
    let temp_dir = tempfile::tempdir().unwrap();
    let db = setup_limbo_mvcc(
        &temp_dir,
        "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
    );
    let all_inserts =
        generate_inserts_per_connection("test", num_connections, num_batches, rows_per_batch);

    let barrier = Arc::new(Barrier::new(num_connections));
    let mut handles = Vec::with_capacity(num_connections);

    for inserts in all_inserts {
        let db = db.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            let conn = db.connect().unwrap();
            barrier.wait();
            for sql in &inserts {
                execute_mvcc_with_retry(&conn, sql);
            }
        }));
    }

    for h in handles {
        h.join().unwrap();
    }
}

// ---------------------------------------------------------------------------
// Benchmark 1: Writer Scalability
// ---------------------------------------------------------------------------

fn bench_writer_scalability(criterion: &mut Criterion) {
    let enable_rusqlite =
        std::env::var("DISABLE_RUSQLITE_BENCHMARK").is_err() && !cfg!(feature = "codspeed");

    let writer_counts: &[usize] = &[1, 2, 4, 8, 16];
    let num_batches = 5;
    let rows_per_batch = 10;

    let mut group = criterion.benchmark_group("Writer Scalability");

    for &writers in writer_counts {
        let total_rows = (writers * num_batches * rows_per_batch) as u64;
        group.throughput(Throughput::Elements(total_rows));

        // Cooperative modes cap at 8 writers to avoid excessive round-robin overhead
        if writers <= 8 {
            // WAL cooperative
            group.bench_function(BenchmarkId::new("limbo_wal_cooperative", writers), |b| {
                b.iter(|| run_cooperative_wal(writers, num_batches, rows_per_batch));
            });

            // MVCC cooperative
            group.bench_function(BenchmarkId::new("limbo_mvcc_cooperative", writers), |b| {
                b.iter(|| run_cooperative_mvcc(writers, num_batches, rows_per_batch));
            });
        }

        // WAL threaded
        group.bench_function(BenchmarkId::new("limbo_wal_threaded", writers), |b| {
            b.iter(|| run_threaded_wal(writers, num_batches, rows_per_batch));
        });

        // MVCC threaded
        group.bench_function(BenchmarkId::new("limbo_mvcc_threaded", writers), |b| {
            b.iter(|| run_threaded_mvcc(writers, num_batches, rows_per_batch));
        });

        // SQLite WAL sequential baseline
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal_sequential", writers), |b| {
                b.iter(|| {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let all_inserts = generate_inserts_per_connection(
                        "test",
                        writers,
                        num_batches,
                        rows_per_batch,
                    );
                    {
                        let conn = setup_rusqlite(
                            &temp_dir,
                            "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)",
                        );
                        drop(conn);
                    }
                    let db_path = temp_dir.path().join("bench.db");
                    for inserts in &all_inserts {
                        let conn = rusqlite::Connection::open(&db_path).unwrap();
                        for sql in inserts {
                            conn.execute(sql, []).unwrap();
                        }
                    }
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 2: Write Contention Patterns
// ---------------------------------------------------------------------------

/// Run cooperative MVCC writes with per-connection table+insert generators.
fn run_cooperative_mvcc_custom(schema: &str, per_conn_inserts: Vec<Vec<String>>) {
    struct ConnState {
        conn: Arc<turso_core::Connection>,
        inserts: Vec<String>,
        current_statement: Option<turso_core::Statement>,
        current_insert: Option<String>,
    }

    let temp_dir = tempfile::tempdir().unwrap();
    let db = setup_limbo_mvcc(&temp_dir, schema);
    let num_connections = per_conn_inserts.len();

    let mut connections: Vec<ConnState> = (0..num_connections)
        .map(|i| ConnState {
            conn: db.connect().unwrap(),
            inserts: per_conn_inserts[i].clone(),
            current_statement: None,
            current_insert: None,
        })
        .collect();

    loop {
        let all_finished = connections
            .iter()
            .all(|c| c.inserts.is_empty() && c.current_statement.is_none());
        if all_finished {
            break;
        }

        for conn in connections.iter_mut() {
            if conn.current_statement.is_none() && !conn.inserts.is_empty() {
                let sql = conn.inserts.remove(0);
                conn.conn.execute("BEGIN CONCURRENT").unwrap();
                conn.current_statement = Some(conn.conn.prepare(&sql).unwrap());
                conn.current_insert = Some(sql);
            }
            let Some(stmt) = conn.current_statement.as_mut() else {
                continue;
            };
            let is_commit = stmt.get_sql() == "COMMIT";
            match stmt.step() {
                Ok(StepResult::Done) => {
                    if is_commit {
                        conn.current_statement = None;
                        conn.current_insert = None;
                    } else {
                        conn.current_statement = Some(conn.conn.prepare("COMMIT").unwrap());
                    }
                }
                Ok(StepResult::IO) => {}
                Ok(StepResult::Busy) => {
                    stmt.reset();
                }
                Err(LimboError::SchemaUpdated) => {
                    conn.current_statement = Some(
                        conn.conn
                            .prepare(conn.current_insert.as_ref().unwrap())
                            .unwrap(),
                    );
                }
                Err(LimboError::WriteWriteConflict) => {
                    // Transaction may already be auto-rolled back; ignore ROLLBACK errors
                    let _ = conn.conn.execute("ROLLBACK");
                    let sql = conn.current_insert.take().unwrap();
                    conn.current_statement = None;
                    conn.inserts.insert(0, sql);
                }
                Err(e) => panic!("unexpected error: {e:?}"),
                _ => unreachable!(),
            }
        }
        db.io.step().unwrap();
    }
}

/// Run threaded MVCC writes with custom per-connection inserts.
fn run_threaded_mvcc_custom(schema: &str, per_conn_inserts: Vec<Vec<String>>) {
    let temp_dir = tempfile::tempdir().unwrap();
    let db = setup_limbo_mvcc(&temp_dir, schema);
    let num_connections = per_conn_inserts.len();

    let barrier = Arc::new(Barrier::new(num_connections));
    let mut handles = Vec::with_capacity(num_connections);

    for inserts in per_conn_inserts {
        let db = db.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            let conn = db.connect().unwrap();
            barrier.wait();
            for sql in &inserts {
                execute_mvcc_with_retry(&conn, sql);
            }
        }));
    }

    for h in handles {
        h.join().unwrap();
    }
}

fn bench_contention_patterns(criterion: &mut Criterion) {
    let num_writers = 4;
    let num_batches = 5;
    let rows_per_batch = 10;
    let total_rows = (num_writers * num_batches * rows_per_batch) as u64;

    let mut group = criterion.benchmark_group("Write Contention");
    group.throughput(Throughput::Elements(total_rows));

    // --- Pattern 1: separate_tables (each writer inserts into its own table) ---
    {
        let mut schema_parts = Vec::new();
        let mut per_conn: Vec<Vec<String>> = Vec::new();
        for w in 0..num_writers {
            let tbl = format!("test_{w}");
            schema_parts.push(format!(
                "CREATE TABLE {tbl} (id INTEGER PRIMARY KEY, data TEXT)"
            ));
            let mut batches = Vec::new();
            for b in 0..num_batches {
                let start = (b * rows_per_batch) as i64;
                batches.push(generate_batch_insert(&tbl, start, rows_per_batch));
            }
            per_conn.push(batches);
        }
        let schema = schema_parts.join("; ");

        group.bench_function(
            BenchmarkId::new("mvcc_cooperative", "separate_tables"),
            |b| {
                let schema = schema.clone();
                let per_conn = per_conn.clone();
                b.iter(|| run_cooperative_mvcc_custom(&schema, per_conn.clone()));
            },
        );

        group.bench_function(BenchmarkId::new("mvcc_threaded", "separate_tables"), |b| {
            let schema = schema.clone();
            let per_conn = per_conn.clone();
            b.iter(|| run_threaded_mvcc_custom(&schema, per_conn.clone()));
        });
    }

    // --- Pattern 2: disjoint_keys (same table, non-overlapping key ranges) ---
    {
        let schema = "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)";
        let per_conn =
            generate_inserts_per_connection("test", num_writers, num_batches, rows_per_batch);

        group.bench_function(BenchmarkId::new("mvcc_cooperative", "disjoint_keys"), |b| {
            let per_conn = per_conn.clone();
            b.iter(|| run_cooperative_mvcc_custom(schema, per_conn.clone()));
        });

        group.bench_function(BenchmarkId::new("mvcc_threaded", "disjoint_keys"), |b| {
            let per_conn = per_conn.clone();
            b.iter(|| run_threaded_mvcc_custom(schema, per_conn.clone()));
        });
    }

    // --- Pattern 3: overlapping_keys (same table, 50% key overlap, INSERT OR REPLACE) ---
    {
        let schema = "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)";
        let range_per_writer = num_batches * rows_per_batch;
        let overlap = range_per_writer / 2;
        let mut per_conn: Vec<Vec<String>> = Vec::new();
        for w in 0..num_writers {
            let base = w as i64 * overlap as i64;
            let mut batches = Vec::new();
            for b in 0..num_batches {
                let start = base + (b * rows_per_batch) as i64;
                let mut sql = String::from("INSERT OR REPLACE INTO test (id, data) VALUES ");
                for i in 0..rows_per_batch {
                    if i > 0 {
                        sql.push(',');
                    }
                    let id = start + i as i64;
                    sql.push_str(&format!("({id}, 'v{id}')"));
                }
                batches.push(sql);
            }
            per_conn.push(batches);
        }

        group.bench_function(
            BenchmarkId::new("mvcc_cooperative", "overlapping_keys"),
            |b| {
                let per_conn = per_conn.clone();
                b.iter(|| run_cooperative_mvcc_custom(schema, per_conn.clone()));
            },
        );

        group.bench_function(BenchmarkId::new("mvcc_threaded", "overlapping_keys"), |b| {
            let per_conn = per_conn.clone();
            b.iter(|| run_threaded_mvcc_custom(schema, per_conn.clone()));
        });
    }

    // --- Pattern 4: hot_rows_update (all writers update same 50 pre-populated rows) ---
    {
        let hot_rows = 50;
        // Schema includes pre-population of hot rows
        let mut schema = String::from(
            "CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT); INSERT INTO test (id, data) VALUES ",
        );
        for i in 0..hot_rows {
            if i > 0 {
                schema.push(',');
            }
            schema.push_str(&format!("({i}, 'init_{i}')"));
        }

        let mut per_conn: Vec<Vec<String>> = Vec::new();
        for w in 0..num_writers {
            let mut batches = Vec::new();
            for b in 0..num_batches {
                // Each batch updates a subset of the hot rows
                let mut sql = String::new();
                for i in 0..rows_per_batch {
                    if i > 0 {
                        sql.push_str("; ");
                    }
                    let row_id = (i + b * rows_per_batch) % hot_rows;
                    sql.push_str(&format!(
                        "UPDATE test SET data = 'w{w}_b{b}_i{i}' WHERE id = {row_id}"
                    ));
                }
                batches.push(sql);
            }
            per_conn.push(batches);
        }

        group.bench_function(
            BenchmarkId::new("mvcc_cooperative", "hot_rows_update"),
            |b| {
                let schema = schema.clone();
                let per_conn = per_conn.clone();
                b.iter(|| run_cooperative_mvcc_custom(&schema, per_conn.clone()));
            },
        );

        group.bench_function(BenchmarkId::new("mvcc_threaded", "hot_rows_update"), |b| {
            let schema = schema.clone();
            let per_conn = per_conn.clone();
            b.iter(|| run_threaded_mvcc_custom(&schema, per_conn.clone()));
        });
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 3: Mixed Read-Write Workload (YCSB-B style)
// ---------------------------------------------------------------------------

fn bench_mixed_workload(criterion: &mut Criterion) {
    let enable_rusqlite =
        std::env::var("DISABLE_RUSQLITE_BENCHMARK").is_err() && !cfg!(feature = "codspeed");

    let num_rows = 1_000;
    let num_readers = 4;
    let num_writers = 2;
    let ops_per_conn = 50;
    let total_ops = ((num_readers + num_writers) * ops_per_conn) as u64;

    let mut group = criterion.benchmark_group("Mixed Workload");
    group.throughput(Throughput::Elements(total_ops));

    // Pre-generate the initial data INSERT
    let mut initial_insert =
        String::from("INSERT INTO usertable (ycsb_key, field0, field1, field2) VALUES ");
    for i in 0..num_rows {
        if i > 0 {
            initial_insert.push(',');
        }
        initial_insert.push_str(&format!("({i}, 'f0_{i}', 'f1_{i}', 'f2_{i}')"));
    }

    let schema = "CREATE TABLE usertable (ycsb_key INTEGER PRIMARY KEY, field0 TEXT, field1 TEXT, field2 TEXT)";

    // Simple pseudo-Zipfian: 80% of accesses hit 20% of keys
    let hot_keys: Vec<i64> = (0..(num_rows / 5)).map(|i| i as i64).collect();
    let cold_keys: Vec<i64> = ((num_rows / 5)..num_rows).map(|i| i as i64).collect();

    // Generate read operations (point lookups)
    let gen_reads = |count: usize, seed: usize| -> Vec<String> {
        (0..count)
            .map(|i| {
                // 80% hot, 20% cold via simple deterministic selection
                let key = if (i + seed) % 5 < 4 {
                    hot_keys[(i + seed) % hot_keys.len()]
                } else {
                    cold_keys[(i + seed) % cold_keys.len()]
                };
                format!("SELECT * FROM usertable WHERE ycsb_key = {key}")
            })
            .collect()
    };

    // Generate write operations (updates)
    let gen_writes = |count: usize, seed: usize| -> Vec<String> {
        (0..count)
            .map(|i| {
                let key = if (i + seed) % 5 < 4 {
                    hot_keys[(i + seed) % hot_keys.len()]
                } else {
                    cold_keys[(i + seed) % cold_keys.len()]
                };
                format!("UPDATE usertable SET field0 = 'upd_{seed}_{i}' WHERE ycsb_key = {key}")
            })
            .collect()
    };

    // --- MVCC threaded ---
    group.bench_function("limbo_mvcc_threaded", |b| {
        b.iter_custom(|iters| {
            let mut total = Duration::ZERO;
            for _ in 0..iters {
                let temp_dir = tempfile::tempdir().unwrap();
                let db = setup_limbo_mvcc(&temp_dir, schema);
                // Pre-populate
                let conn = db.connect().unwrap();
                conn.execute(&initial_insert).unwrap();

                let barrier = Arc::new(Barrier::new(num_readers + num_writers));
                let mut handles = Vec::new();

                // Spawn readers
                for r in 0..num_readers {
                    let db = db.clone();
                    let barrier = barrier.clone();
                    let reads = gen_reads(ops_per_conn, r);
                    handles.push(std::thread::spawn(move || {
                        let conn = db.connect().unwrap();
                        barrier.wait();
                        for sql in &reads {
                            execute_with_retry(&conn, sql);
                        }
                    }));
                }

                // Spawn writers
                for w in 0..num_writers {
                    let db = db.clone();
                    let barrier = barrier.clone();
                    let writes = gen_writes(ops_per_conn, w + num_readers);
                    handles.push(std::thread::spawn(move || {
                        let conn = db.connect().unwrap();
                        barrier.wait();
                        for sql in &writes {
                            execute_mvcc_with_retry(&conn, sql);
                        }
                    }));
                }

                let start = Instant::now();
                // Threads are already spawned and waiting at barrier; the last thread
                // to reach barrier unblocks all of them. Timing starts before join.
                // Actually all threads have already started - we just join them.
                // Re-structure: we need barrier.wait() from main too or not.
                // Simpler: time from spawn (they wait at barrier) to join.
                for h in handles {
                    h.join().unwrap();
                }
                total += start.elapsed();
            }
            total
        });
    });

    // --- SQLite WAL baseline ---
    if enable_rusqlite {
        group.bench_function("sqlite_wal_threaded", |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let conn = setup_rusqlite(&temp_dir, schema);
                    conn.execute_batch(&initial_insert).unwrap();
                    drop(conn);

                    let db_path = temp_dir.path().join("bench.db");
                    let barrier = Arc::new(Barrier::new(num_readers + num_writers));
                    let mut handles = Vec::new();

                    for r in 0..num_readers {
                        let db_path = db_path.clone();
                        let barrier = barrier.clone();
                        let reads = gen_reads(ops_per_conn, r);
                        handles.push(std::thread::spawn(move || {
                            let conn = rusqlite::Connection::open(&db_path).unwrap();
                            conn.busy_timeout(std::time::Duration::from_secs(5))
                                .unwrap();
                            barrier.wait();
                            for sql in &reads {
                                let mut stmt = conn.prepare(sql).unwrap();
                                let mut rows = stmt.raw_query();
                                while let Some(_row) = rows.next().unwrap() {}
                            }
                        }));
                    }

                    for w in 0..num_writers {
                        let db_path = db_path.clone();
                        let barrier = barrier.clone();
                        let writes = gen_writes(ops_per_conn, w + num_readers);
                        handles.push(std::thread::spawn(move || {
                            let conn = rusqlite::Connection::open(&db_path).unwrap();
                            conn.busy_timeout(std::time::Duration::from_secs(5))
                                .unwrap();
                            barrier.wait();
                            for sql in &writes {
                                conn.execute(sql, []).unwrap();
                            }
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();
                }
                total
            });
        });
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Benchmark 4: Write Burst (Thundering Herd)
// ---------------------------------------------------------------------------

fn bench_write_burst(criterion: &mut Criterion) {
    let enable_rusqlite =
        std::env::var("DISABLE_RUSQLITE_BENCHMARK").is_err() && !cfg!(feature = "codspeed");

    let writer_counts: &[usize] = &[4, 8, 16];
    let rows_per_writer = 50;

    let mut group = criterion.benchmark_group("Write Burst");

    for &writers in writer_counts {
        let total_rows = (writers * rows_per_writer) as u64;
        group.throughput(Throughput::Elements(total_rows));

        let schema = "CREATE TABLE events (id INTEGER PRIMARY KEY, writer_id INTEGER, seq INTEGER, payload TEXT)";
        // WAL threaded uses a table without PK to avoid UNIQUE constraint failures on BUSY retries
        let wal_schema =
            "CREATE TABLE events (id INTEGER, writer_id INTEGER, seq INTEGER, payload TEXT)";

        // Generate per-writer inserts (one big batch per writer, disjoint key ranges)
        let gen_burst_inserts = |num_writers: usize| -> Vec<String> {
            (0..num_writers)
                .map(|w| {
                    let mut sql =
                        String::from("INSERT INTO events (id, writer_id, seq, payload) VALUES ");
                    for i in 0..rows_per_writer {
                        if i > 0 {
                            sql.push(',');
                        }
                        let id = w * rows_per_writer + i;
                        sql.push_str(&format!("({id}, {w}, {i}, 'payload_{w}_{i}')"));
                    }
                    sql
                })
                .collect()
        };

        // MVCC threaded
        group.bench_function(BenchmarkId::new("limbo_mvcc_threaded", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_limbo_mvcc(&temp_dir, schema);
                    let inserts = gen_burst_inserts(writers);

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for sql in inserts {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            execute_mvcc_with_retry(&conn, &sql);
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();
                }
                total
            });
        });

        // WAL threaded
        group.bench_function(BenchmarkId::new("limbo_wal_threaded", writers), |b| {
            b.iter_custom(|iters| {
                let mut total = Duration::ZERO;
                for _ in 0..iters {
                    let temp_dir = tempfile::tempdir().unwrap();
                    let db = setup_limbo(&temp_dir, wal_schema);
                    let inserts = gen_burst_inserts(writers);

                    let barrier = Arc::new(Barrier::new(writers));
                    let mut handles = Vec::with_capacity(writers);

                    for sql in inserts {
                        let db = db.clone();
                        let barrier = barrier.clone();
                        handles.push(std::thread::spawn(move || {
                            let conn = db.connect().unwrap();
                            barrier.wait();
                            execute_with_retry(&conn, &sql);
                        }));
                    }

                    let start = Instant::now();
                    for h in handles {
                        h.join().unwrap();
                    }
                    total += start.elapsed();
                }
                total
            });
        });

        // SQLite WAL threaded
        if enable_rusqlite {
            group.bench_function(BenchmarkId::new("sqlite_wal_threaded", writers), |b| {
                b.iter_custom(|iters| {
                    let mut total = Duration::ZERO;
                    for _ in 0..iters {
                        let temp_dir = tempfile::tempdir().unwrap();
                        let conn = setup_rusqlite(&temp_dir, schema);
                        drop(conn);

                        let db_path = temp_dir.path().join("bench.db");
                        let inserts = gen_burst_inserts(writers);
                        let barrier = Arc::new(Barrier::new(writers));
                        let mut handles = Vec::with_capacity(writers);

                        for sql in inserts {
                            let db_path = db_path.clone();
                            let barrier = barrier.clone();
                            handles.push(std::thread::spawn(move || {
                                let conn = rusqlite::Connection::open(&db_path).unwrap();
                                conn.busy_timeout(std::time::Duration::from_secs(10))
                                    .unwrap();
                                barrier.wait();
                                conn.execute(&sql, []).unwrap();
                            }));
                        }

                        let start = Instant::now();
                        for h in handles {
                            h.join().unwrap();
                        }
                        total += start.elapsed();
                    }
                    total
                });
            });
        }
    }

    group.finish();
}

// ---------------------------------------------------------------------------
// Criterion harness
// ---------------------------------------------------------------------------

#[cfg(not(feature = "codspeed"))]
criterion_group! {
    name = parallel_write_benches;
    config = Criterion::default()
        .with_profiler(PProfProfiler::new(100, Output::Flamegraph(None)))
        .sample_size(20);
    targets = bench_writer_scalability, bench_contention_patterns, bench_mixed_workload, bench_write_burst
}

#[cfg(feature = "codspeed")]
criterion_group! {
    name = parallel_write_benches;
    config = Criterion::default().sample_size(20);
    targets = bench_writer_scalability, bench_contention_patterns, bench_mixed_workload, bench_write_burst
}

criterion_main!(parallel_write_benches);
