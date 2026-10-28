import test from "node:test";
import assert from "node:assert/strict";
import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  chmodSync,
  existsSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { Client } from "@modelcontextprotocol/client";
import {
  getDefaultEnvironment,
  StdioClientTransport,
} from "@modelcontextprotocol/client/stdio";
import { createStore } from "../src/store.mjs";

const packageRoot = path.resolve(import.meta.dirname, "..");
const serverPath = path.join(packageRoot, "src/server.mjs");
const temporary = () =>
  mkdtempSync(path.join(os.tmpdir(), "personal-inbox-mcp-test-"));
function data(result) {
  const text = result.content?.find((part) => part.type === "text")?.text;
  assert.ok(text, "MCP response includes a text block");
  return JSON.parse(text);
}
async function start(root, imports) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath, "--data-dir", root, "--import-dir", imports],
    cwd: packageRoot,
    stderr: "pipe",
  });
  const client = new Client({ name: "personal-inbox-test", version: "1.0.0" });
  await client.connect(transport);
  return { client, transport };
}

async function startWithEnv(root, imports) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    cwd: packageRoot,
    env: {
      ...getDefaultEnvironment(),
      PERSONAL_INBOX_DIR: root,
      PERSONAL_INBOX_IMPORT_DIR: imports,
    },
    stderr: "pipe",
  });
  const client = new Client({
    name: "personal-inbox-env-test",
    version: "1.0.0",
  });
  await client.connect(transport);
  return { client, transport };
}

