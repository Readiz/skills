import { createHash, randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import {
  closeSync,
  chmodSync as chmodSyncLocal,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  writeFileSync,
  unlinkSync as unlinkSyncLocal,
  fsyncSync,
} from "node:fs";
import path from "node:path";
import os from "node:os";

const MAX_BODY = 100_000;
const MAX_FILE = 25 * 1024 * 1024;
const DEFAULT_READ = 32 * 1024;
const MAX_READ = 64 * 1024;
const now = () => new Date().toISOString();
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const json = (value) => JSON.stringify(value);
const fail = (code, message = code) =>
  Object.assign(new Error(message), { code });

function ensurePrivateDir(dir) {
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const info = lstatSync(dir);
  if (info.isSymbolicLink() || !info.isDirectory()) throw fail("INVALID_PATH");
  chmodPrivate(dir, 0o700);
  return realpathSync(dir);
}

function chmodPrivate(file, mode) {
  chmodSyncLocal(file, mode);
}

function assertOutsideGit(dataDir) {
  let current = dataDir;
  for (;;) {
    const marker = path.join(current, ".git");
    if (existsSync(marker)) throw fail("DATA_DIR_IN_GIT");
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
}
function futureRealPath(target) {
  let ancestor = path.resolve(target);
  const suffix = [];
  while (!existsSync(ancestor)) {
    const parent = path.dirname(ancestor);
    if (parent === ancestor) throw fail("INVALID_PATH");
    suffix.unshift(path.basename(ancestor));
    ancestor = parent;
  }
  const info = lstatSync(ancestor);
  if (info.isSymbolicLink() || !info.isDirectory()) throw fail("INVALID_PATH");
  return path.resolve(realpathSync(ancestor), ...suffix);
}

export function normalizeFolder(value = "") {
  if (typeof value !== "string") throw fail("INVALID_FOLDER");
  if (value === "") return "";
  if (value.includes("\\") || value.startsWith("/") || value.endsWith("/"))
    throw fail("INVALID_FOLDER");
  const parts = value.split("/");
  if (
    parts.some(
      (part) =>
        !part ||
        part === "." ||
        part === ".." ||
        /[\u0000-\u001f\u007f]/u.test(part),
    )
  )
    throw fail("INVALID_FOLDER");
  if (value.length > 240) throw fail("INVALID_FOLDER");
  return parts.join("/");
}

function assertString(value, name, max, { min = 0, trim = false } = {}) {
  if (typeof value !== "string")
    throw fail("INVALID_INPUT", `${name} must be a string`);
  const result = trim ? value.trim() : value;
  if (result.length < min || result.length > max)
    throw fail("INVALID_INPUT", `${name} length is invalid`);
  return result;
}

function validObservedAt(value) {
  if (value == null) return now();
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d\d-\d\dT/.test(value) ||
    !Number.isFinite(Date.parse(value))
  )
    throw fail("INVALID_INPUT", "observed_at must be an ISO 8601 timestamp");
  return new Date(value).toISOString();
}

function normalizeSources(sources = []) {
  if (!Array.isArray(sources) || sources.length > 20)
    throw fail("INVALID_INPUT", "sources must contain at most 20 entries");
  return sources.map((source) => {
    if (
      !source ||
      typeof source !== "object" ||
      Array.isArray(source) ||
      Object.keys(source).some((key) => !["title", "url"].includes(key))
    )
      throw fail("INVALID_INPUT", "invalid source");
    const title = assertString(source.title, "source title", 240, {
      min: 1,
      trim: true,
    });
    const url = assertString(source.url, "source url", 2048, {
      min: 1,
      trim: true,
    });
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      throw fail("INVALID_INPUT", "source url is invalid");
    }
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password
    )
      throw fail(
        "INVALID_INPUT",
        "source url must use HTTP(S) without credentials",
      );
    return { title, url: parsed.toString() };
  });
}

function payloadHash(value) {
  return sha256(Buffer.from(json(value)));
}

function escapeLike(value) {
  return value.replace(/[\\%_]/g, "\\$&");
}

