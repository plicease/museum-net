//! The durable configuration store and change log.
//!
//! Each revision is a complete snapshot in its own numbered file, and the
//! highest number is current. A file only appears under its final name once
//! its contents are on disk, so a crash leaves the previous revision current.
//! Nothing is rewritten: a rollback is a new revision.

use std::fs::{self, File, TryLockError};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use museum_core::config::{Config, SCHEMA_VERSION};
use museum_core::protocol::Change;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Revision {
    pub schema: u32,
    #[serde(flatten)]
    pub change: Change,
    pub config: Config,
}

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("{path}: {source}")]
    Io { path: PathBuf, source: io::Error },
    #[error("{0} is in use by another museumd")]
    Locked(PathBuf),
    #[error("{path} has schema {schema}, but this museumd supports up to {SCHEMA_VERSION}")]
    TooNew { path: PathBuf, schema: u64 },
    #[error("{path} is damaged: {reason}")]
    Damaged { path: PathBuf, reason: String },
}

pub struct Store {
    dir: PathBuf,
    latest: u64,
    /// Held for the life of the process; the OS releases it on exit.
    _lock: File,
}

fn io_error(path: &Path) -> impl FnOnce(io::Error) -> StoreError {
    let path = path.to_owned();
    move |source| StoreError::Io { path, source }
}

impl Store {
    pub fn open(dir: &Path) -> Result<Self, StoreError> {
        let lock_path = dir.join("lock");
        let lock = File::create(&lock_path).map_err(io_error(&lock_path))?;
        lock.try_lock().map_err(|error| match error {
            TryLockError::WouldBlock => StoreError::Locked(dir.to_owned()),
            TryLockError::Error(source) => io_error(&lock_path)(source),
        })?;

        let mut latest = 0;
        for entry in fs::read_dir(dir).map_err(io_error(dir))? {
            let path = entry.map_err(io_error(dir))?.path();
            match path.extension().and_then(|extension| extension.to_str()) {
                // Left by a crash during `append`, which never reported success.
                Some("tmp") => fs::remove_file(&path).map_err(io_error(&path))?,
                Some("json") => {
                    let number = path.file_stem().and_then(|stem| stem.to_str());
                    match number.and_then(|number| number.parse().ok()) {
                        Some(revision) => latest = latest.max(revision),
                        None => {
                            return Err(StoreError::Damaged {
                                reason: "not named after a revision".to_owned(),
                                path,
                            });
                        }
                    }
                }
                _ => {}
            }
        }
        Ok(Self {
            dir: dir.to_owned(),
            latest,
            _lock: lock,
        })
    }

    /// The current revision number, or 0 for an empty store.
    pub fn latest(&self) -> u64 {
        self.latest
    }

    fn path(&self, revision: u64) -> PathBuf {
        self.dir.join(format!("{revision:08}.json"))
    }

    pub fn contains(&self, revision: u64) -> bool {
        self.path(revision).exists()
    }

    pub fn read(&self, revision: u64) -> Result<Revision, StoreError> {
        let path = self.path(revision);
        let text = fs::read_to_string(&path).map_err(io_error(&path))?;
        let record = decode(&path, &text)?;
        if record.change.revision != revision {
            return Err(StoreError::Damaged {
                reason: format!("it claims to be revision {}", record.change.revision),
                path,
            });
        }
        Ok(record)
    }

    /// Commit `config` as the next revision. On return it is on disk.
    pub fn append(
        &mut self,
        time: String,
        actor: String,
        action: String,
        config: Config,
    ) -> Result<Revision, StoreError> {
        let revision = self.latest + 1;
        let record = Revision {
            schema: SCHEMA_VERSION,
            change: Change {
                revision,
                time,
                actor,
                action,
            },
            config,
        };
        let path = self.path(revision);
        let temporary = path.with_extension("tmp");
        let text = serde_json::to_vec_pretty(&record).expect("a revision always serializes");

        let mut file = File::create(&temporary).map_err(io_error(&temporary))?;
        file.write_all(&text).map_err(io_error(&temporary))?;
        file.sync_all().map_err(io_error(&temporary))?;
        fs::rename(&temporary, &path).map_err(io_error(&path))?;
        // The rename isn't durable until the directory is.
        File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(io_error(&self.dir))?;

        self.latest = revision;
        Ok(record)
    }

