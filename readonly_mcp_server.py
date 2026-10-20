#!/usr/bin/env python3
"""読み取り専用のMCPサーバー（Python標準ライブラリのみ・追加インストール不要）

指定したフォルダ（文書フォルダ）の中にあるテキスト文書だけを、
AIが「一覧を見る」「探す」「必要な行だけ読む」ことができるようにする。
書き込み・削除・外部通信の機能は持たない。
読んだ記録（いつ・どの道具で・どのファイルの何行目か）を JSON Lines で残す。

通信方式: stdio（標準入出力）。1行に1つの JSON-RPC 2.0 メッセージ。

使い方:
    python readonly_mcp_server.py --root <文書フォルダ> [--log <記録ファイル>]
"""

import argparse
import datetime
import json
import os
import sys

SERVER_NAME = "readonly-docs"
SERVER_VERSION = "0.1.0"

# 対応するMCPの版（新しい順）。クライアントが求めた版が一覧にあればその版で答え、
# 無ければ一覧の先頭の版で答える。
SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

ALLOWED_EXTENSIONS = {".txt", ".md", ".csv"}
MAX_FILE_BYTES = 2 * 1024 * 1024      # 1ファイル2MBまで
MAX_LINES_PER_READ = 400              # 1回に読める行数の上限
MAX_SEARCH_HITS = 50                  # 検索結果の上限

TOOLS = [
    {
        "name": "list_documents",
        "description": "文書フォルダ内で読めるテキスト文書（.txt .md .csv）の一覧と行数を返す。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "search_documents",
        "description": "文書フォルダ内の全文書から語句を探し、見つかったファイル名・行番号・その行を返す。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "探す語句（大文字小文字は区別しない）"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "read_document",
        "description": "指定した文書の指定した行だけを、行番号付きで返す。行を省略すると先頭から読む。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "list_documents が返したファイル名（文書フォルダからの相対パス）"},
                "start_line": {"type": "integer", "minimum": 1, "description": "読み始める行（1から）"},
                "end_line": {"type": "integer", "minimum": 1, "description": "読み終える行（この行を含む）"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class ToolError(Exception):
    """道具の実行は失敗したが、サーバーは動き続ける種類の誤り"""


class ReadOnlyDocs:
    def __init__(self, root, log_path):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise SystemExit(f"文書フォルダが見つかりません: {root}")
        self.log_path = log_path

    # ---- 記録 ----
    def log(self, tool, **detail):
        if not self.log_path:
            return
        entry = {
            "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
        }
        entry.update(detail)
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---- 文書フォルダの外へ出ないための確認 ----
    def resolve(self, rel_path):
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ToolError("path を指定してください。")
        full = os.path.realpath(os.path.join(self.root, rel_path))
        if os.path.commonpath([self.root, full]) != self.root:
            raise ToolError("文書フォルダの外は読めません。")
        if not os.path.isfile(full):
            raise ToolError(f"ファイルが見つかりません: {rel_path}")
        if os.path.splitext(full)[1].lower() not in ALLOWED_EXTENSIONS:
            raise ToolError("読めるのは .txt .md .csv だけです。")
        if os.path.getsize(full) > MAX_FILE_BYTES:
            raise ToolError("ファイルが大きすぎます（2MBまで）。")
        return full

    def iter_documents(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.realpath(os.path.join(dirpath, name))
                if os.path.commonpath([self.root, full]) != self.root:
                    continue
                if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
                    continue
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                yield os.path.relpath(full, self.root).replace(os.sep, "/"), full

    @staticmethod
    def read_lines(full):
        with open(full, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read().splitlines()

    # ---- 道具 ----
    def list_documents(self, args):
        rows = []
        for rel, full in self.iter_documents():
            rows.append(f"{rel}（{len(self.read_lines(full))}行）")
        self.log("list_documents", count=len(rows))
        if not rows:
            return "読める文書がありません。"
        return "\n".join(rows)

    def search_documents(self, args):
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query を指定してください。")
        q = query.lower()
        hits = []
        for rel, full in self.iter_documents():
            for no, line in enumerate(self.read_lines(full), start=1):
                if q in line.lower():
                    hits.append((rel, no, line.strip()))
                    if len(hits) >= MAX_SEARCH_HITS:
                        break
            if len(hits) >= MAX_SEARCH_HITS:
                break
        self.log("search_documents", query=query,
                 hits=[{"path": h[0], "line": h[1]} for h in hits])
        if not hits:
            return f"「{query}」は見つかりませんでした。"
        out = [f"{rel} {no}行目: {text}" for rel, no, text in hits]
        if len(hits) >= MAX_SEARCH_HITS:
            out.append(f"（上限の{MAX_SEARCH_HITS}件で打ち切りました）")
        return "\n".join(out)

    def read_document(self, args):
        rel = args.get("path")
        full = self.resolve(rel)
        lines = self.read_lines(full)
        total = len(lines)
        start = args.get("start_line", 1)
        end = args.get("end_line", min(total, start + MAX_LINES_PER_READ - 1))
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ToolError("start_line と end_line は 1 以上で、start_line ≦ end_line にしてください。")
        if total == 0:
            self.log("read_document", path=rel, start_line=0, end_line=0)
            return f"{rel} は空です。"
        if start > total:
            raise ToolError(f"{rel} は{total}行しかありません。")
        end = min(end, total, start + MAX_LINES_PER_READ - 1)
        self.log("read_document", path=rel, start_line=start, end_line=end)
        body = "\n".join(f"{no}: {lines[no - 1]}" for no in range(start, end + 1))
        return f"{rel}（全{total}行のうち {start}〜{end}行目）\n{body}"

    def call(self, name, args):
        handlers = {
            "list_documents": self.list_documents,
            "search_documents": self.search_documents,
            "read_document": self.read_document,
        }
        if name not in handlers:
            raise KeyError(name)
        return handlers[name](args or {})


# ---- JSON-RPC の送受信 ----
def send(message):
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def reply(msg_id, result):
    send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def reply_error(msg_id, code, message):
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(docs, msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    is_request = "id" in msg

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        reply(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "読み取り専用です。文書の一覧・検索・指定行の読み取りだけができます。",
        })
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        try:
            text = docs.call(name, params.get("arguments"))
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except KeyError:
            reply_error(msg_id, -32602, f"知らない道具です: {name}")
        except ToolError as e:
            docs.log("error", tool_name=name, message=str(e))
            reply(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
    elif method and method.startswith("notifications/"):
        pass  # 通知には返事をしない
    elif is_request:
        reply_error(msg_id, -32601, f"対応していない要求です: {method}")


def main():
    parser = argparse.ArgumentParser(description="読み取り専用MCPサーバー")
    parser.add_argument("--root", required=True, help="読ませる文書フォルダ")
    parser.add_argument("--log", default=None,
                        help="記録ファイル（省略時はこのファイルと同じ場所の logs/access_log.jsonl）")
    a = parser.parse_args()
    log_path = a.log or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "access_log.jsonl")
    docs = ReadOnlyDocs(a.root, log_path)

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply_error(None, -32700, "JSONとして読めません。")
            continue
        if not isinstance(msg, dict):
            reply_error(None, -32600, "要求の形が正しくありません。")
            continue
        try:
            handle(docs, msg)
        except Exception as e:  # 想定外の誤りでもサーバーは止めない
            print(f"[{SERVER_NAME}] 想定外の誤り: {e!r}", file=sys.stderr)
            if "id" in msg:
                reply_error(msg.get("id"), -32603, "サーバー内部の誤りです。")


if __name__ == "__main__":
    main()
