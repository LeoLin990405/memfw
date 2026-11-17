"""A tiny stdio MCP server that replays recorded OCR tool answers (tests only).

Settings come from the environment (the adapter's ``mcp_env``):
FAKE_MCP_RECORDED (the JSON file), FAKE_MCP_LOG (a JSON-lines log of calls and
the environment keys it saw), FAKE_MCP_MODE (normal | scan | boxes | escape |
hang | flood | flood_slow | spawn | tool_error | no_async | structured | noise).
"""

import json
import os
import sys
import time

RECORDED = json.load(open(os.environ["FAKE_MCP_RECORDED"], encoding="utf-8"))
LOG = os.environ.get("FAKE_MCP_LOG", "")
MODE = os.environ.get("FAKE_MCP_MODE", "normal")
STATUS_CALLS = [0]


def log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def fill(value, path, out_dir):
    text = json.dumps(value).replace("{path}", path).replace("{out_dir}", out_dir)
    return json.loads(text)


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def answer(name, args):
    path, out_dir = args.get("path", ""), args.get("out_dir", "")
    if MODE == "tool_error":
        return {"content": [{"type": "text", "text": "engine crashed"}], "isError": True}
    if name == "pdf_info":
        payload = RECORDED[
            "pdf_info_scan" if MODE in ("scan", "escape", "no_async") else "pdf_info_text"
        ]
    elif name == "pdf_text":
        os.makedirs(os.path.join(out_dir, "original"), exist_ok=True)
        with open(os.path.join(out_dir, "original", "original.txt"), "w", encoding="utf-8") as h:
            h.write(RECORDED["pdf_text_file"])
        payload = RECORDED["pdf_text"]
    elif name in ("ocr_pdf", "ocr_submit"):
        with open(os.path.join(out_dir, "ocr.md"), "w", encoding="utf-8") as h:
            h.write(RECORDED["ocr_md_file"])
        payload = RECORDED[name]
        if MODE == "escape":
            payload = dict(payload, output_file="/etc/hosts")
        if name == "ocr_submit":
            answer.out_dir = out_dir
    elif name == "ocr_job_status":
        STATUS_CALLS[0] += 1
        key = "ocr_job_status_running" if STATUS_CALLS[0] == 1 else "ocr_job_status_written"
        payload = RECORDED[key]
        out_dir = getattr(answer, "out_dir", "")
    elif name == "ocr_job_result":
        payload = RECORDED["ocr_job_result"]
        out_dir = getattr(answer, "out_dir", "")
    elif name == "ocr_image":
        payload = RECORDED["ocr_image_boxes" if MODE == "boxes" else "ocr_image"]
    else:
        return {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
    payload = fill(payload, path, out_dir)
    if MODE == "structured":
        return {"content": [], "structuredContent": payload, "isError": False}
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


def spawn_worker(out_dir):
    """An engine child that ignores SIGTERM and keeps writing into out_dir."""

    import subprocess

    code = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
        "for n in range(600):\n"
        "    os.makedirs(sys.argv[1], exist_ok=True)\n"
        "    open(os.path.join(sys.argv[1], 'late.txt'), 'w').write(str(n))\n"
        "    time.sleep(0.05)\n"
    )
    pid_file = os.environ["FAKE_MCP_LOG"] + ".worker"
    subprocess.Popen([sys.executable, "-I", "-c", code, out_dir, pid_file])
    for _ in range(200):  # answer only once the worker runs
        if os.path.exists(pid_file) and open(pid_file).read():
            return
        time.sleep(0.025)


def main():
    log({"env": sorted(os.environ)})
    for line in sys.stdin:
        message = json.loads(line)
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            continue  # a notification
        if MODE == "noise":
            sys.stdout.write("not json at all\n")
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
            send({"jsonrpc": "2.0", "id": 999, "method": "ping"})
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": RECORDED["server_info"],
            }
        elif method == "tools/list":
            names = RECORDED["tools"]
            if MODE == "no_async":
                names = [n for n in names if n not in ("ocr_submit", "ocr_job_status")]
            result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names]}
        elif method == "tools/call":
            params = message["params"]
            if MODE == "spawn" and params["arguments"].get("out_dir"):
                spawn_worker(params["arguments"]["out_dir"])
            log({"tool": params["name"], "arguments": params["arguments"]})
            if MODE == "hang":
                continue
            if MODE in ("flood", "flood_slow"):
                # requests of its own, never reading our answers
                count = 5000 if MODE == "flood" else 20
                for n in range(count):
                    send({"jsonrpc": "2.0", "id": f"srv-{n}", "method": "ping"})
                time.sleep(3600)
            result = answer(params["name"], params["arguments"])
        else:
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no"}})
            continue
        send({"jsonrpc": "2.0", "id": ident, "result": result})


main()