test("stdio MCP preserves receipts and immutable revisions across restart; attachments are bounded to import root", async (t) => {
  const base = temporary();
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, "private-store");
  const imports = path.join(base, "imports");
  mkdirSync(imports, { mode: 0o700 });
  const outside = path.join(base, "outside.txt");
  writeFileSync(outside, "private");
  const firstFile = path.join(imports, "evidence.md");
  const binaryFile = path.join(imports, "archive.bin");
  writeFileSync(firstFile, "alpha\nβeta\ngamma\n");
  writeFileSync(binaryFile, Buffer.from([0xff, 0x00, 0x01]));

  let { client, transport } = await start(root, imports);
  t.after(async () => {
    try {
      await client.close();
    } catch {}
    try {
      await transport.close();
    } catch {}
  });
  const tools = await client.listTools();
  const names = new Set(tools.tools.map((tool) => tool.name));
  for (const name of [
    "list_folders",
    "save_note",
    "get_receipt",
    "search_updates",
    "read_update",
    "update_note",
    "attach_file",
    "list_files",
    "read_file",
  ])
    assert.ok(names.has(name), name);
  assert.equal(
    (await client.callTool({ name: "list_folders", arguments: {} })).isError,
    undefined,
  );
  const payload = {
    title: "Search work",
    body: "Decision: keep the current index.\nNext: inspect ranking.",
    folder: "notes/web/search",
    task: "search-handoff",
    run_key: "turn-1",
    sources: [{ title: "Search guide", url: "https://example.com/guide" }],
    observed_at: "2026-10-28T00:00:00Z",
  };
  const saved = data(
    await client.callTool({ name: "save_note", arguments: payload }),
  );
  assert.equal(saved.saved, true);
  assert.equal(saved.revision, 1);
  assert.equal(
    data(await client.callTool({ name: "save_note", arguments: payload })).id,
    saved.id,
  );
  const collision = data(
    await client.callTool({
      name: "save_note",
      arguments: { ...payload, body: "different" },
    }),
  );
  assert.equal(collision.error, "CONFLICT");
  const receipt = data(
    await client.callTool({
      name: "get_receipt",
      arguments: { task: payload.task, run_key: payload.run_key },
    }),
  );
  assert.equal(receipt.id, saved.id);
  assert.equal(receipt.revision, 1);
  const search = data(
    await client.callTool({
      name: "search_updates",
      arguments: { query: "current index", folder: "notes", limit: 10 },
    }),
  );
  assert.equal(search.total, 1);
  assert.equal(search.items[0].summary.includes("Decision"), true);
  assert.equal("body" in search.items[0], false);
  const revision1 = data(
    await client.callTool({
      name: "read_update",
      arguments: { id: saved.id, revision: 1 },
    }),
  );
  assert.match(revision1.body, /Decision: keep the current index/);

  const updateArgs = {
    id: saved.id,
    revision: 1,
    body: "Decision: keep the current index.\nNext: inspect ranking and keyboard access.",
  };
  const updated = data(
    await client.callTool({ name: "update_note", arguments: updateArgs }),
  );
  assert.equal(updated.revision, 2);
  assert.equal(
    data(await client.callTool({ name: "update_note", arguments: updateArgs }))
      .revision,
    2,
  );
  const stale = data(
    await client.callTool({
      name: "update_note",
      arguments: { id: saved.id, revision: 1, body: "conflicting edit" },
    }),
  );
  assert.equal(stale.error, "CONFLICT");
  assert.match(
    data(
      await client.callTool({
        name: "read_update",
        arguments: { id: saved.id, revision: 1 },
      }),
    ).body,
    /Next: inspect ranking\./,
  );

  const attachedArgs = {
    id: saved.id,
    revision: 2,
    attachment_key: "evidence-v1",
    path: firstFile,
  };
  const attached = data(
    await client.callTool({ name: "attach_file", arguments: attachedArgs }),
  );
  assert.equal(attached.saved, true);
  assert.equal(attached.revision, 3);
  assert.equal(attached.bytes, Buffer.byteLength(readFileSync(firstFile)));
  assert.equal(
    attached.sha256,
    createHash("sha256").update(readFileSync(firstFile)).digest("hex"),
  );
  assert.equal(
    data(
      await client.callTool({ name: "attach_file", arguments: attachedArgs }),
    ).revision,
    3,
  );
  const list = data(
    await client.callTool({
      name: "list_files",
      arguments: { folder: "notes/web" },
    }),
  );
  assert.equal(list.total, 1);
  assert.equal(list.items[0].sha256, attached.sha256);
  const file = data(
    await client.callTool({
      name: "read_file",
      arguments: { id: attached.attachment_id, offset: 0, limit: 8 },
    }),
  );
  assert.match(file.text, /alpha/);
  assert.notEqual(file.next_offset, null);
  const last = data(
    await client.callTool({
      name: "read_file",
      arguments: {
        id: attached.attachment_id,
        offset: file.next_offset,
        limit: 100,
      },
    }),
  );
  assert.match(last.text, /gamma/);
  assert.equal(last.next_offset, null);
  const tinyChunk1 = data(
    await client.callTool({
      name: "read_file",
      arguments: { id: attached.attachment_id, offset: 6, limit: 1 },
    }),
  );
  assert.equal(tinyChunk1.text, "β");
  assert.equal(tinyChunk1.next_offset, 8);
  const tinyChunk2 = data(
    await client.callTool({
      name: "read_file",
      arguments: {
        id: attached.attachment_id,
        offset: tinyChunk1.next_offset,
        limit: 2,
      },
    }),
  );
  assert.equal(tinyChunk2.text, "et");
  assert.equal(tinyChunk2.next_offset, 10);
  const escaped = data(
    await client.callTool({
      name: "attach_file",
      arguments: {
        id: saved.id,
        revision: 3,
        attachment_key: "outside",
        path: outside,
      },
    }),
  );
  assert.equal(escaped.error, "INVALID_FILE_PATH");
  const traversal = data(
    await client.callTool({
      name: "attach_file",
      arguments: {
        id: saved.id,
        revision: 3,
        attachment_key: "traversal",
        path: `${imports}/../imports/evidence.md`,
      },
    }),
  );
  assert.equal(traversal.error, "INVALID_FILE_PATH");
  const link = path.join(imports, "link.txt");
  symlinkSync(outside, link);
  const symlink = data(
    await client.callTool({
      name: "attach_file",
      arguments: {
        id: saved.id,
        revision: 3,
        attachment_key: "symlink",
        path: link,
      },
    }),
  );
  assert.equal(symlink.error, "INVALID_FILE_PATH");
  const oldSnapshot = data(
    await client.callTool({
      name: "read_update",
      arguments: { id: saved.id, revision: 2 },
    }),
  );
  assert.equal(oldSnapshot.attachments.length, 0);
  const binary = data(
    await client.callTool({
      name: "attach_file",
      arguments: {
        id: saved.id,
        revision: 3,
        attachment_key: "binary-v1",
        path: binaryFile,
      },
    }),
  );
  assert.equal(binary.revision, 4);
  const binaryRead = await client.callTool({
    name: "read_file",
    arguments: { id: binary.attachment_id },
  });
  assert.equal(binaryRead.isError, true);
  assert.equal(data(binaryRead).error, "BINARY_FILE");
  assert.equal(
    data(
      await client.callTool({ name: "attach_file", arguments: attachedArgs }),
    ).revision,
    3,
  );

  await client.close();
  await transport.close();
  ({ client, transport } = await start(root, imports));
  t.after(async () => {
    try {
      await client.close();
    } catch {}
    try {
      await transport.close();
    } catch {}
  });
  const reread = data(
    await client.callTool({ name: "read_update", arguments: { id: saved.id } }),
  );
  assert.equal(reread.revision, 4);
  assert.equal(reread.attachments.length, 2);
  assert.equal(reread.body, updateArgs.body);
  assert.equal(
    data(
      await client.callTool({
        name: "get_receipt",
        arguments: { task: payload.task, run_key: payload.run_key },
      }),
    ).revision,
    1,
  );
  const folders = data(
    await client.callTool({ name: "list_folders", arguments: {} }),
  );
  assert.deepEqual(
    folders.folders.find((entry) => entry.folder === "notes"),
    { folder: "notes", count: 0 },
  );
  await client.close();
  await transport.close();

  const mode = lstatSync(root).mode & 0o777;
  assert.equal(mode, 0o700);
  assert.equal(lstatSync(path.join(root, "inbox.sqlite")).mode & 0o777, 0o600);
});