function ensureDbFile(file) {
  if (existsSync(file)) {
    const info = lstatSync(file);
    if (info.isSymbolicLink() || !info.isFile()) throw fail("INVALID_PATH");
    chmodSyncLocal(file, 0o600);
  } else {
    const fd = openSync(file, "wx", 0o600);
    closeSync(fd);
  }
}

function ensureInputPath(importRoot, input) {
  if (typeof input !== "string" || !path.isAbsolute(input))
    throw fail("INVALID_FILE_PATH");
  if (input.split(path.sep).some((part) => part === ".."))
    throw fail("INVALID_FILE_PATH");
  const raw = path.resolve(input);
  const importRootReal = realpathSync(importRoot);
  let relative = path.relative(importRoot, raw);
  let walkRoot = importRoot;
  if (
    !relative ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    relative = path.relative(importRootReal, raw);
    walkRoot = importRootReal;
  }
  if (
    !relative ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  )
    throw fail("INVALID_FILE_PATH");
  let cursor = walkRoot;
  for (const part of relative.split(path.sep)) {
    if (!part || part === "." || part === "..") throw fail("INVALID_FILE_PATH");
    cursor = path.join(cursor, part);
    const info = lstatSync(cursor);
    if (info.isSymbolicLink())
      throw fail("INVALID_FILE_PATH", "symlink paths are not allowed");
  }
  const resolved = realpathSync(raw);
  const resolvedRelative = path.relative(importRootReal, resolved);
  if (
    !resolvedRelative ||
    resolvedRelative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(resolvedRelative)
  )
    throw fail("INVALID_FILE_PATH");
  const stat = statSync(resolved);
  if (!stat.isFile() || stat.size > MAX_FILE) throw fail("FILE_TOO_LARGE");
  return resolved;
}

function mimeFor(name) {
  const ext = path.extname(name).toLowerCase();
  return (
    {
      ".txt": "text/plain",
      ".md": "text/markdown",
      ".csv": "text/csv",
      ".json": "application/json",
      ".html": "text/html",
      ".xml": "application/xml",
      ".log": "text/plain",
    }[ext] ?? "application/octet-stream"
  );
}

function summary(body) {
  return body.replace(/\s+/gu, " ").trim().slice(0, 280);
}

