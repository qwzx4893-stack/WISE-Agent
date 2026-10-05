PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('persistent', 'daily', 'discussion')),
  kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'event', 'note', 'summary', 'knowledge', 'task')),
  title TEXT,
  content_md TEXT NOT NULL,
  content_text TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('explicit_user', 'inferred', 'tool_output', 'conversation', 'system')),
  source_ref TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.7,
  day_key TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_accessed_at INTEGER,
  expires_at INTEGER,
  is_pinned INTEGER NOT NULL DEFAULT 0,
  is_deleted INTEGER NOT NULL DEFAULT 0,
  supersedes_item_id TEXT,
  dedupe_hash TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_items_scope_created
  ON memory_items(scope, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_items_day_key
  ON memory_items(day_key);
CREATE INDEX IF NOT EXISTS idx_memory_items_expires_at
  ON memory_items(expires_at);
CREATE INDEX IF NOT EXISTS idx_memory_items_deleted
  ON memory_items(is_deleted);

CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_items_scope_dedupe
  ON memory_items(scope, dedupe_hash)
  WHERE dedupe_hash IS NOT NULL AND is_deleted = 0;

CREATE TABLE IF NOT EXISTS memory_chunks (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  namespace TEXT NOT NULL CHECK (namespace IN ('memory_persistent', 'memory_daily', 'memory_discussion', 'conversation_daily', 'context')),
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  token_estimate INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  embedding_model TEXT,
  embedding_json TEXT,
  FOREIGN KEY (item_id) REFERENCES memory_items(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_chunks_item_index
  ON memory_chunks(item_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_namespace
  ON memory_chunks(namespace);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
  content,
  chunk_id UNINDEXED,
  item_id UNINDEXED,
  namespace UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS trg_memory_chunks_ai
AFTER INSERT ON memory_chunks BEGIN
  INSERT INTO memory_chunks_fts(content, chunk_id, item_id, namespace)
  VALUES (new.content, new.id, new.item_id, new.namespace);
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_chunks_au
AFTER UPDATE ON memory_chunks BEGIN
  DELETE FROM memory_chunks_fts WHERE chunk_id = old.id;
  INSERT INTO memory_chunks_fts(content, chunk_id, item_id, namespace)
  VALUES (new.content, new.id, new.item_id, new.namespace);
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_chunks_ad
AFTER DELETE ON memory_chunks BEGIN
  DELETE FROM memory_chunks_fts WHERE chunk_id = old.id;
END;

CREATE TABLE IF NOT EXISTS memory_facts (
  id TEXT PRIMARY KEY,
  fact_key TEXT NOT NULL,
  fact_value_json TEXT NOT NULL,
  canonical_text TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 50,
  source_item_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  is_pinned INTEGER NOT NULL DEFAULT 0,
  is_deleted INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (source_item_id) REFERENCES memory_items(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_facts_key_active
  ON memory_facts(fact_key)
  WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS context_documents (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  file_path TEXT NOT NULL UNIQUE,
  checksum TEXT NOT NULL,
  title TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_indexed_at INTEGER NOT NULL,
  is_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS context_chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  token_estimate INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  embedding_model TEXT,
  embedding_json TEXT,
  FOREIGN KEY (document_id) REFERENCES context_documents(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_context_chunks_doc_index
  ON context_chunks(document_id, chunk_index);

CREATE VIRTUAL TABLE IF NOT EXISTS context_chunks_fts USING fts5(
  content,
  chunk_id UNINDEXED,
  document_id UNINDEXED,
  filename UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS trg_context_chunks_ai
AFTER INSERT ON context_chunks BEGIN
  INSERT INTO context_chunks_fts(content, chunk_id, document_id, filename)
  VALUES (
    new.content,
    new.id,
    new.document_id,
    (SELECT filename FROM context_documents WHERE id = new.document_id)
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_context_chunks_au
AFTER UPDATE ON context_chunks BEGIN
  DELETE FROM context_chunks_fts WHERE chunk_id = old.id;
  INSERT INTO context_chunks_fts(content, chunk_id, document_id, filename)
  VALUES (
    new.content,
    new.id,
    new.document_id,
    (SELECT filename FROM context_documents WHERE id = new.document_id)
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_context_chunks_ad
AFTER DELETE ON context_chunks BEGIN
  DELETE FROM context_chunks_fts WHERE chunk_id = old.id;
END;