    /// Change log entries, newest first.
    pub fn history(&self, limit: Option<usize>) -> Result<Vec<Change>, StoreError> {
        (1..=self.latest)
            .rev()
            .filter(|revision| self.contains(*revision))
            .take(limit.unwrap_or(usize::MAX))
            .map(|revision| Ok(self.read(revision)?.change))
            .collect()
    }
}

/// Read a stored revision of any supported schema as the current one. When
/// `SCHEMA_VERSION` changes, convert the older `Value` here.
fn decode(path: &Path, text: &str) -> Result<Revision, StoreError> {
    let damaged = |reason: String| StoreError::Damaged {
        path: path.to_owned(),
        reason,
    };
    let value: Value = serde_json::from_str(text).map_err(|error| damaged(error.to_string()))?;
    let schema = value
        .get("schema")
        .and_then(Value::as_u64)
        .ok_or_else(|| damaged("no schema version".to_owned()))?;
    if schema > u64::from(SCHEMA_VERSION) {
        return Err(StoreError::TooNew {
            path: path.to_owned(),
            schema,
        });
    }
    serde_json::from_value(value).map_err(|error| damaged(error.to_string()))
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    pub fn scratch() -> PathBuf {
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let dir = std::env::temp_dir().join(format!(
            "museumd-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn append(store: &mut Store, action: &str) -> Revision {
        store
            .append(
                "now".into(),
                "tester".into(),
                action.into(),
                Config::default(),
            )
            .unwrap()
    }

    #[test]
    fn revisions_count_up_and_persist() {
        let dir = scratch();
        let first = {
            let mut store = Store::open(&dir).unwrap();
            assert_eq!(store.latest(), 0);
            let first = append(&mut store, "first");
            append(&mut store, "second");
            first
        };
        let store = Store::open(&dir).unwrap();
        assert_eq!(store.latest(), 2);
        assert_eq!(store.read(1).unwrap(), first);
    }

    #[test]
    fn history_is_newest_first_and_limited() {
        let dir = scratch();
        let mut store = Store::open(&dir).unwrap();
        for action in ["a", "b", "c"] {
            append(&mut store, action);
        }
        let actions: Vec<_> = store
            .history(Some(2))
            .unwrap()
            .into_iter()
            .map(|change| change.action)
            .collect();
        assert_eq!(actions, ["c", "b"]);
    }

    #[test]
    fn a_crash_before_the_rename_leaves_the_previous_revision_current() {
        let dir = scratch();
        append(&mut Store::open(&dir).unwrap(), "committed");
        fs::write(dir.join("00000002.tmp"), "{ half a rev").unwrap();

        let store = Store::open(&dir).unwrap();
        assert_eq!(store.latest(), 1);
        assert!(!dir.join("00000002.tmp").exists());
    }

    #[test]
    fn a_second_daemon_cannot_share_the_store() {
        let dir = scratch();
        let _first = Store::open(&dir).unwrap();
        assert!(matches!(Store::open(&dir), Err(StoreError::Locked(_))));
    }

    #[test]
    fn a_damaged_revision_is_reported_not_guessed_at() {
        let dir = scratch();
        let mut store = Store::open(&dir).unwrap();
        append(&mut store, "committed");
        fs::write(dir.join("00000001.json"), "not json").unwrap();
        assert!(matches!(store.read(1), Err(StoreError::Damaged { .. })));

        fs::write(dir.join("junk.json"), "{}").unwrap();
        drop(store);
        assert!(matches!(Store::open(&dir), Err(StoreError::Damaged { .. })));
    }

    #[test]
    fn a_renamed_revision_is_detected() {
        let dir = scratch();
        let mut store = Store::open(&dir).unwrap();
        append(&mut store, "first");
        fs::copy(dir.join("00000001.json"), dir.join("00000002.json")).unwrap();
        assert!(matches!(store.read(2), Err(StoreError::Damaged { .. })));
    }

    #[test]
    fn a_revision_from_a_newer_schema_is_refused() {
        let dir = scratch();
        let mut store = Store::open(&dir).unwrap();
        append(&mut store, "first");
        let path = dir.join("00000001.json");
        let text = fs::read_to_string(&path).unwrap();
        let future = text.replace(
            &format!("\"schema\": {SCHEMA_VERSION}"),
            &format!("\"schema\": {}", SCHEMA_VERSION + 1),
        );
        assert_ne!(future, text);
        fs::write(&path, future).unwrap();
        assert!(matches!(store.read(1), Err(StoreError::TooNew { .. })));
    }
}