export function createStore({ dataDir, importDir } = {}) {
  const requested =
    dataDir ??
    process.env.PERSONAL_INBOX_DIR ??
    path.join(os.homedir(), ".local", "share", "personal-inbox");
  if (!path.isAbsolute(requested)) throw fail("DATA_DIR_MUST_BE_ABSOLUTE");
  const requestedRoot = path.resolve(requested);
  assertOutsideGit(futureRealPath(requestedRoot));
  const explicitImportDir = importDir ?? process.env.PERSONAL_INBOX_IMPORT_DIR;
  if (explicitImportDir != null && !path.isAbsolute(explicitImportDir))
    throw fail("INVALID_IMPORT_DIR");
  const importRequested = path.resolve(
    explicitImportDir ?? path.join(requestedRoot, "imports"),
  );
  // Validate all caller-supplied paths before creating or chmodding the store.
  if (explicitImportDir != null) {
    if (!existsSync(importRequested)) throw fail("INVALID_IMPORT_DIR");
    const importInfo = lstatSync(importRequested);
    if (importInfo.isSymbolicLink() || !importInfo.isDirectory())
      throw fail("INVALID_IMPORT_DIR");
  }
  const root = ensurePrivateDir(requestedRoot);
  assertOutsideGit(root);
  if (explicitImportDir == null) ensurePrivateDir(importRequested);
  const importInfo = lstatSync(importRequested);
  if (importInfo.isSymbolicLink() || !importInfo.isDirectory())
    throw fail("INVALID_IMPORT_DIR");
  const imports = importRequested;
  const filesDir = ensurePrivateDir(path.join(root, "files"));
  const dbPath = path.join(root, "inbox.sqlite");
  ensureDbFile(dbPath);
  const db = new DatabaseSync(dbPath);
  db.exec(
    "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;",
  );
  db.exec(`
    CREATE TABLE IF NOT EXISTS notes (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      folder TEXT NOT NULL,
      task TEXT NOT NULL,
      run_key TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      sources_json TEXT NOT NULL,
      revision INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS note_versions (
      note_id TEXT NOT NULL REFERENCES notes(id),
      revision INTEGER NOT NULL,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      folder TEXT NOT NULL,
      task TEXT NOT NULL,
      run_key TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      sources_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY(note_id, revision)
    );
    CREATE TABLE IF NOT EXISTS save_receipts (
      task TEXT NOT NULL,
      run_key TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      note_id TEXT NOT NULL REFERENCES notes(id),
      revision INTEGER NOT NULL,
      saved_at TEXT NOT NULL,
      PRIMARY KEY(task, run_key)
    );
    CREATE TABLE IF NOT EXISTS attachments (
      id TEXT PRIMARY KEY,
      note_id TEXT NOT NULL REFERENCES notes(id),
      attachment_key TEXT NOT NULL,
      sha256 TEXT NOT NULL,
      bytes INTEGER NOT NULL,
      filename TEXT NOT NULL,
      mime_type TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(note_id, attachment_key)
    );
    CREATE TABLE IF NOT EXISTS version_attachments (
      note_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      attachment_id TEXT NOT NULL REFERENCES attachments(id),
      PRIMARY KEY(note_id, revision, attachment_id),
      FOREIGN KEY(note_id, revision) REFERENCES note_versions(note_id, revision)
    );
    CREATE TABLE IF NOT EXISTS attachment_receipts (
      note_id TEXT NOT NULL REFERENCES notes(id),
      attachment_key TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      revision INTEGER NOT NULL,
      attachment_id TEXT NOT NULL REFERENCES attachments(id),
      saved_at TEXT NOT NULL,
      PRIMARY KEY(note_id, attachment_key)
    );
    CREATE INDEX IF NOT EXISTS notes_folder_idx ON notes(folder);
    CREATE INDEX IF NOT EXISTS notes_updated_idx ON notes(updated_at DESC);
    CREATE INDEX IF NOT EXISTS attachments_note_idx ON attachments(note_id);
  `);
  // Existing stores may contain files created by an earlier run; preserve them and tighten modes.
  chmodSyncLocal(dbPath, 0o600);

  const inTransaction = (fn) => {
    db.exec("BEGIN IMMEDIATE");
    try {
      const result = fn();
      db.exec("COMMIT");
      return result;
    } catch (error) {
      try {
        db.exec("ROLLBACK");
      } catch {}
      throw error;
    }
  };
  const getNote = db.prepare("SELECT * FROM notes WHERE id=?");
  const getVersion = db.prepare(
    "SELECT * FROM note_versions WHERE note_id=? AND revision=?",
  );
  const getVersionAttachments =
    db.prepare(`SELECT a.id,a.attachment_key,a.sha256,a.bytes,a.filename,a.mime_type
    FROM version_attachments va JOIN attachments a ON a.id=va.attachment_id
    WHERE va.note_id=? AND va.revision=? ORDER BY a.created_at,a.id`);
  const getReceipt = db.prepare(
    "SELECT * FROM save_receipts WHERE task=? AND run_key=?",
  );

  function versionAttachments(noteId, revision) {
    return getVersionAttachments.all(noteId, revision);
  }
  function attachSnapshot(noteId, fromRevision, toRevision, extraId = null) {
    const prior =
      fromRevision == null
        ? []
        : versionAttachments(noteId, fromRevision).map((row) => row.id);
    if (extraId) prior.push(extraId);
    const insert = db.prepare(
      "INSERT INTO version_attachments(note_id,revision,attachment_id) VALUES(?,?,?)",
    );
    for (const attachmentId of prior)
      insert.run(noteId, toRevision, attachmentId);
  }
  function receipt(row) {
    return {
      saved: true,
      id: row.note_id,
      revision: Number(row.revision),
      saved_at: row.saved_at,
    };
  }
  function conflict(message = "The record changed since it was read.") {
    throw fail("CONFLICT", message);
  }

  function saveNote(input) {
    const task = assertString(input.task, "task", 180, { min: 1, trim: true });
    const runKey = assertString(input.run_key, "run_key", 180, {
      min: 1,
      trim: true,
    });
    const payload = {
      title: assertString(input.title, "title", 240, { min: 1, trim: true }),
      body: assertString(input.body, "body", MAX_BODY, { min: 1 }),
      folder: normalizeFolder(input.folder ?? ""),
      task,
      run_key: runKey,
      sources: normalizeSources(input.sources ?? []),
      observed_at:
        input.observed_at == null ? null : validObservedAt(input.observed_at),
    };
    const hash = payloadHash(payload);
    const existing = getReceipt.get(task, runKey);
    if (existing) {
      if (existing.payload_hash !== hash)
        conflict("This task/run_key was already used with different content.");
      return receipt(existing);
    }
    const created = now();
    const observedAt = payload.observed_at ?? created;
    const id = randomUUID();
    return inTransaction(() => {
      const raced = getReceipt.get(task, runKey);
      if (raced) {
        if (raced.payload_hash !== hash)
          conflict(
            "This task/run_key was already used with different content.",
          );
        return receipt(raced);
      }
      db.prepare(
        `INSERT INTO notes(id,title,body,folder,task,run_key,observed_at,sources_json,revision,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,1,?,?)`,
      ).run(
        id,
        payload.title,
        payload.body,
        payload.folder,
        task,
        runKey,
        observedAt,
        json(payload.sources),
        created,
        created,
      );
      db.prepare(
        `INSERT INTO note_versions(note_id,revision,title,body,folder,task,run_key,observed_at,sources_json,created_at)
        VALUES(?,1,?,?,?,?,?,?,?,?)`,
      ).run(
        id,
        payload.title,
        payload.body,
        payload.folder,
        task,
        runKey,
        observedAt,
        json(payload.sources),
        created,
      );
      db.prepare(
        "INSERT INTO save_receipts(task,run_key,payload_hash,note_id,revision,saved_at) VALUES(?,?,?,?,1,?)",
      ).run(task, runKey, hash, id, created);
      return { saved: true, id, revision: 1, saved_at: created };
    });
  }

  function getSaveReceipt({ task, run_key }) {
    const row = getReceipt.get(
      assertString(task, "task", 180, { min: 1, trim: true }),
      assertString(run_key, "run_key", 180, { min: 1, trim: true }),
    );
    return row ? receipt(row) : { saved: false, task, run_key };
  }

  function searchUpdates({ query = "", folder, limit = 20, offset = 0 } = {}) {
    query = assertString(query, "query", 500, { trim: true });
    if (
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100 ||
      !Number.isInteger(offset) ||
      offset < 0 ||
      offset > 10_000_000
    )
      throw fail("INVALID_INPUT");
    const clauses = [];
    const params = [];
    if (folder !== undefined) {
      const normalized = normalizeFolder(folder);
      if (normalized) {
        clauses.push("(folder=? OR folder LIKE ? ESCAPE '\\')");
        params.push(normalized, `${escapeLike(normalized)}/%`);
      } else clauses.push("folder=''");
    }
    if (query) {
      const pattern = `%${escapeLike(query)}%`;
      clauses.push(
        "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR task LIKE ? ESCAPE '\\')",
      );
      params.push(pattern, pattern, pattern);
    }
    const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
    const count = Number(
      db.prepare(`SELECT COUNT(*) AS total FROM notes ${where}`).get(...params)
        .total,
    );
    const rows = db
      .prepare(
        `SELECT id,title,folder,task,run_key,observed_at,revision,updated_at,body
      FROM notes ${where} ORDER BY updated_at DESC,id LIMIT ? OFFSET ?`,
      )
      .all(...params, limit, offset);
    return {
      total: count,
      limit,
      offset,
      items: rows.map((row) => ({
        id: row.id,
        title: row.title,
        folder: row.folder,
        task: row.task,
        run_key: row.run_key,
        observed_at: row.observed_at,
        revision: Number(row.revision),
        updated_at: row.updated_at,
        summary: summary(row.body),
      })),
    };
  }

  function readUpdate({ id, revision }) {
    if (typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/iu.test(id))
      throw fail("NOT_FOUND");
    const note = getNote.get(id);
    if (!note) throw fail("NOT_FOUND");
    const selected = revision == null ? Number(note.revision) : revision;
    if (!Number.isInteger(selected) || selected < 1)
      throw fail("INVALID_INPUT");
    const row = getVersion.get(id, selected);
    if (!row) throw fail("NOT_FOUND");
    return {
      id: row.note_id,
      title: row.title,
      body: row.body,
      folder: row.folder,
      task: row.task,
      run_key: row.run_key,
      observed_at: row.observed_at,
      sources: JSON.parse(row.sources_json),
      revision: Number(row.revision),
      created_at: row.created_at,
      latest_revision: Number(note.revision),
      updated_at: note.updated_at,
      attachments: versionAttachments(id, selected),
    };
  }

  function updateNote(input) {
    const id = input.id;
    if (typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/iu.test(id))
      throw fail("NOT_FOUND");
    const baseRevision = input.revision;
    if (!Number.isInteger(baseRevision) || baseRevision < 1)
      throw fail("INVALID_INPUT");
    const base = getVersion.get(id, baseRevision);
    const current = getNote.get(id);
    if (!base || !current) throw fail("NOT_FOUND");
    const desired = {
      title:
        input.title === undefined
          ? base.title
          : assertString(input.title, "title", 240, { min: 1, trim: true }),
      body:
        input.body === undefined
          ? base.body
          : assertString(input.body, "body", MAX_BODY, { min: 1 }),
      folder:
        input.folder === undefined
          ? base.folder
          : normalizeFolder(input.folder),
    };
    const retry = getVersion.get(id, baseRevision + 1);
    if (Number(current.revision) !== baseRevision) {
      if (
        retry &&
        retry.title === desired.title &&
        retry.body === desired.body &&
        retry.folder === desired.folder
      )
        return {
          saved: true,
          id,
          revision: baseRevision + 1,
          updated_at: retry.created_at,
          retry: true,
        };
      conflict();
    }
    const updatedAt = now();
    return inTransaction(() => {
      const fresh = getNote.get(id);
      if (Number(fresh.revision) !== baseRevision) {
        const racedRetry = getVersion.get(id, baseRevision + 1);
        if (
          racedRetry &&
          racedRetry.title === desired.title &&
          racedRetry.body === desired.body &&
          racedRetry.folder === desired.folder
        )
          return {
            saved: true,
            id,
            revision: baseRevision + 1,
            updated_at: racedRetry.created_at,
            retry: true,
          };
        conflict();
      }
      const revision = baseRevision + 1;
      db.prepare(
        `INSERT INTO note_versions(note_id,revision,title,body,folder,task,run_key,observed_at,sources_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)`,
      ).run(
        id,
        revision,
        desired.title,
        desired.body,
        desired.folder,
        base.task,
        base.run_key,
        base.observed_at,
        base.sources_json,
        updatedAt,
      );
      attachSnapshot(id, baseRevision, revision);
      db.prepare(
        `UPDATE notes SET title=?,body=?,folder=?,revision=?,updated_at=? WHERE id=? AND revision=?`,
      ).run(
        desired.title,
        desired.body,
        desired.folder,
        revision,
        updatedAt,
        id,
        baseRevision,
      );
      return { saved: true, id, revision, updated_at: updatedAt };
    });
  }

  function listFolders() {
    const rows = db
      .prepare(
        "SELECT folder,COUNT(*) AS count FROM notes GROUP BY folder ORDER BY folder",
      )
      .all();
    const counts = new Map();
    for (const row of rows) {
      counts.set(row.folder, Number(row.count));
      const parts = row.folder ? row.folder.split("/") : [];
      while (parts.length > 1) {
        parts.pop();
        const parent = parts.join("/");
        if (!counts.has(parent)) counts.set(parent, 0);
      }
    }
    return {
      folders: [...counts]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([folder, count]) => ({ folder, count })),
    };
  }

  function putObject(bytes, hash) {
    const target = path.join(filesDir, hash);
    if (existsSync(target)) {
      const info = lstatSync(target);
      if (info.isSymbolicLink() || !info.isFile())
        throw fail("INVALID_OBJECT_STORE");
      if (sha256(readFileSync(target)) !== hash)
        throw fail("OBJECT_HASH_MISMATCH");
      return target;
    }
    const temp = path.join(filesDir, `.${hash}.${randomUUID()}.tmp`);
    const fd = openSync(temp, "wx", 0o600);
    try {
      writeFileSync(fd, bytes);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    try {
      renameSync(temp, target);
    } catch (error) {
      try {
        if (existsSync(target) && sha256(readFileSync(target)) === hash)
          return target;
      } finally {
        try {
          unlinkSyncLocal(temp);
        } catch {}
      }
      throw error;
    }
    chmodSyncLocal(target, 0o600);
    return target;
  }

  function attachFile({ id, revision, attachment_key, path: inputPath }) {
    const key = assertString(attachment_key, "attachment_key", 180, {
      min: 1,
      trim: true,
    });
    if (typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/iu.test(id))
      throw fail("NOT_FOUND");
    if (!Number.isInteger(revision) || revision < 1)
      throw fail("INVALID_INPUT");
    const sourcePath = ensureInputPath(imports, inputPath);
    const bytes = readFileSync(sourcePath);
    if (bytes.length > MAX_FILE) throw fail("FILE_TOO_LARGE");
    const hash = sha256(bytes);
    const filename = path.basename(sourcePath).slice(0, 240);
    const inputHash = payloadHash({
      sha256: hash,
      bytes: bytes.length,
      filename,
    });
    return inTransaction(() => {
      const raced = db
        .prepare(
          "SELECT * FROM attachment_receipts WHERE note_id=? AND attachment_key=?",
        )
        .get(id, key);
      if (raced) {
        if (raced.payload_hash !== inputHash)
          conflict(
            "This attachment_key was already used with different content.",
          );
        return {
          saved: true,
          id,
          attachment_id: raced.attachment_id,
          revision: Number(raced.revision),
          sha256: hash,
          bytes: bytes.length,
          retry: true,
        };
      }
      const fresh = getNote.get(id);
      if (!fresh) throw fail("NOT_FOUND");
      if (Number(fresh.revision) !== revision) conflict();
      // Keep receipt lookup and the base-revision check in this same write
      // transaction. A concurrent identical request must see its receipt
      // before a now-stale revision can turn an idempotent retry into conflict.
      putObject(bytes, hash);
      const attachmentId = randomUUID();
      const createdAt = now();
      const next = revision + 1;
      db.prepare(
        `INSERT INTO attachments(id,note_id,attachment_key,sha256,bytes,filename,mime_type,created_at)
        VALUES(?,?,?,?,?,?,?,?)`,
      ).run(
        attachmentId,
        id,
        key,
        hash,
        bytes.length,
        filename,
        mimeFor(filename),
        createdAt,
      );
      const version = getVersion.get(id, revision);
      db.prepare(
        `INSERT INTO note_versions(note_id,revision,title,body,folder,task,run_key,observed_at,sources_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)`,
      ).run(
        id,
        next,
        version.title,
        version.body,
        version.folder,
        version.task,
        version.run_key,
        version.observed_at,
        version.sources_json,
        createdAt,
      );
      attachSnapshot(id, revision, next, attachmentId);
      db.prepare(
        "UPDATE notes SET revision=?,updated_at=? WHERE id=? AND revision=?",
      ).run(next, createdAt, id, revision);
      db.prepare(
        `INSERT INTO attachment_receipts(note_id,attachment_key,payload_hash,revision,attachment_id,saved_at)
        VALUES(?,?,?,?,?,?)`,
      ).run(id, key, inputHash, next, attachmentId, createdAt);
      return {
        saved: true,
        id,
        attachment_id: attachmentId,
        revision: next,
        sha256: hash,
        bytes: bytes.length,
      };
    });
  }

  function listFiles({ folder, query = "", limit = 20, offset = 0 } = {}) {
    query = assertString(query, "query", 500, { trim: true });
    if (
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100 ||
      !Number.isInteger(offset) ||
      offset < 0 ||
      offset > 10_000_000
    )
      throw fail("INVALID_INPUT");
    const clauses = [];
    const params = [];
    if (folder !== undefined) {
      const normalized = normalizeFolder(folder);
      if (normalized) {
        clauses.push("(n.folder=? OR n.folder LIKE ? ESCAPE '\\')");
        params.push(normalized, `${escapeLike(normalized)}/%`);
      } else clauses.push("n.folder=''");
    }
    if (query) {
      const pattern = `%${escapeLike(query)}%`;
      clauses.push(
        "(a.filename LIKE ? ESCAPE '\\' OR n.title LIKE ? ESCAPE '\\')",
      );
      params.push(pattern, pattern);
    }
    const where = `${clauses.length ? "AND " + clauses.join(" AND ") : ""}`;
    const base = `FROM notes n JOIN version_attachments va ON va.note_id=n.id AND va.revision=n.revision JOIN attachments a ON a.id=va.attachment_id WHERE 1=1 ${where}`;
    const total = Number(
      db.prepare(`SELECT COUNT(*) AS total ${base}`).get(...params).total,
    );
    const items = db
      .prepare(
        `SELECT a.id,a.note_id AS report_id,a.attachment_key,a.filename,a.mime_type,a.bytes,a.sha256,n.title,n.folder,n.revision
      ${base} ORDER BY a.created_at DESC,a.id LIMIT ? OFFSET ?`,
      )
      .all(...params, limit, offset);
    return {
      total,
      limit,
      offset,
      items: items.map((x) => ({
        ...x,
        bytes: Number(x.bytes),
        revision: Number(x.revision),
      })),
    };
  }

  function readFile({ id, offset = 0, limit = DEFAULT_READ } = {}) {
    if (typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f-]{27}$/iu.test(id))
      throw fail("NOT_FOUND");
    if (
      !Number.isInteger(offset) ||
      offset < 0 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > MAX_READ
    )
      throw fail("INVALID_INPUT");
    const meta = db.prepare("SELECT * FROM attachments WHERE id=?").get(id);
    if (!meta) throw fail("NOT_FOUND");
    const objectPath = path.join(filesDir, meta.sha256);
    const info = lstatSync(objectPath);
    if (info.isSymbolicLink() || !info.isFile())
      throw fail("OBJECT_HASH_MISMATCH");
    const bytes = readFileSync(objectPath);
    if (bytes.length !== Number(meta.bytes) || sha256(bytes) !== meta.sha256)
      throw fail("OBJECT_HASH_MISMATCH");
    let decoded;
    try {
      decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      throw fail(
        "BINARY_FILE",
        "Only valid UTF-8 text files can be read through read_file.",
      );
    }
    const start = Math.min(offset, bytes.length);
    let actualStart = start;
    while (actualStart < bytes.length && (bytes[actualStart] & 0xc0) === 0x80)
      actualStart++;
    let end = Math.min(actualStart + limit, bytes.length);
    while (
      end > actualStart &&
      end < bytes.length &&
      (bytes[end] & 0xc0) === 0x80
    )
      end--;
    // A small requested limit can land inside a multibyte code point. Include
    // that one code point so every successful chunk advances the byte offset.
    if (end === actualStart && actualStart < bytes.length) {
      const lead = bytes[actualStart];
      const width = lead < 0x80 ? 1 : lead < 0xe0 ? 2 : lead < 0xf0 ? 3 : 4;
      end = Math.min(actualStart + width, bytes.length);
    }
    // Validate that the bounded slice ends on a UTF-8 codepoint boundary.
    const text = new TextDecoder("utf-8", { fatal: true }).decode(
      bytes.subarray(actualStart, end),
    );
    return {
      id,
      filename: meta.filename,
      mime_type: meta.mime_type,
      sha256: meta.sha256,
      total_bytes: bytes.length,
      offset: actualStart,
      next_offset: end < bytes.length ? end : null,
      text,
    };
  }

  return {
    dataDir: root,
    importDir: imports,
    listFolders,
    saveNote,
    getSaveReceipt,
    searchUpdates,
    readUpdate,
    updateNote,
    attachFile,
    listFiles,
    readFile,
    close() {
      db.close();
    },
  };
}
