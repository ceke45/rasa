# app.py
import os, json, re
from pathlib import Path
from flask import Flask, send_from_directory, request, jsonify
from werkzeug.utils import secure_filename
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# ── 설정 ─────────────────────────────────────────────
RASA_URL   = "http://localhost:5005"
SENDER_ID  = "local-user"
UPLOAD_DIR = "uploads"
LOG_DIR    = "logs"
KST        = ZoneInfo("Asia/Seoul")
# ────────────────────────────────────────────────────

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ── 시간 유틸 (KST만 사용) ───────────────────────────
def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")

def now_kst_human() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d (%a) %H:%M:%S")

def ensure_kst_fields(msg: dict) -> dict:
    """메시지에 KST 타임스탬프만 주입 (UTC 없음)."""
    msg["ts_kst"] = now_kst_iso()
    msg["ts_kst_human"] = now_kst_human()
    # 더 이상 msg['ts'](UTC)는 넣지 않음
    return msg

# ── 모드 전환 식별 ──────────────────────────────────
_MODE_TEXT_RE = re.compile(
    r'(모드\s*로\s*전환했어요|모드\s*전환|\[mode[_\s-]*change\]|switched\s+to.*mode|mode\s+changed)',
    re.IGNORECASE
)
def is_mode_change(obj) -> bool:
    if not isinstance(obj, dict): return False
    meta = obj.get("meta") or {}
    role = str(obj.get("role") or "")
    text = str(obj.get("text") or "")
    if isinstance(meta, dict) and meta.get("action") == "action_set_mode":
        return True
    if role.lower() == "system":
        return True
    return bool(_MODE_TEXT_RE.search(text))

# ── JSONL 로깅 (KST만 기록) ─────────────────────────
def _log_path_for_today() -> Path:
    return Path(LOG_DIR) / f"chat-{datetime.now(KST).strftime('%Y%m%d')}.jsonl"

def log_event(event: dict):
    """모드 전환/시스템 이벤트는 기록하지 않음. KST만 저장."""
    if is_mode_change(event):
        return
    record = {
        "ts_kst": event.get("ts_kst") or now_kst_iso(),
        "ts_kst_human": event.get("ts_kst_human") or now_kst_human(),
        "sender_id": event.get("sender_id") or SENDER_ID,
        "role": event.get("role") or "",
        "text": event.get("text") or "",
        "mode": event.get("mode") or event.get("meta", {}).get("mode", ""),
        "meta": event.get("meta") or {},
    }
    with open(_log_path_for_today(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ── 정적 파일 ────────────────────────────────────────
@app.route("/")
def serve_index():
    return send_from_directory(".", "index.html")

@app.route("/<path:path>")
def serve_file(path):
    return send_from_directory(".", path)

# ── 업로드 ───────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "file 필드가 없습니다."}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"ok": False, "msg": "파일명이 비어있습니다."}), 400

    filename = secure_filename(f.filename)
    save_path = os.path.join(UPLOAD_DIR, filename)
    try:
        f.save(save_path)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"파일 저장 실패: {e}"}), 500

    mime = f.mimetype or "application/octet-stream"

    # 1) 인텐트 트리거
    try:
        trigger_payload = {
            "name": "file_uploaded",
            "entities": {
                "uploaded_file_path": save_path,
                "uploaded_file_mime": mime,
            },
        }
        r = requests.post(
            f"{RASA_URL}/conversations/{SENDER_ID}/trigger_intent",
            json=trigger_payload, timeout=30,
        )
        if r.status_code >= 300:
            return jsonify({"ok": False, "msg": f"Rasa trigger 실패: {r.text}"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "msg": f"Rasa trigger 예외: {e}"}), 500

    # 2) 요약 실행
    try:
        msg_payload = {"sender": SENDER_ID, "message": "/file_uploaded"}
        r2 = requests.post(f"{RASA_URL}/webhooks/rest/webhook", json=msg_payload, timeout=90)
        if not r2.ok:
            return jsonify({"ok": False, "msg": f"Rasa 응답 실패: {r2.text}"}), 500

        replies, replies_rich = [], []
        for m in r2.json():
            if not isinstance(m, dict) or is_mode_change(m):
                continue
            if "text" in m:
                replies.append(m["text"])
                rich = ensure_kst_fields({"text": m["text"], "role": "bot", "meta": m.get("meta", {})})
                replies_rich.append(rich)
                log_event({**rich, "sender_id": SENDER_ID})

        return jsonify({"ok": True, "path": save_path, "mime": mime,
                        "replies": replies, "replies_rich": replies_rich})
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "msg": f"Rasa 요청 예외: {e}"}), 500

# ── 일반 메시지 ──────────────────────────────────────
@app.route("/send", methods=["POST"])
def send():
    """
    { "text": "..." } -> Rasa에 전달
    - /set_mode{...}: 응답/로그 모두 폐기, [] 반환
    - 그 외: 모드 전환/시스템 메시지는 응답/로그에서 제거
    - 타임스탬프는 KST만 포함(ts_kst, ts_kst_human)
    """
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "text가 비었습니다."}), 400

    is_setmode = text.startswith("/set_mode")
    if not is_setmode:
        log_event(ensure_kst_fields({"role": "user", "text": text, "meta": {}}))

    try:
        payload = {"sender": SENDER_ID, "message": text}
        r = requests.post(f"{RASA_URL}/webhooks/rest/webhook", json=payload, timeout=60)
        if not r.ok:
            return jsonify({"ok": False, "msg": f"Rasa 응답 실패: {r.text}"}), 500

        raw = r.json()

        if is_setmode:
            return jsonify([])

        if isinstance(raw, list):
            filtered = [m for m in raw if isinstance(m, dict) and not is_mode_change(m)]
        elif isinstance(raw, dict):
            filtered = [] if is_mode_change(raw) else [raw]
        else:
            filtered = []

        msgs = []
        for m in filtered:
            enriched = ensure_kst_fields(m if isinstance(m, dict) else {"text": str(m)})
            msgs.append(enriched)
            log_event({**enriched, "role": "bot"})

        return jsonify(msgs)

    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "msg": f"Rasa 요청 예외: {e}"}), 500

# ── 헬스체크 ─────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
