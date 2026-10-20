#!/usr/bin/env python3
"""readonly_mcp_server.py の動作確認（標準ライブラリのみ）
使い方: python test_client.py
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "logs", "test_access_log.jsonl")
if os.path.exists(LOG):
    os.remove(LOG)

proc = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "readonly_mcp_server.py"),
     "--root", os.path.join(HERE, "docs"), "--log", LOG],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE)

def rpc(msg, expect_reply=True):
    proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()
    if expect_reply:
        return json.loads(proc.stdout.readline().decode("utf-8"))

results = []
def check(label, ok):
    results.append(ok)
    print(("OK  " if ok else "NG  ") + label)

r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}})
check("初期化（版 2025-06-18 で応答）", r["result"]["protocolVersion"] == "2025-06-18")
rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)

r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
check("道具が3つ", [t["name"] for t in r["result"]["tools"]] ==
      ["list_documents", "search_documents", "read_document"])

r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_documents", "arguments": {}}})
check("一覧に sample.md", "sample.md" in r["result"]["content"][0]["text"])

r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "search_documents", "arguments": {"query": "記録"}}})
check("検索で行番号つきの結果", "行目" in r["result"]["content"][0]["text"])

r = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "read_document", "arguments": {"path": "sample.md", "start_line": 6, "end_line": 7}}})
check("指定行だけ読める", r["result"]["content"][0]["text"].count("\n") == 2)

r = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "read_document", "arguments": {"path": "../../outside/secret.txt"}}})
check("文書フォルダの外は拒否", r["result"]["isError"] is True)

r = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "write_document", "arguments": {}}})
check("書き込みの道具は存在しない", "error" in r)

r = rpc({"jsonrpc": "2.0", "id": 8, "method": "ping"})
check("ping に応答", r.get("result") == {})

proc.stdin.close(); proc.wait(timeout=5)

with open(LOG, encoding="utf-8") as f:
    log = [json.loads(l) for l in f]
check("読んだ記録が残る（read_document の行範囲）",
      any(e["tool"] == "read_document" and e["start_line"] == 6 and e["end_line"] == 7 for e in log))

print(f"\n{sum(results)}/{len(results)} 件合格")
sys.exit(0 if all(results) else 1)