test("data store refuses Git repositories and folder traversal", (t) => {
  assert.throws(
    () =>
      createStore({
        dataDir: path.resolve(packageRoot, "../../.git/personal-inbox"),
      }),
    { code: "DATA_DIR_IN_GIT" },
  );
  const base = temporary();
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const preexisting = path.join(base, "preexisting-data");
  mkdirSync(preexisting, { mode: 0o755 });
  chmodSync(preexisting, 0o755);
  assert.throws(
    () =>
      createStore({
        dataDir: preexisting,
        importDir: path.join(base, "missing"),
      }),
    { code: "INVALID_IMPORT_DIR" },
  );
  assert.equal(lstatSync(preexisting).mode & 0o777, 0o755);
  assert.throws(
    () =>
      createStore({
        dataDir: path.join(base, "relative-import-data"),
        importDir: "relative-imports",
      }),
    { code: "INVALID_IMPORT_DIR" },
  );
  assert.equal(existsSync(path.join(base, "relative-import-data")), false);
  const store = createStore({ dataDir: path.join(base, "data") });
  t.after(() => store.close());
  assert.throws(
    () =>
      store.saveNote({
        title: "x",
        body: "y",
        task: "t",
        run_key: "r",
        folder: "notes/../escape",
      }),
    { code: "INVALID_FOLDER" },
  );
  assert.throws(
    () =>
      store.saveNote({
        title: "x",
        body: "y",
        task: "t",
        run_key: "r",
        folder: "/absolute",
      }),
    { code: "INVALID_FOLDER" },
  );
});

test("simultaneous attachment retries return the original receipt", async (t) => {
  const base = temporary();
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, "data");
  const imports = path.join(base, "imports");
  mkdirSync(imports, { mode: 0o700 });
  const source = path.join(imports, "concurrent.txt");
  writeFileSync(source, Buffer.alloc(16 * 1024 * 1024, 0x61));

  const first = await start(root, imports);
  const second = await start(root, imports);
  t.after(async () => {
    await Promise.allSettled([
      first.client.close(), second.client.close(),
      first.transport.close(), second.transport.close(),
    ]);
  });
  const saved = data(await first.client.callTool({
    name: "save_note",
    arguments: { title: "Race", body: "same base", task: "race", run_key: "one" },
  }));
  const request = {
    name: "attach_file",
    arguments: {
      id: saved.id,
      revision: 1,
      attachment_key: "same-key",
      path: source,
    },
  };
  // Both independent server processes receive the identical request before
  // either response is awaited, exercising their shared SQLite receipt race.
  const [a, b] = await Promise.all([
    first.client.callTool(request),
    second.client.callTool(request),
  ]);
  const resultA = data(a);
  const resultB = data(b);
  assert.equal(resultA.saved, true);
  assert.equal(resultB.saved, true);
  assert.equal(resultA.attachment_id, resultB.attachment_id);
  assert.equal(resultA.revision, 2);
  assert.equal(resultB.revision, 2);
});

test("environment variables select the data and import directories", async (t) => {
  const base = temporary();
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, "env-data");
  const imports = path.join(base, "env-imports");
  mkdirSync(imports, { mode: 0o700 });
  const { client, transport } = await startWithEnv(root, imports);
  t.after(async () => {
    try {
      await client.close();
    } catch {}
    try {
      await transport.close();
    } catch {}
  });
  const saved = data(
    await client.callTool({
      name: "save_note",
      arguments: {
        title: "env",
        body: "configured",
        task: "env",
        run_key: "1",
      },
    }),
  );
  assert.equal(saved.revision, 1);
  assert.equal(lstatSync(root).mode & 0o777, 0o700);
  await client.close();
  await transport.close();
});
